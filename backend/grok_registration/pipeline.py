"""Asynchronous probe and import queue orchestration."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import state


def run_probe_wave(
    group: str,
    session_ids: list[str],
    limit: int,
    *,
    retry_probe: Callable[..., dict[str, Any]],
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    for sid in session_ids:
        state._set_pipeline_session_state(
            sid,
            "probing",
            f"本批 {len(session_ids)} 个账号正在测活，按错峰时间依次发起",
            phase="probe",
            position=0,
            limit=limit,
        )

    slots = state._probe_group_slots(group, limit)

    def run_one(sid: str) -> dict[str, Any]:
        with slots:
            session = state._load_reg_sess(sid) or {}
            if state._session_pause_requested(session):
                state._mark_pipeline_paused(sid)
                return {"ok": False, "session_id": sid, "paused": True}
            if state._session_cancel_requested(session):
                state._mark_pipeline_cancelled(sid)
                return {"ok": False, "session_id": sid, "cancelled": True}
            config = dict(session.get("_post_registration") or {})
            return retry_probe(sid, config, manual_retry=False)

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="gba-probe-wave") as pool:
        futures = {pool.submit(run_one, sid): sid for sid in session_ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                with state._lock:
                    current = state._sessions.get(sid) or {}
                    current["probe"] = {
                        "count": 1,
                        "ok": 0,
                        "fail": 1,
                        "state": "complete",
                        "queued": False,
                        "position": 0,
                        "results": [
                            {"account_id": None, "ok": False, "error": str(exc)[:180]}
                        ],
                    }
                    current["updated_at"] = state._now()
                    state._append_session_event(
                        current,
                        "probe_complete",
                        f"队列测活异常结束：{str(exc)[:180]}",
                        at=current["updated_at"],
                    )
                    state._sessions[sid] = current
                    state._mirror_reg_sess(sid, current)


def run_import_wave(
    group: str,
    session_ids: list[str],
    limit: int,
    *,
    retry_import: Callable[..., dict[str, Any]],
) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    for sid in session_ids:
        state._set_pipeline_session_state(
            sid,
            "auto_importing",
            f"本批 {len(session_ids)} 个账号正在导入，按错峰时间依次发起",
            phase="import",
            position=0,
            limit=limit,
        )

    def run_one(sid: str) -> dict[str, Any]:
        session = state._load_reg_sess(sid) or {}
        if state._session_pause_requested(session):
            state._mark_pipeline_paused(sid)
            return {"ok": False, "session_id": sid, "paused": True}
        if state._session_cancel_requested(session):
            state._mark_pipeline_cancelled(sid)
            return {"ok": False, "session_id": sid, "cancelled": True}
        config = dict(session.get("_post_registration") or {})
        return retry_import(sid, config, manual_retry=False)

    with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="gba-import-wave") as pool:
        futures = {pool.submit(run_one, sid): sid for sid in session_ids}
        for future in as_completed(futures):
            sid = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                session = state._load_reg_sess(sid) or {}
                config = dict(session.get("_post_registration") or {})
                target = str(config.get("target") or "sub2api").lower()
                with state._lock:
                    current = state._sessions.get(sid) or dict(session)
                    current["auto_import"] = {
                        "enabled": True,
                        "target": target,
                        "ok": False,
                        "skipped": False,
                        "count": 1,
                        "imported": 0,
                        "failed": 1,
                        "error": str(exc)[:220],
                    }
                    current["status"] = "imported"
                    current["message"] = "队列导入异常结束：成功 0，失败 1"
                    current["pipeline_queue"] = {
                        "phase": "done",
                        "position": 0,
                        "concurrency": int(config.get("pipeline_concurrency") or limit),
                    }
                    current["updated_at"] = state._now()
                    state._sessions[sid] = current
                    state._mirror_reg_sess(sid, current)


def continuous_import_dispatch_loop(
    key: str,
    *,
    max_concurrency: int,
    run_import_wave: Callable[[str, list[str], int], None],
) -> None:
    """Start imports as soon as individual pipeline slots are available."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    active: dict[Any, str] = {}
    with ThreadPoolExecutor(
        max_workers=max_concurrency,
        thread_name_prefix="gba-import-queue",
    ) as pool:
        while True:
            jobs: list[tuple[str, str, int]] = []
            with state._pipeline_queue_lock:
                queue_state = state._pipeline_queues.get(key)
                if queue_state is None:
                    return
                group = str(queue_state.get("group") or "")
                limit = max(
                    1,
                    min(max_concurrency, int(queue_state.get("limit") or 1)),
                )
                pending = queue_state.get("pending") or []
                closed = bool(queue_state.get("producer_closed"))
                while pending and len(active) + len(jobs) < limit:
                    jobs.append((str(pending.pop(0)), group, limit))
                queue_state["in_wave"] = bool(active or jobs)
                state._refresh_pipeline_positions(queue_state)
                if not active and not jobs and closed:
                    state._pipeline_queues.pop(key, None)
                    return

            for sid, group, limit in jobs:
                active[pool.submit(run_import_wave, group, [sid], limit)] = sid

            if not active:
                time.sleep(0.05)
                continue

            done, _ = wait(set(active), timeout=0.1, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                sid = active.pop(future)
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[registration] import queue worker failed sid={sid}: {exc}")
            with state._pipeline_queue_lock:
                queue_state = state._pipeline_queues.get(key)
                if queue_state is not None:
                    queue_state["in_wave"] = bool(active)


def pipeline_dispatch_loop(
    key: str,
    *,
    run_probe_wave: Callable[[str, list[str], int], None],
    run_import_wave: Callable[[str, list[str], int], None],
    continuous_import_dispatch: Callable[[str], None],
) -> None:
    with state._pipeline_queue_lock:
        queue_state = state._pipeline_queues.get(key)
        if queue_state is None:
            return
        is_import = str(queue_state.get("phase") or "probe") == "import"
    if is_import:
        continuous_import_dispatch(key)
        return

    while True:
        wave: list[str] = []
        phase = "probe"
        group = ""
        limit = 1
        with state._pipeline_queue_lock:
            queue_state = state._pipeline_queues.get(key)
            if queue_state is None:
                return
            pending = queue_state.get("pending") or []
            phase = str(queue_state.get("phase") or "probe")
            group = str(queue_state.get("group") or "")
            limit = max(1, int(queue_state.get("limit") or 1))
            closed = bool(queue_state.get("producer_closed"))
            if pending and (len(pending) >= limit or closed):
                wave = [str(pending.pop(0)) for _ in range(min(limit, len(pending)))]
                queue_state["in_wave"] = True
                state._refresh_pipeline_positions(queue_state)
            elif not pending and closed:
                state._pipeline_queues.pop(key, None)
                return
            else:
                queue_state["in_wave"] = False
        if not wave:
            time.sleep(0.2)
            continue
        if phase == "import":
            run_import_wave(group, wave, limit)
        else:
            run_probe_wave(group, wave, limit)
        with state._pipeline_queue_lock:
            queue_state = state._pipeline_queues.get(key)
            if queue_state is not None:
                queue_state["in_wave"] = False


def enqueue_pipeline_phase(
    sid: str,
    phase: str = "probe",
    *,
    max_concurrency: int,
    dispatch_loop: Callable[[str], None],
) -> None:
    session = state._load_reg_sess(sid) or {}
    if not session:
        return
    name = "import" if str(phase).lower() == "import" else "probe"
    if name == "import":
        status = str(session.get("status") or "").lower()
        if status in {
            "error",
            "failed",
            "protocol_error",
            "protocol_blocked",
            "cancelled",
            "stopped",
        }:
            return
        if not (session.get("imported_account_ids") or []):
            return
    config = dict(session.get("_post_registration") or {})
    group = state._pipeline_group(session)
    concurrency_key = "import_concurrency" if name == "import" else "probe_concurrency"
    limit = max(
        1,
        min(
            max_concurrency,
            int(
                config.get(concurrency_key)
                or config.get("pipeline_concurrency")
                or (state._load_reg_batch(group) or {}).get("concurrency")
                or 1
            ),
        ),
    )
    key = f"{group}:{name}"
    start_thread = False
    with state._pipeline_queue_lock:
        queue_state = state._pipeline_queues.get(key)
        if queue_state is None:
            queue_state = {
                "group": group,
                "phase": name,
                "limit": limit,
                "pending": [],
                "producer_closed": not bool(session.get("batch_id")),
                "in_wave": False,
            }
            state._pipeline_queues[key] = queue_state
            start_thread = True
        queue_state["limit"] = limit
        if sid not in queue_state["pending"]:
            queue_state["pending"].append(sid)
        state._refresh_pipeline_positions(queue_state)
    if name == "import":
        target = str(config.get("target") or "sub2api").lower()
        with state._lock:
            current = state._sessions.get(sid) or dict(session)
            current["auto_import"] = {
                "enabled": True,
                "target": target,
                "ok": None,
                "skipped": False,
                "queued": True,
            }
            current["updated_at"] = state._now()
            state._sessions[sid] = current
            state._mirror_reg_sess(sid, current)
    if start_thread:
        threading.Thread(
            target=dispatch_loop,
            args=(key,),
            daemon=True,
            name=f"gba-{name}-queue-{group[-8:]}",
        ).start()
