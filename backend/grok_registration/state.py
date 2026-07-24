"""Registration state, concurrency controls, and queue state helpers."""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from registration_state import load_state, save_state


# Hard cap for concurrent xAI protocol registrations.
# Batch count is intentionally uncapped — only concurrency bounds parallelism.
MAX_CONCURRENCY = int(os.environ.get("GROK2API_REG_MAX_CONCURRENCY", "10") or 10)
DEFAULT_CONCURRENCY = int(os.environ.get("GROK2API_REG_CONCURRENCY", "3") or 3)


_sessions, _batches = load_state("grok")
_lock = threading.RLock()


def _persist_monitor_state() -> None:
    with _lock:
        sessions = {sid: dict(session) for sid, session in _sessions.items()}
        batches = {bid: dict(batch) for bid, batch in _batches.items()}
    save_state("grok", sessions, batches)


_persist_timer: threading.Timer | None = None
_persist_timer_lock = threading.Lock()


def _request_monitor_persist() -> None:
    global _persist_timer
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _persist_timer_lock:
        if _persist_timer is not None and _persist_timer.is_alive():
            return
        _persist_timer = threading.Timer(0.25, _persist_monitor_state)
        _persist_timer.daemon = True
        _persist_timer.start()


# batch_id -> True while a local ThreadPool spawner is alive in this process.
_active_batch_runners: dict[str, bool] = {}
try:
    _LOCAL_CAPTCHA_CONCURRENCY = max(
        1,
        min(
            10,
            int(os.environ.get("GROK2API_LOCAL_CAPTCHA_CONCURRENCY", "3") or 3),
        ),
    )
except (TypeError, ValueError):
    _LOCAL_CAPTCHA_CONCURRENCY = 3
_local_captcha_slots = threading.BoundedSemaphore(_LOCAL_CAPTCHA_CONCURRENCY)
_pipeline_stagger_lock = threading.RLock()
_pipeline_next_allowed = {"probe": 0.0, "import": 0.0}
_pipeline_queue_lock = threading.RLock()
_pipeline_queues: dict[str, dict[str, Any]] = {}
PROBE_RECHECK_DELAYS = (30.0, 120.0, 300.0)
_probe_slots_lock = threading.RLock()
_probe_slots: dict[tuple[str, int], threading.BoundedSemaphore] = {}
try:
    _GLOBAL_REG_INFLIGHT_MAX = max(
        1,
        min(
            32,
            int(os.environ.get("GROK2API_REG_GLOBAL_INFLIGHT", "6") or 6),
        ),
    )
except (TypeError, ValueError):
    _GLOBAL_REG_INFLIGHT_MAX = 6
_global_reg_inflight = threading.Semaphore(_GLOBAL_REG_INFLIGHT_MAX)
_reg_soft_pause_until = 0.0
_reg_soft_pause_lock = threading.Lock()


def _note_reg_pressure(reason: str = "", *, pause_sec: float | None = None) -> None:
    """Back off new admissions briefly when upstream rate limits hit."""
    global _reg_soft_pause_until
    try:
        sec = float(
            pause_sec
            if pause_sec is not None
            else (os.environ.get("GROK2API_REG_SOFT_PAUSE_SEC", "8") or 8)
        )
    except (TypeError, ValueError):
        sec = 8.0
    sec = max(1.0, min(60.0, sec))
    with _reg_soft_pause_lock:
        _reg_soft_pause_until = max(_reg_soft_pause_until, time.time() + sec)
    if reason:
        print(f"[registration] soft-pause {sec:.0f}s ({reason})")


def _wait_reg_admission(*, check_cancel=None) -> None:
    """Block until a global inflight slot is free and the soft pause ends."""
    while True:
        if check_cancel is not None:
            check_cancel()
        with _reg_soft_pause_lock:
            wait = _reg_soft_pause_until - time.time()
        if wait > 0:
            time.sleep(min(1.0, wait))
            continue
        if _global_reg_inflight.acquire(blocking=False):
            return
        time.sleep(0.15)


def _release_reg_admission() -> None:
    try:
        _global_reg_inflight.release()
    except Exception:
        pass


