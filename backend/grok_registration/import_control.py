"""Grok registration import_control operations."""

from __future__ import annotations

from typing import Any

from .flow import RegistrationContext


def _retry_import_now(
    ctx: RegistrationContext,
    session_id: str,
    post_registration: dict[str, Any] | None = None,
    *,
    manual_retry: bool = True,
) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    sess = ctx._load_reg_sess(sid)
    if not sess:
        return {"ok": False, "error": "registration session not found"}
    if not manual_retry and ctx._session_pause_requested(sess):
        ctx._mark_pipeline_paused(sid)
        return {"ok": False, "paused": True, "error": "batch paused"}
    account_ids = [str(x) for x in sess.get("imported_account_ids") or [] if x]
    if not account_ids:
        return {"ok": False, "error": "该会话没有可导入的注册账号"}
    config = dict(sess.get("_post_registration") or {})
    config.update(dict(post_registration or {}))
    target = str(config.get("target") or "sub2api").lower()
    if bool(config.get("pre_import_probe_enabled", True)):
        probe = sess.get("probe") if isinstance(sess.get("probe"), dict) else {}
        probe_ok = int(probe.get("ok") or 0)
        probe_count = int(probe.get("count") or 0)
        if (
            probe_count <= 0
            or probe_ok < probe_count
            or int(probe.get("fail") or 0) > 0
            or int(probe.get("uncertain") or 0) > 0
        ):
            return {
                "ok": False,
                "session_id": sid,
                "error": "导入前测活尚未通过，请先完成测活",
            }
    with ctx._lock:
        current = ctx._sessions.get(sid) or dict(sess)
        current["status"] = "auto_importing"
        current["auto_import"] = {
            "enabled": True,
            "target": target,
            "ok": None,
            "skipped": False,
            "queued": False,
            "manual_retry": bool(manual_retry),
        }
        current["message"] = (
            f"正在手动重试导入 {('CPA' if target == 'cpa' else 'Sub2API')}"
            if manual_retry
            else f"正在执行队列导入 {('CPA' if target == 'cpa' else 'Sub2API')}"
        )
        current["updated_at"] = ctx._now()
        ctx._append_session_event(
            current, current["status"], current["message"], at=current["updated_at"]
        )
        ctx._sessions[sid] = current
        ctx._mirror_reg_sess(sid, current)
    results: list[dict[str, Any]] = []
    try:
        import model_health
        from account_pipeline import import_account

        for account_id in account_ids:
            try:
                record = model_health._load_record(account_id)
                if ctx._session_pause_requested(ctx._load_reg_sess(sid) or {}):
                    ctx._mark_pipeline_paused(sid)
                    return {"ok": False, "paused": True, "error": "batch paused"}
                response = import_account(record, config)
                results.append(
                    {
                        "ok": bool(response.get("ok")),
                        "status_code": response.get("status_code"),
                        "error": str(response.get("error") or "")[:220] or None,
                    }
                )
            except Exception as exc:
                results.append({"ok": False, "error": str(exc)[:220]})
    except Exception as exc:
        results.append({"ok": False, "error": str(exc)[:220]})
    ok_count = sum((1 for item in results if item.get("ok")))
    fail_count = sum((1 for item in results if not item.get("ok")))
    first_error = next(
        (item.get("error") for item in results if item.get("error")), None
    )
    auto_import = {
        "enabled": True,
        "target": target,
        "ok": fail_count == 0 and ok_count > 0,
        "skipped": False,
        "count": len(results),
        "imported": ok_count,
        "failed": fail_count,
        "error": first_error,
    }
    if manual_retry:
        auto_import["manual_retry"] = True
    with ctx._lock:
        current = ctx._sessions.get(sid) or dict(sess)
        current["auto_import"] = auto_import
        current["status"] = "imported"
        current["message"] = (
            f"手动导入完成：成功 {ok_count}，失败 {fail_count}"
            if manual_retry
            else f"队列导入完成：成功 {ok_count}，失败 {fail_count}"
        )
        current["pipeline_queue"] = {
            "phase": "done",
            "position": 0,
            "concurrency": int(config.get("pipeline_concurrency") or 1),
        }
        current["updated_at"] = ctx._now()
        ctx._append_session_event(
            current, current["status"], current["message"], at=current["updated_at"]
        )
        ctx._sessions[sid] = current
        ctx._mirror_reg_sess(sid, current)
    if auto_import["ok"]:
        try:
            from account_rotation import record_imported_session

            record_imported_session("xai", dict(current))
        except Exception:
            pass
    return {
        "ok": bool(auto_import["ok"]),
        "session_id": sid,
        "auto_import": auto_import,
    }


def retry_registration_import(
    ctx: RegistrationContext,
    session_id: str,
    post_registration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    sess = ctx._load_reg_sess(sid)
    if not sess:
        return {"ok": False, "error": "registration session not found"}
    status = str(sess.get("status") or "").lower()
    if status in {"auto_importing", "import_queued"}:
        return {
            "ok": True,
            "session_id": sid,
            "already_running": True,
            "queued": status == "import_queued",
        }
    config = dict(sess.get("_post_registration") or {})
    config.update(dict(post_registration or {}))
    if bool(config.get("pre_import_probe_enabled", True)):
        probe = sess.get("probe") if isinstance(sess.get("probe"), dict) else {}
        probe_count = int(probe.get("count") or 0)
        if (
            probe_count <= 0
            or int(probe.get("ok") or 0) < probe_count
            or int(probe.get("fail") or 0) > 0
            or int(probe.get("uncertain") or 0) > 0
        ):
            return {
                "ok": False,
                "session_id": sid,
                "error": "导入前测活尚未通过，请先完成测活",
            }
    ctx.threading.Thread(
        target=ctx._retry_import_now,
        args=(sid, post_registration),
        daemon=True,
        name=f"gba-retry-import-{sid[-8:]}",
    ).start()
    return {"ok": True, "session_id": sid, "scheduled": True}


def retry_registration_batch_import(
    ctx: RegistrationContext,
    batch_id: str,
    post_registration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bid = str(batch_id or "").strip()
    batch = ctx._load_reg_batch(bid)
    if not batch:
        return {"ok": False, "error": "registration batch not found"}
    candidates: list[str] = []
    for sid in batch.get("session_ids") or []:
        sess = ctx._load_reg_sess(str(sid)) or {}
        status = str(sess.get("status") or "").lower()
        imported = (
            sess.get("auto_import") if isinstance(sess.get("auto_import"), dict) else {}
        )
        if (
            status not in {"auto_importing", "import_queued"}
            and bool(sess.get("imported_account_ids") or [])
            and (imported.get("ok") is not True)
        ):
            candidates.append(str(sid))

    def run() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        config = dict(post_registration or {})
        limit = max(
            1,
            min(
                ctx.MAX_CONCURRENCY,
                int(
                    config.get("import_concurrency")
                    or config.get("pipeline_concurrency")
                    or 1
                ),
            ),
        )
        with ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="gba-manual-import"
        ) as pool:
            futures = [
                pool.submit(ctx._retry_import_now, sid, post_registration)
                for sid in candidates
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass

    if candidates:
        ctx.threading.Thread(
            target=run, daemon=True, name=f"gba-retry-import-{bid[-8:]}"
        ).start()
    return {"ok": True, "batch_id": bid, "scheduled": len(candidates)}
