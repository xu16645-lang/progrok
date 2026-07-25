"""Grok registration probe_control operations."""

from __future__ import annotations

from typing import Any

from .flow import RegistrationContext


def _schedule_probe_recheck(
    ctx: RegistrationContext,
    sid: str,
    config: dict[str, Any],
    *,
    current_attempt: int,
    next_attempt: int,
    delay: float,
) -> None:

    def run() -> None:
        sess = ctx._load_reg_sess(sid) or {}
        probe = sess.get("probe") if isinstance(sess.get("probe"), dict) else {}
        if (
            str(probe.get("state") or "").lower() != "retry_pending"
            or int(probe.get("recheck_attempt") or 0) != current_attempt
            or ctx._session_cancel_requested(sess)
        ):
            return
        group = ctx._pipeline_group(sess)
        limit = max(
            1,
            min(
                ctx.MAX_CONCURRENCY,
                int(
                    config.get("probe_concurrency")
                    or config.get("pipeline_concurrency")
                    or 1
                ),
            ),
        )
        with ctx._probe_group_slots(group, limit):
            ctx._retry_probe_now(
                sid, config, manual_retry=False, recheck_attempt=next_attempt
            )

    timer = ctx.threading.Timer(max(1.0, float(delay)), run)
    timer.daemon = True
    timer.name = f"gba-probe-recheck-{sid[-8:]}-{next_attempt}"
    timer.start()