def wait_pipeline_stagger(
    phase: str,
    delay_ms: int | float | None,
    *,
    should_cancel: Any | None = None,
) -> bool:
    """Space probe/import request starts across concurrent registration workers."""
    name = "import" if str(phase).lower() == "import" else "probe"
    try:
        interval = max(0.0, min(60.0, float(delay_ms or 0) / 1000.0))
    except (TypeError, ValueError):
        interval = 0.0
    if interval <= 0:
        return not bool(callable(should_cancel) and should_cancel())
    with _pipeline_stagger_lock:
        now = time.monotonic()
        scheduled = max(now, float(_pipeline_next_allowed.get(name) or 0.0))
        _pipeline_next_allowed[name] = scheduled + interval
    wait = scheduled - time.monotonic()
    while wait > 0:
        if callable(should_cancel) and should_cancel():
            return False
        time.sleep(min(0.2, wait))
        wait = scheduled - time.monotonic()
    return not bool(callable(should_cancel) and should_cancel())


REG_BATCH_RUNNER_LOCK_TTL = int(
    os.environ.get("GROK2API_REG_RUNNER_LOCK_TTL", "90") or 90
)


def _now() -> float:
    return time.time()


def _reg_redis() -> bool:
    try:
        from store.redis_client import redis_enabled

        return redis_enabled()
    except Exception:
        return False


def _batch_runner_lock_key(batch_id: str) -> str:
    try:
        from store.redis_client import key

        return key("reg", "runner", batch_id)
    except Exception:
        return f"g2a:reg:runner:{batch_id}"


def _try_acquire_batch_runner(batch_id: str) -> tuple[bool, str | None]:
    """Claim exclusive spawner ownership for a batch across workers."""
    bid = str(batch_id or "").strip()
    if not bid:
        return False, None
    with _lock:
        if _active_batch_runners.get(bid):
            return False, None
        _active_batch_runners[bid] = True
    token = f"{uuid.uuid4().hex}|{os.getpid()}|{_now():.0f}"
    if _reg_redis():
        try:
            from store.redis_client import set_nx_ex, worker_id

            token = f"{worker_id()}|{os.getpid()}|{uuid.uuid4().hex[:10]}"
            ok = set_nx_ex(_batch_runner_lock_key(bid), token, REG_BATCH_RUNNER_LOCK_TTL)
            if not ok:
                with _lock:
                    _active_batch_runners.pop(bid, None)
                return False, None
        except Exception:
            pass
    return True, token


def _renew_batch_runner(batch_id: str, token: str | None) -> None:
    if not token or not _reg_redis():
        return
    try:
        from store.redis_client import renew_if_owner

        renew_if_owner(_batch_runner_lock_key(batch_id), token, REG_BATCH_RUNNER_LOCK_TTL)
    except Exception:
        pass


def _release_batch_runner(batch_id: str, token: str | None) -> None:
    bid = str(batch_id or "").strip()
    with _lock:
        _active_batch_runners.pop(bid, None)
    if token and _reg_redis():
        try:
            from store.redis_client import compare_and_delete

            compare_and_delete(_batch_runner_lock_key(bid), token)
        except Exception:
            pass


def _snapshot_reg_config(
    *,
    captcha_provider: str,
    yescaptcha_key: str,
    proxy: str,
    moemail_api_key: str | None,
    moemail_base_url: str | None,
    prefix: str | None,
    domain: str | None,
    expiry_ms: int | None,
    concurrency: int,
    stagger_ms: int,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    post_registration: dict[str, Any] | None = None,
    auto_tune_enabled: bool = False,
    headless: bool = True,
) -> dict[str, Any]:
    """Config snapshot kept with the in-memory/Redis batch while it is running."""
    return {
        "captcha_provider": captcha_provider,
        "yescaptcha_key": yescaptcha_key if captcha_provider == "yescaptcha" else "",
        "proxy": proxy or "",
        "moemail_api_key": moemail_api_key or "",
        "moemail_base_url": moemail_base_url or "",
        "prefix": prefix or "",
        "domain": domain or "",
        "expiry_ms": expiry_ms,
        "concurrency": concurrency,
        "stagger_ms": stagger_ms,
        "local_solver_url": "http://127.0.0.1:5072",
        "mail_provider": (mail_provider or "moemail").strip().lower() or "moemail",
        "hotmail_local_base_url": hotmail_local_base_url or "http://127.0.0.1:17373",
        "post_registration": dict(post_registration or {}),
        "auto_tune_enabled": bool(auto_tune_enabled),
        "headless": bool(headless),
    }


class _RegCancelled(Exception):
    """Cooperative cancel for in-flight registration workers."""


class _RegPaused(_RegCancelled):
    """Cooperative interrupt for a batch that will later be resumed."""


