"""Grok registration management operations."""

from __future__ import annotations

from typing import Any

from .flow import RegistrationContext


def reclaim_orphaned_registration_batches(
    ctx: RegistrationContext,
    *,
    stale_sec: float | None = None,
    auto_resume: bool = True,
    max_batches: int | None = None,
) -> dict[str, Any]:
    """On startup: reclaim orphan sessions and optionally resume open batches."""
    try:
        ttl = float(
            stale_sec
            if stale_sec is not None
            else ctx.os.environ.get("GROK2API_REG_STALE_SEC", "120") or 120
        )
    except (TypeError, ValueError):
        ttl = 120.0
    if max_batches is None:
        try:
            max_batches = int(
                ctx.os.environ.get("GROK2API_REG_AUTO_RESUME_MAX", "1") or 1
            )
        except (TypeError, ValueError):
            max_batches = 1
    max_batches = max(0, min(10, int(max_batches)))
    sess_result = ctx.reclaim_orphaned_registration_sessions(stale_sec=ttl)
    batches: list[dict[str, Any]] = []
    if ctx._reg_redis():
        try:
            from store import sessions_redis

            listed = sessions_redis.reg_batch_list() or []
            if isinstance(listed, list):
                batches = [b for b in listed if isinstance(b, dict)]
        except Exception:
            batches = []
    if not batches:
        with ctx._lock:
            batches = [dict(v) for v in ctx._batches.values() if isinstance(v, dict)]
    resumed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    batches = sorted(
        batches,
        key=lambda b: float(b.get("updated_at") or b.get("created_at") or 0),
        reverse=True,
    )
    for b in batches:
        if len(resumed) >= max(0, int(max_batches)):
            break
        bid = str(b.get("id") or "").strip()
        if not bid:
            continue
        st = str(b.get("status") or "").strip().lower()
        if st not in {"running", "starting", "stopping", "partial"} and (
            not (
                st == "error" and int(b.get("finished") or 0) < int(b.get("count") or 0)
            )
        ):
            continue
        if b.get("cancel_requested") and st in {"stopping", "cancelled", "stopped"}:
            skipped.append(
                {"batch_id": bid, "reason": "cancel_requested", "status": st}
            )
            continue
        has_local = False
        with ctx._lock:
            has_local = bool(ctx._active_batch_runners.get(bid))
        if has_local:
            skipped.append(
                {"batch_id": bid, "reason": "local_runner_alive", "status": st}
            )
            continue
        if ctx._reg_redis():
            try:
                from store.redis_client import get_str as redis_get

                token = redis_get(ctx._batch_runner_lock_key(bid))
                if token:
                    try:
                        from store.redis_client import compare_and_delete

                        compare_and_delete(ctx._batch_runner_lock_key(bid), str(token))
                    except Exception:
                        try:
                            from store.redis_client import set_ex

                            set_ex(ctx._batch_runner_lock_key(bid), "reclaimed", 1)
                        except Exception:
                            pass
            except Exception:
                pass
        count = int(b.get("count") or 0)
        finished = int(b.get("finished") or 0)
        if count > 0 and finished >= count:
            skipped.append(
                {"batch_id": bid, "reason": "already_finished", "status": st}
            )
            continue
        if not auto_resume:
            skipped.append(
                {"batch_id": bid, "reason": "auto_resume_disabled", "status": st}
            )
            continue
        r = ctx.resume_registration_batch(bid, force=True, reclaim_stale_sec=ttl)
        resumed.append(r)
    return {
        "ok": True,
        "sessions_reclaimed": sess_result.get("reclaimed") or 0,
        "session_reclaim": sess_result,
        "batches_resumed": sum((1 for r in resumed if r.get("ok"))),
        "resumed": resumed,
        "skipped": skipped[:20],
    }


def stop_registration_session(
    ctx: RegistrationContext, session_id: str
) -> dict[str, Any]:
    """Request cooperative cancel for one registration session."""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "missing session id"}
    sess = ctx._load_reg_sess(sid)
    if not sess:
        return {"ok": False, "error": "registration session not found"}
    st = str(sess.get("status") or "").lower()
    if st in ctx._TERMINAL_STATUSES:
        return {
            "ok": True,
            "id": sid,
            "status": st,
            "already_terminal": True,
            "message": sess.get("message") or st,
        }
    with ctx._lock:
        cur = ctx._sessions.get(sid) or dict(sess)
        cur["cancel_requested"] = True
        cur["status"] = "stopping"
        cur["message"] = "stop requested; waiting for worker to exit"
        cur["updated_at"] = ctx._now()
        ctx._sessions[sid] = cur
        ctx._mirror_reg_sess(sid, cur)
        out = ctx._compact_session(cur)
    return {"ok": True, "id": sid, **out}


