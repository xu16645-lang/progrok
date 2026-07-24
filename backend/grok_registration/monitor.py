"""Read-side projections of registration sessions and batches."""
from __future__ import annotations

import re
from typing import Any

from . import state


def clean_old_sessions() -> None:
    """Keep registration history until the user explicitly clears it."""
    return


def compact_session(session: dict[str, Any]) -> dict[str, Any]:
    """Build a polling-safe session view without local helpers or secrets."""
    out = dict(session)
    for key in list(out):
        if isinstance(key, str) and key.startswith("_"):
            out.pop(key, None)
    out.pop("password", None)
    out.pop("yescaptcha_key", None)
    imported_ids = list(out.get("imported_account_ids") or [])
    imported_accounts = list(out.get("imported_accounts") or [])
    auth_json = out.get("auth_json")
    if isinstance(auth_json, dict):
        rows = [item for item in (auth_json.get("imported") or []) if isinstance(item, dict)]
        out["auth_json_count"] = len(rows)
        if not imported_ids:
            imported_ids = [str(item.get("id")) for item in rows if item.get("id")]
        if not imported_accounts:
            imported_accounts = [
                {"id": item.get("id"), "email": item.get("email")}
                for item in rows
                if item.get("id") or item.get("email")
            ]
    elif auth_json is not None:
        try:
            out["auth_json_count"] = len(auth_json)  # type: ignore[arg-type]
        except Exception:
            out["auth_json_count"] = 0
    if imported_ids:
        out["imported_account_ids"] = imported_ids
    if imported_accounts:
        out["imported_accounts"] = imported_accounts
    out.pop("auth_json", None)
    return out


def list_registration_sessions() -> dict[str, Any]:
    clean_old_sessions()
    if state._reg_redis():
        try:
            from store import sessions_redis

            for remote in sessions_redis.reg_sess_list():
                sid = str(remote.get("id") or "")
                if not sid:
                    continue
                with state._lock:
                    if sid not in state._sessions:
                        state._sessions[sid] = remote
                    else:
                        local = state._sessions[sid]
                        if float(remote.get("updated_at") or 0) >= float(
                            local.get("updated_at") or 0
                        ):
                            merged = {**local, **remote}
                            for key, value in local.items():
                                if (
                                    isinstance(key, str)
                                    and key.startswith("_")
                                    and key not in remote
                                ):
                                    merged[key] = value
                            state._sessions[sid] = merged
            for remote_batch in sessions_redis.reg_batch_list():
                bid = str(remote_batch.get("id") or remote_batch.get("batch_id") or "")
                if not bid:
                    continue
                with state._lock:
                    if bid not in state._batches:
                        state._batches[bid] = remote_batch
                    else:
                        local_batch = state._batches[bid]
                        if float(remote_batch.get("updated_at") or 0) >= float(
                            local_batch.get("updated_at") or 0
                        ):
                            ids = list(local_batch.get("session_ids") or [])
                            for session_id in remote_batch.get("session_ids") or []:
                                if session_id not in ids:
                                    ids.append(session_id)
                            state._batches[bid] = {
                                **local_batch,
                                **remote_batch,
                                "session_ids": ids,
                            }
        except Exception:
            pass
    with state._lock:
        sessions = [compact_session(session) for session in state._sessions.values()]
        sessions.sort(
            key=lambda session: float(
                session.get("updated_at") or session.get("created_at") or 0
            ),
            reverse=True,
        )
        batches = []
        for batch in state._batches.values():
            session_ids = list(batch.get("session_ids") or [])
            stats = batch_stats(session_ids, batch=batch)
            if session_ids and stats.get("running") == 0 and stats.get("cancelled", 0) > 0:
                if (
                    stats.get("imported", 0) == 0
                    and stats.get("error", 0) == 0
                    and stats.get("missing", 0) == 0
                ):
                    stats["batch_status"] = "cancelled"
            item = {**batch, **stats}
            item.pop("reg_config", None)
            batch_status = str(stats.get("batch_status") or "").lower()
            current_status = str(batch.get("status") or "").lower()
            if batch_status and (
                current_status in ("", "running", "starting")
                or (
                    batch_status in ("done", "partial", "error", "cancelled", "stopped")
                    and stats.get("running", 0) == 0
                )
            ):
                if current_status != "stopping" or stats.get("running", 0) == 0:
                    item["status"] = (
                        batch_status
                        if batch_status != "running" or current_status != "stopping"
                        else current_status
                    )
            batches.append(item)
        batches.sort(
            key=lambda batch: float(
                batch.get("updated_at") or batch.get("created_at") or 0
            ),
            reverse=True,
        )
    state._persist_monitor_state()
    return {
        "sessions": sessions,
        "batches": batches,
        "active": sum(
            1
            for session in sessions
            if str(session.get("status") or "").lower() not in state._TERMINAL_STATUSES
        ),
    }


def get_registration_session(
    session_id: str,
    *,
    include_auth_json: bool = False,
) -> dict[str, Any] | None:
    session = state._load_reg_sess(session_id)
    if not session:
        return None
    out = dict(session)
    for key in list(out):
        if isinstance(key, str) and key.startswith("_"):
            out.pop(key, None)
    out.pop("password", None)
    out.pop("yescaptcha_key", None)
    if not include_auth_json:
        out.pop("auth_json", None)
    return out