_TERMINAL_STATUSES = frozenset(
    {
        "imported",
        "success",
        "completed",
        "error",
        "failed",
        "expired",
        "protocol_error",
        "protocol_blocked",
        "cancelled",
        "stopped",
        "paused",
    }
)

_REGISTRATION_STAGE_BY_STATUS = {
    "waiting_solver": "captcha",
    "solving_turnstile": "captcha",
    "waiting_email": "email",
    "registering": "account",
    "creating_account": "account",
    "fetching_sso": "auth",
    "importing": "auth",
}


def _is_cancel_status(status: str | None) -> bool:
    return str(status or "").lower() in ("cancelled", "stopped", "stopping")


def _session_cancel_requested(sess: dict[str, Any] | None) -> bool:
    if not isinstance(sess, dict):
        return False
    if sess.get("cancel_requested"):
        return True
    return _is_cancel_status(sess.get("status"))


def _session_pause_requested(sess: dict[str, Any] | None) -> bool:
    if not isinstance(sess, dict):
        return False
    if sess.get("pause_requested"):
        return True
    return str(sess.get("status") or "").lower() in ("pausing", "paused")


def _mirror_reg_sess(sid: str, sess: dict[str, Any] | None) -> None:
    _request_monitor_persist()
    if not _reg_redis() or not sid:
        return
    try:
        from store import sessions_redis

        if sess is None:
            sessions_redis.reg_sess_delete(sid)
        else:
            payload = {
                k: v
                for k, v in sess.items()
                if isinstance(k, str) and not k.startswith("_") and not callable(v)
            }
            sessions_redis.reg_sess_put(sid, payload)
    except Exception:
        pass


def _mirror_reg_batch(batch_id: str, batch: dict[str, Any] | None) -> None:
    _request_monitor_persist()
    if not _reg_redis() or not batch_id or batch is None:
        return
    try:
        from store import sessions_redis

        sessions_redis.reg_batch_put(batch_id, batch)
    except Exception:
        pass