def stop_registration_batch(ctx: RegistrationContext, batch_id: str) -> dict[str, Any]:
    """Request cooperative cancel for every non-terminal session in a batch."""
    bid = str(batch_id or "").strip()
    if not bid:
        return {"ok": False, "error": "missing batch id"}
    batch = ctx._load_reg_batch(bid)
    if not batch:
        return {"ok": False, "error": "registration batch not found"}
    with ctx._lock:
        b = ctx._batches.get(bid) or dict(batch)
        b["cancel_requested"] = True
        if str(b.get("status") or "").lower() not in (
            "done",
            "partial",
            "error",
            "cancelled",
            "stopped",
        ):
            b["status"] = "stopping"
        b["message"] = "stop requested; signalling sessions"
        b["updated_at"] = ctx._now()
        ctx._batches[bid] = b
        ctx._mirror_reg_batch(bid, dict(b))
        sids = list(b.get("session_ids") or [])
    stopped: list[str] = []
    already: list[str] = []
    missing: list[str] = []
    for sid in sids:
        r = ctx.stop_registration_session(str(sid))
        if not r.get("ok"):
            missing.append(str(sid))
            continue
        if r.get("already_terminal"):
            already.append(str(sid))
        else:
            stopped.append(str(sid))
    with ctx._lock:
        b = ctx._batches.get(bid) or dict(batch)
        b["cancel_requested"] = True
        if str(b.get("status") or "").lower() not in (
            "done",
            "partial",
            "error",
            "cancelled",
            "stopped",
        ):
            b["status"] = "stopping"
        b["message"] = (
            f"stop requested: stopping={len(stopped)} already_done={len(already)} missing={len(missing)}"
        )
        b["updated_at"] = ctx._now()
        ctx._batches[bid] = b
        ctx._mirror_reg_batch(bid, dict(b))
        out = dict(b)
    return {
        "ok": True,
        "batch_id": bid,
        "stopped": stopped,
        "already_terminal": already,
        "missing": missing,
        "message": out.get("message") or "stop requested",
        "batch": out,
    }


def stop_all_active_registrations(ctx: RegistrationContext) -> dict[str, Any]:
    """Stop every non-terminal registration session currently visible."""
    listed = ctx.list_registration_sessions()
    sessions = list(listed.get("sessions") or [])
    stopped = []
    already = []
    for s in sessions:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        r = ctx.stop_registration_session(sid)
        if r.get("already_terminal"):
            already.append(sid)
        elif r.get("ok"):
            stopped.append(sid)
    for b in list(listed.get("batches") or []):
        bid = str(b.get("id") or b.get("batch_id") or "")
        if not bid:
            continue
        st = str(b.get("status") or b.get("batch_status") or "").lower()
        if st in ("done", "partial", "error", "cancelled", "stopped"):
            continue
        try:
            ctx.stop_registration_batch(bid)
        except Exception:
            pass
    return {
        "ok": True,
        "stopped": stopped,
        "already_terminal": already,
        "stopped_count": len(stopped),
        "already_count": len(already),
    }


def list_registration_sessions(ctx: RegistrationContext) -> dict[str, Any]:
    return ctx._monitor.list_registration_sessions()


def reset_registration_monitor(ctx: RegistrationContext) -> dict[str, Any]:
    """Clear the current monitor round without deleting saved account files."""
    with ctx._lock:
        active_sessions = [
            sid
            for sid, session in ctx._sessions.items()
            if str(session.get("status") or "").lower() not in ctx._TERMINAL_STATUSES
        ]
        active_batches = [
            bid
            for bid, batch in ctx._batches.items()
            if str(batch.get("status") or "").lower()
            not in {"paused", "done", "partial", "error", "cancelled", "stopped"}
        ]
        active_runners = [
            bid for bid, running in ctx._active_batch_runners.items() if running
        ]
        if active_sessions or active_batches or active_runners:
            return {
                "ok": False,
                "error": "仍有任务正在运行，请先暂停或停止后再清除本轮任务",
            }
        session_count = len(ctx._sessions)
        batch_count = len(ctx._batches)
        ctx._sessions.clear()
        ctx._batches.clear()
        ctx._active_batch_runners.clear()
    with ctx._pipeline_queue_lock:
        ctx._pipeline_queues.clear()
    ctx.clear_state("grok")
    return {
        "ok": True,
        "sessions_cleared": session_count,
        "batches_cleared": batch_count,
    }


def get_registration_session(
    ctx: RegistrationContext, sid: str, *, include_auth_json: bool = False
) -> dict[str, Any] | None:
    return ctx._monitor.get_registration_session(
        sid, include_auth_json=include_auth_json
    )


def _batch_stats(
    ctx: RegistrationContext,
    session_ids: list[str],
    *,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ctx._monitor.batch_stats(session_ids, batch=batch)


def get_registration_batch(
    ctx: RegistrationContext, batch_id: str
) -> dict[str, Any] | None:
    return ctx._monitor.get_registration_batch(batch_id)