def batch_stats(
    session_ids: list[str],
    *,
    batch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute live batch counters without treating missing sessions as active."""
    imported = error = running = cancelled = missing = 0
    for session_id in session_ids:
        session = state._load_reg_sess(session_id)
        if not session:
            missing += 1
            continue
        status = str(session.get("status") or "").lower()
        if status in ("imported", "success", "completed"):
            imported += 1
        elif status in ("cancelled", "stopped"):
            cancelled += 1
        elif status in ("error", "failed", "expired", "protocol_error", "protocol_blocked"):
            error += 1
        else:
            running += 1

    total = len(session_ids)
    observed = imported + error + cancelled + running
    done = imported + error + cancelled
    target = 0
    if isinstance(batch, dict):
        try:
            target = int(batch.get("count") or 0)
        except Exception:
            target = 0
    if target <= 0:
        target = total

    status = "running"
    if observed == 0:
        stored = ""
        if isinstance(batch, dict):
            stored = str(batch.get("batch_status") or batch.get("status") or "").lower()
        if stored in ("done", "partial", "error", "cancelled", "stopped"):
            status = stored
            try:
                imported = int(batch.get("imported") or imported)
            except Exception:
                pass
            try:
                error = int(batch.get("error") or error)
            except Exception:
                pass
            try:
                cancelled = int(batch.get("cancelled") or cancelled)
            except Exception:
                pass
            try:
                done = int(batch.get("done") or (imported + error + cancelled))
            except Exception:
                done = imported + error + cancelled
            running = 0
        elif total and missing >= total:
            message = str((batch or {}).get("message") or "")
            if isinstance(batch, dict):
                try:
                    imported = int(batch.get("imported") or imported or 0)
                except Exception:
                    pass
                try:
                    error = int(batch.get("error") or error or 0)
                except Exception:
                    pass
                try:
                    cancelled = int(batch.get("cancelled") or cancelled or 0)
                except Exception:
                    pass
            if imported == 0 and error == 0 and cancelled == 0 and message:
                ok_match = re.search(r"ok\s*=\s*(\d+)", message)
                fail_match = re.search(r"fail\s*=\s*(\d+)", message)
                if ok_match:
                    try:
                        imported = int(ok_match.group(1))
                    except Exception:
                        pass
                if fail_match:
                    try:
                        error = int(fail_match.group(1))
                    except Exception:
                        pass
            done = imported + error + cancelled
            if cancelled and not imported and not error:
                status = "cancelled"
            elif imported and not error and not cancelled:
                status = "done"
            elif imported:
                status = "partial"
            elif error:
                status = "error"
            elif stored in ("stopping",):
                status = "stopped"
            else:
                status = "done"
            running = 0
        else:
            status = "running"
    elif done >= max(target, total) and running == 0:
        if cancelled and not imported and not error:
            status = "cancelled"
        elif error == 0 and cancelled == 0:
            status = "done"
        elif imported:
            status = "partial"
        else:
            status = "error"
    elif running == 0 and missing > 0 and done > 0 and observed < total:
        if imported and (error or cancelled or missing):
            status = "partial"
        elif imported and not error and not cancelled:
            status = "done"
        elif cancelled and not imported and not error:
            status = "cancelled"
        elif error and not imported:
            status = "error"
        else:
            status = "partial"
    elif total and (imported or error or cancelled) and running:
        status = "running"
    elif running:
        status = "running"

    if isinstance(batch, dict):
        batch_status = str(batch.get("status") or "").lower()
        if batch_status in ("pausing", "paused"):
            status = "pausing" if running else "paused"
        elif batch_status in ("stopping", "cancelled", "stopped") and running == 0:
            if status == "running":
                status = "cancelled" if cancelled or batch_status != "stopping" else "stopped"
        if batch_status == "stopping" and running:
            status = "running"

    return {
        "total": max(total, target),
        "imported": imported,
        "error": error,
        "cancelled": cancelled,
        "running": running,
        "missing": missing,
        "done": done,
        "batch_status": status,
    }


def get_registration_batch(batch_id: str) -> dict[str, Any] | None:
    batch = state._load_reg_batch(batch_id)
    if not batch:
        return None
    session_ids = list(batch.get("session_ids") or [])
    stats = batch_stats(session_ids, batch=batch)
    sessions = []
    for session_id in session_ids[-120:]:
        session = state._load_reg_sess(session_id)
        if session:
            sessions.append(compact_session(session))
    try:
        sessions.sort(
            key=lambda session: float(
                session.get("updated_at") or session.get("created_at") or 0
            ),
            reverse=True,
        )
    except Exception:
        pass
    out = {**batch, **stats, "sessions": sessions}
    out.pop("reg_config", None)
    if stats.get("batch_status"):
        if str(batch.get("status") or "").lower() != "stopping" or stats.get("running", 0) == 0:
            if stats.get("running", 0) == 0 or str(batch.get("status") or "").lower() in (
                "",
                "running",
                "starting",
            ):
                out["status"] = stats["batch_status"]
    return out