def _record_register_task(
    *,
    task_id: str | None,
    summary: str,
    status: str,
    ok: bool | None = None,
    progress_done: int = 0,
    progress_total: int = 0,
    finished: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    """Best-effort write into the admin registration task log."""
    tid = str(task_id or "").strip()
    if not tid:
        return
    try:
        import task_log

        task_log.record(
            "register",
            task_id=tid,
            summary=str(summary or f"协议注册 {tid}")[:500],
            status=str(status or "done"),
            ok=ok,
            progress_done=int(progress_done or 0),
            progress_total=int(progress_total or 0),
            finished=bool(finished),
            detail=detail if isinstance(detail, dict) else {},
        )
    except Exception:
        pass


def _load_reg_sess(sid: str) -> dict[str, Any] | None:
    with _lock:
        local = _sessions.get(sid)
        if local is not None:
            return local
    if not _reg_redis():
        return None
    try:
        from store import sessions_redis

        remote = sessions_redis.reg_sess_get(sid)
        if remote:
            with _lock:
                _sessions.setdefault(sid, remote)
            return remote
    except Exception:
        pass
    return None


def _load_reg_batch(batch_id: str) -> dict[str, Any] | None:
    with _lock:
        local = _batches.get(batch_id)
        if local is not None:
            return local
    if not _reg_redis():
        return None
    try:
        from store import sessions_redis

        remote = sessions_redis.reg_batch_get(batch_id)
        if remote:
            with _lock:
                _batches.setdefault(batch_id, remote)
            return remote
    except Exception:
        pass
    return None


def _pipeline_group(sess: dict[str, Any]) -> str:
    return str(sess.get("batch_id") or f"session-{sess.get('id') or uuid.uuid4().hex}")


def _probe_group_slots(group: str, limit: int) -> threading.BoundedSemaphore:
    key = (str(group or "probe"), max(1, int(limit)))
    with _probe_slots_lock:
        slots = _probe_slots.get(key)
        if slots is None:
            slots = threading.BoundedSemaphore(key[1])
            _probe_slots[key] = slots
        return slots


def _append_session_event(
    sess: dict[str, Any], status: str, message: str, *, at: float | None = None
) -> None:
    events = [dict(item) for item in (sess.get("events") or []) if isinstance(item, dict)]
    event = {
        "at": float(at or _now()),
        "status": str(status or "unknown"),
        "message": str(message or ""),
    }
    if events and all(events[-1].get(key) == event[key] for key in ("status", "message")):
        return
    events.append(event)
    sess["events"] = events[-60:]
    _request_monitor_persist()


def _set_pipeline_session_state(
    sid: str, status: str, message: str, *, phase: str, position: int, limit: int
) -> None:
    with _lock:
        current = _sessions.get(sid) or _load_reg_sess(sid)
        if not current:
            return
        current = dict(current)
        queue_state = {
            "phase": phase,
            "position": max(0, int(position)),
            "concurrency": max(1, int(limit)),
        }
        if phase == "probe":
            probe = dict(current.get("probe") or {})
            probe.update(
                {
                    "state": "running" if status == "probing" else "queued",
                    "queued": status != "probing",
                    "position": queue_state["position"],
                    "concurrency": queue_state["concurrency"],
                }
            )
            current["probe"] = probe
            current["probe_queue"] = queue_state
        else:
            current["status"] = status
            current["message"] = message
            current["pipeline_queue"] = queue_state
        current["updated_at"] = _now()
        _append_session_event(current, status, message, at=current["updated_at"])
        _sessions[sid] = current
        _mirror_reg_sess(sid, current)


def _refresh_pipeline_positions(state: dict[str, Any]) -> None:
    phase = str(state.get("phase") or "probe")
    limit = int(state.get("limit") or 1)
    status = "import_queued" if phase == "import" else "probe_queued"
    label = "导入" if phase == "import" else "测活"
    for position, sid in enumerate(list(state.get("pending") or []), start=1):
        _set_pipeline_session_state(
            str(sid),
            status,
            f"已进入{label}缓存队列，当前排队第 {position} 位",
            phase=phase,
            position=position,
            limit=limit,
        )


def _close_pipeline_phase(group: str, phase: str) -> None:
    key = f"{group}:{phase}"
    with _pipeline_queue_lock:
        state = _pipeline_queues.get(key)
        if state is not None:
            state["producer_closed"] = True


def _finish_pipeline_without_import(sid: str, config: dict[str, Any]) -> None:
    sess = _load_reg_sess(sid) or {}
    target = str(config.get("target") or "sub2api").lower()
    auto_import = {
        "enabled": False,
        "target": target,
        "ok": None,
        "skipped": True,
    }
    with _lock:
        current = _sessions.get(sid) or dict(sess)
        current["auto_import"] = auto_import
        current["status"] = "imported"
        current["message"] = "本地账号已保存；自动导入未开启；自动测活独立执行"
        current["pipeline_queue"] = {
            "phase": "done",
            "position": 0,
            "concurrency": int(config.get("pipeline_concurrency") or 1),
        }
        current["updated_at"] = _now()
        _append_session_event(
            current, current["status"], current["message"], at=current["updated_at"]
        )
        _sessions[sid] = current
        _mirror_reg_sess(sid, current)


def _mark_pipeline_cancelled(sid: str) -> None:
    with _lock:
        current = _sessions.get(sid) or _load_reg_sess(sid)
        if not current:
            return
        current = dict(current)
        current["status"] = "cancelled"
        current["message"] = "队列任务已取消"
        current["error"] = "cancelled"
        current["cancel_requested"] = True
        current["updated_at"] = _now()
        _append_session_event(
            current, current["status"], current["message"], at=current["updated_at"]
        )
        _sessions[sid] = current
        _mirror_reg_sess(sid, current)


def _mark_pipeline_paused(sid: str) -> None:
    with _lock:
        current = _sessions.get(sid) or _load_reg_sess(sid)
        if not current:
            return
        current = dict(current)
        status = str(current.get("status") or "").lower()
        if status in _TERMINAL_STATUSES and status != "paused":
            return
        current["pause_requested"] = True
        current["cancel_requested"] = False
        current["status"] = "paused"
        current["message"] = "批次已暂停，后续导入或测活已中断"
        current["pipeline_queue"] = {
            "phase": "paused",
            "position": 0,
            "concurrency": 0,
        }
        current["updated_at"] = _now()
        _append_session_event(
            current, current["status"], current["message"], at=current["updated_at"]
        )
        _sessions[sid] = current
        _mirror_reg_sess(sid, current)


def _interrupt_pipeline_for_batch(group: str) -> None:
    """Stop queued import/probe work when a registration batch is paused."""
    pending: list[str] = []
    with _pipeline_queue_lock:
        for state in _pipeline_queues.values():
            if str(state.get("group") or "") != str(group or ""):
                continue
            pending.extend(str(sid) for sid in (state.get("pending") or []) if sid)
            state["pending"] = []
            state["producer_closed"] = True
    for sid in pending:
        _mark_pipeline_paused(sid)