def _retry_probe_now(
    ctx: RegistrationContext,
    session_id: str,
    post_registration: dict[str, Any] | None = None,
    *,
    manual_retry: bool = True,
    recheck_attempt: int = 0,
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
        return {"ok": False, "error": "该会话没有可测活的注册账号"}
    with ctx._lock:
        current = ctx._sessions.get(sid) or dict(sess)
        probe = dict(current.get("probe") or {})
        probe.update(
            {
                "state": "running",
                "queued": False,
                "position": 0,
                "recheck_attempt": max(0, int(recheck_attempt)),
            }
        )
        probe.pop("next_recheck_at", None)
        current["probe"] = probe
        message = "正在手动重试测活" if manual_retry else "正在执行队列测活"
        current["updated_at"] = ctx._now()
        if manual_retry:
            ctx._append_session_event(
                current, "probing", message, at=current["updated_at"]
            )
        ctx._sessions[sid] = current
        ctx._mirror_reg_sess(sid, current)
    results: list[dict[str, Any]] = []
    config = dict(sess.get("_post_registration") or {})
    config.update(dict(post_registration or {}))

    def append_probe_event(status: str, message: str) -> None:
        with ctx._lock:
            current = ctx._sessions.get(sid) or ctx._load_reg_sess(sid) or dict(sess)
            current["updated_at"] = ctx._now()
            ctx._append_session_event(
                current, status, message, at=current["updated_at"]
            )
            ctx._sessions[sid] = current
            ctx._mirror_reg_sess(sid, current)

    try:
        import account_rotation

        model = str(config.get("model") or "grok-4.5")
        for account_id in account_ids:
            try:
                if ctx._session_pause_requested(ctx._load_reg_sess(sid) or {}):
                    ctx._mark_pipeline_paused(sid)
                    return {"ok": False, "paused": True, "error": "batch paused"}
                append_probe_event(
                    "probe_request",
                    f"测活上游请求：POST /responses，模型 {model}，输入 hi",
                )
                response = account_rotation.probe_registration_account(
                    "xai",
                    email=str(sess.get("email") or ""),
                    account_id=account_id,
                    session_id=sid,
                    session_file=str(sess.get("session_file") or ""),
                    config=config,
                )
                detail = response if isinstance(response, dict) else {}
                error = detail.get("error") or detail.get("message") or ""
                item = {
                    "account_id": account_id,
                    "ok": bool(detail.get("ok") or detail.get("available") is True),
                    "available": detail.get("available"),
                    "model": detail.get("model") or model,
                    "status_code": detail.get("status_code"),
                    "latency_ms": detail.get("latency_ms"),
                    "retry_count": int(detail.get("retry_count") or 0),
                    "retryable": bool(detail.get("retryable")),
                    "classification": detail.get("classification"),
                    "fallback": detail.get("fallback"),
                    "degraded": bool(detail.get("degraded")),
                    "chat_ready": detail.get("chat_ready"),
                    "message": detail.get("message"),
                    "reply": str(detail.get("reply") or "")[:180] or None,
                    "error": str(error)[:180] if error else None,
                }
                results.append(item)
                status_text = f"HTTP {item['status_code']}" if item.get("status_code") else "无 HTTP 状态"
                latency_text = f"，耗时 {item['latency_ms']}ms" if item.get("latency_ms") is not None else ""
                retry_text = f"，权限传播重试 {item['retry_count']} 次" if item.get("retry_count") else ""
                reply_text = item.get("reply") or item.get("message") or item.get("error") or "上游未返回内容"
                append_probe_event(
                    "probe_response" if item["ok"] else "probe_response_failed",
                    f"测活上游回复：{status_text}{latency_text}{retry_text}，{str(reply_text)[:180]}",
                )
            except Exception as exc:
                error = str(exc)[:180]
                results.append({"account_id": account_id, "ok": False, "error": error})
                append_probe_event("probe_response_failed", f"测活上游回复：请求异常，{error}")
    except Exception as exc:
        results.append({"account_id": None, "ok": False, "error": str(exc)[:180]})
    uncertain = sum(
        (1 for item in results if item.get("retryable") and (not item.get("ok")))
    )
    failed = sum(
        (1 for item in results if not item.get("ok") and (not item.get("retryable")))
    )
    degraded = sum((1 for item in results if item.get("ok") and item.get("degraded")))
    summary = {
        "count": len(results),
        "ok": sum((1 for item in results if item.get("ok"))),
        "fail": failed,
        "uncertain": uncertain,
        "degraded": degraded,
        "results": results,
        "state": "complete",
        "queued": False,
        "position": 0,
        "recheck_attempt": max(0, int(recheck_attempt)),
    }
    recheck_delay = None
    if uncertain:
        if recheck_attempt < len(ctx.PROBE_RECHECK_DELAYS):
            recheck_delay = ctx.PROBE_RECHECK_DELAYS[recheck_attempt]
            summary["state"] = "retry_pending"
            summary["next_recheck_at"] = ctx._now() + recheck_delay
        else:
            summary["state"] = "uncertain"
    if manual_retry:
        summary["manual_retry"] = True
    with ctx._lock:
        current = ctx._sessions.get(sid) or dict(sess)
        current["probe"] = summary
        if summary["state"] == "retry_pending":
            message = f"测活暂未确认：通过 {summary['ok']}，待复检 {summary['uncertain']}，将在 {int(recheck_delay or 0)} 秒后自动复检"
            event_status = "probe_retry_pending"
        elif summary["state"] == "uncertain":
            message = (
                f"测活检测异常：通过 {summary['ok']}，无法确认 {summary['uncertain']}"
            )
            event_status = "probe_uncertain"
        else:
            degraded_text = (
                f"，其中轻量验证 {summary['degraded']}" if summary["degraded"] else ""
            )
            message = (
                f"手动测活完成：通过 {summary['ok']}，失败 {summary['fail']}{degraded_text}"
                if manual_retry
                else f"队列测活完成：通过 {summary['ok']}，失败 {summary['fail']}{degraded_text}"
            )
            event_status = "probe_complete"
        current["updated_at"] = ctx._now()
        ctx._append_session_event(
            current, event_status, message, at=current["updated_at"]
        )
        ctx._sessions[sid] = current
        ctx._mirror_reg_sess(sid, current)
    pre_import_probe = bool(config.get("pre_import_probe_enabled", True))
    auto_import = bool(config.get("auto_import_enabled"))
    probe_succeeded = summary["fail"] == 0 and summary["uncertain"] == 0
    if pre_import_probe and auto_import and summary["state"] not in {
        "retry_pending",
        "uncertain",
    }:
        if probe_succeeded:
            with ctx._lock:
                current = ctx._sessions.get(sid) or dict(sess)
                current["status"] = "converted"
                current["error"] = None
                pending_import = dict(current.get("auto_import") or {})
                pending_import["waiting_for_probe"] = False
                pending_import["queued"] = True
                current["auto_import"] = pending_import
                current["updated_at"] = ctx._now()
                ctx._sessions[sid] = current
                ctx._mirror_reg_sess(sid, current)
            ctx._enqueue_pipeline_phase(sid, "import")
        else:
            error = next(
                (
                    str(item.get("error") or item.get("message") or "").strip()
                    for item in results
                    if not item.get("ok")
                ),
                "账号请求上游失败",
            )
            error = error or "账号请求上游失败"
            with ctx._lock:
                current = ctx._sessions.get(sid) or dict(sess)
                current["status"] = "error"
                current["error"] = f"导入前测活失败：{error}"
                current["message"] = "导入前测活失败，已停止自动导入"
                blocked_import = dict(current.get("auto_import") or {})
                blocked_import.update(
                    {
                        "enabled": True,
                        "ok": False,
                        "skipped": False,
                        "queued": False,
                        "waiting_for_probe": False,
                        "error": current["error"],
                    }
                )
                current["auto_import"] = blocked_import
                current["updated_at"] = ctx._now()
                ctx._append_session_event(
                    current,
                    "pre_import_probe_failed",
                    current["message"],
                    at=current["updated_at"],
                )
                ctx._sessions[sid] = current
                ctx._mirror_reg_sess(sid, current)
    if recheck_delay is not None:
        ctx._schedule_probe_recheck(
            sid,
            config,
            current_attempt=max(0, int(recheck_attempt)),
            next_attempt=max(0, int(recheck_attempt)) + 1,
            delay=recheck_delay,
        )
    return {
        "ok": summary["fail"] == 0 and summary["uncertain"] == 0,
        "session_id": sid,
        "probe": summary,
    }


def retry_registration_probe(
    ctx: RegistrationContext,
    session_id: str,
    post_registration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sid = str(session_id or "").strip()
    sess = ctx._load_reg_sess(sid)
    if not sess:
        return {"ok": False, "error": "registration session not found"}
    probe = sess.get("probe") if isinstance(sess.get("probe"), dict) else {}
    if str(probe.get("state") or "").lower() in {"running", "retry_pending"}:
        return {"ok": True, "session_id": sid, "already_running": True}
    ctx.threading.Thread(
        target=ctx._retry_probe_now,
        args=(sid, post_registration),
        daemon=True,
        name=f"gba-retry-probe-{sid[-8:]}",
    ).start()
    return {"ok": True, "session_id": sid, "scheduled": True}


def retry_registration_batch_probe(
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
        probe = sess.get("probe") if isinstance(sess.get("probe"), dict) else {}
        if str(probe.get("state") or "").lower() not in {
            "running",
            "retry_pending",
        } and (sess.get("imported_account_ids") or []):
            candidates.append(str(sid))

    def run() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def probe_one(sid: str) -> dict[str, Any]:
            while True:
                with ctx._lock:
                    paused = bool(
                        (ctx._batches.get(bid) or {}).get("probe_pause_requested")
                    )
                if not paused:
                    break
                ctx.time.sleep(0.25)
            return ctx._retry_probe_now(sid, post_registration)

        config = dict(post_registration or {})
        limit = max(
            1,
            min(
                ctx.MAX_CONCURRENCY,
                int(
                    config.get("probe_concurrency")
                    or config.get("pipeline_concurrency")
                    or 1
                ),
            ),
        )
        with ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="gba-manual-probe"
        ) as pool:
            futures = [pool.submit(probe_one, sid) for sid in candidates]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass
        with ctx._lock:
            current_batch = ctx._batches.get(bid) or batch
            current_batch["probe_status"] = "completed"
            current_batch["probe_pause_requested"] = False
            current_batch["updated_at"] = ctx._now()
            ctx._batches[bid] = current_batch
            ctx._mirror_reg_batch(bid, dict(current_batch))

    if candidates:
        with ctx._lock:
            for sid in candidates:
                current = ctx._sessions.get(sid) or ctx._load_reg_sess(sid) or {}
                probe = dict(current.get("probe") or {})
                probe.update({"state": "queued", "queued": True, "position": 0})
                current["probe"] = probe
                current["updated_at"] = ctx._now()
                ctx._append_session_event(
                    current, "probe_queued", "批次测活已排队", at=current["updated_at"]
                )
                ctx._sessions[sid] = current
                ctx._mirror_reg_sess(sid, current)
        ctx.threading.Thread(
            target=run, daemon=True, name=f"gba-retry-probe-{bid[-8:]}"
        ).start()
    else:
        with ctx._lock:
            current_batch = ctx._batches.get(bid) or batch
            current_batch["probe_status"] = "completed"
            current_batch["updated_at"] = ctx._now()
            ctx._batches[bid] = current_batch
            ctx._mirror_reg_batch(bid, dict(current_batch))
    return {"ok": True, "batch_id": bid, "scheduled": len(candidates)}


def pause_registration_batch_probe(
    ctx: RegistrationContext, batch_id: str
) -> dict[str, Any]:
    bid = str(batch_id or "").strip()
    with ctx._lock:
        batch = ctx._batches.get(bid) or ctx._load_reg_batch(bid)
        if batch is None:
            return {"ok": False, "error": "registration batch not found"}
        batch["probe_pause_requested"] = True
        batch["probe_status"] = "paused"
        batch["updated_at"] = ctx._now()
        ctx._batches[bid] = batch
        ctx._mirror_reg_batch(bid, dict(batch))
    return {"ok": True, "batch_id": bid, "probe_status": "paused"}


def resume_registration_batch_probe(
    ctx: RegistrationContext, batch_id: str
) -> dict[str, Any]:
    bid = str(batch_id or "").strip()
    with ctx._lock:
        batch = ctx._batches.get(bid) or ctx._load_reg_batch(bid)
        if batch is None:
            return {"ok": False, "error": "registration batch not found"}
        batch["probe_pause_requested"] = False
        batch["probe_status"] = "running"
        batch["updated_at"] = ctx._now()
        ctx._batches[bid] = batch
        ctx._mirror_reg_batch(bid, dict(batch))
    return {"ok": True, "batch_id": bid, "probe_status": "running"}
