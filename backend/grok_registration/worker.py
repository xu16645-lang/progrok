"""Single-session xAI browser registration worker."""

from __future__ import annotations

from typing import Any

from .flow import RegistrationContext
from .worker_lifecycle import WorkerSessionLifecycle


def _run_registration(
    ctx: RegistrationContext,
    sid: str,
    yescaptcha_key: str,
    proxy: str,
    receiver: Any,
    browser_runtime: Any | None = None,
) -> None:
    del yescaptcha_key  # Kept in the call signature for persisted batch compatibility.
    with ctx._lock:
        sess = ctx._sessions.get(sid)
    if not sess:
        sess = ctx._load_reg_sess(sid)
    if not sess:
        return
    with ctx._lock:
        ctx._sessions[sid] = sess

    lifecycle = WorkerSessionLifecycle(ctx, sid, sess)
    update = lifecycle.update
    _check_cancel = lifecycle.check_cancel
    email = str(sess.get("email") or "").strip().lower()
    password = str(sess.get("password") or "")
    headless = bool(sess.get("_headless", True))
    pipeline_cfg = dict(sess.get("_post_registration") or {})
    step_delay_ms = max(
        0, min(30_000, int(pipeline_cfg.get("step_delay_ms") or 0))
    )
    hotmail_account_id = str(sess.get("_hotmail_account_id") or "")
    hotmail_alias_index = sess.get("_hotmail_alias_index")
    try:
        hotmail_alias_index = (
            int(hotmail_alias_index) if hotmail_alias_index is not None else None
        )
    except (TypeError, ValueError):
        hotmail_alias_index = None
    hotmail_marked_used = False
    browser_session = None

    if not email or not password:
        update(
            "error",
            "xAI 注册任务缺少邮箱或密码",
            error="missing email or password",
        )
        return

    def _should_cancel() -> bool:
        return ctx._session_cancel_requested(ctx._sessions.get(sid))

    def _get_code(force_refresh: bool = False) -> str | None:
        _check_cancel()
        if force_refresh and hasattr(receiver, "mark_code_request_started"):
            receiver.mark_code_request_started()
        kwargs = {
            "timeout": 180.0,
            "should_cancel": _should_cancel,
            "poll_interval": 1.0,
        }
        try:
            return receiver.wait_for_code(**kwargs)
        except TypeError:
            return receiver.wait_for_code(timeout=180.0)

    def _on_progress(message: str) -> None:
        update("registering", message)
        if str(message).lower().startswith("[xai] email: submitted") and hasattr(
            receiver, "mark_code_request_started"
        ):
            receiver.mark_code_request_started()

    try:
        _check_cancel()
        update("starting", f"启动 xAI 浏览器注册；邮箱={email}")
        from xai_browser import register_xai_account

        proxy_dict = ctx._proxy_for_browser(proxy) if proxy else None
        result, browser_session = register_xai_account(
            email=email,
            password=password,
            get_verification_code=_get_code,
            proxy=proxy_dict,
            headless=headless,
            timeout_sec=360.0,
            should_cancel=_should_cancel,
            on_progress=_on_progress,
            operation_delay_ms=step_delay_ms,
            browser_runtime=browser_runtime,
        )
        sso = str(result.get("sso") or "").strip()
        cookies = dict(result.get("cookies") or {})
        if not sso:
            raise RuntimeError("xAI 浏览器注册完成，但未取得 sso cookie")

        session_dir = ctx.RUNTIME_DATA_DIR / "register_sso"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / f"{sid}.browser-session.json"
        session_file.write_text(
            ctx.json.dumps(
                {
                    "email": email,
                    "password": password,
                    "sso": sso,
                    "cookies": cookies,
                    "source": "xai-browser",
                    "created_at": ctx._now(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        update(
            "fetching_sso",
            "xAI 浏览器注册成功，已保存原始浏览器 Session",
            sso=sso,
            session_file=str(session_file),
            registration_succeeded_at=ctx._now(),
        )

        if hotmail_account_id and not hotmail_marked_used:
            from hotmail_local import mark_used

            hotmail_marked_used = mark_used(
                hotmail_account_id, alias_index=hotmail_alias_index
            )

        output_target = str(
            pipeline_cfg.get("output_format")
            or pipeline_cfg.get("target")
            or "cpa"
        ).strip().lower()
        output_target = output_target if output_target in {"cpa", "sub2api"} else "cpa"
        auto_import_enabled = bool(pipeline_cfg.get("auto_import_enabled"))
        pre_import_probe_enabled = bool(
            pipeline_cfg.get("pre_import_probe_enabled", True)
        )

        output_format_label = "CPA JSON" if output_target == "cpa" else "Sub2API JSON"
        update(
            "importing",
            "正在生成本地 xAI PKCE 会话",
            registration_json_format=output_target,
        )
        from xai_pkce import (
            authorize_and_exchange,
            http_timeout,
            proxy_kwargs,
            token_to_auth_payload,
        )

        pkce_file = ctx.RUNTIME_DATA_DIR / "pkce_sessions" / f"{sid}.json"
        pkce_messages = {
            "session_created": "本地 PKCE 会话已生成，正在浏览器中完成 xAI OAuth 授权",
            "callback_captured": "已捕获 xAI OAuth 回调，正在本地兑换 AT/RT",
            "token_exchanged": "本地 AT/RT 兑换完成",
        }

        def _on_pkce_progress(stage: str) -> None:
            update(
                "importing",
                pkce_messages.get(stage, f"xAI OAuth：{stage}"),
                registration_json_format=output_target,
                pkce_session_file=str(pkce_file),
            )

        token = authorize_and_exchange(
            browser_session.authorize_pkce,
            session_path=pkce_file,
            should_cancel=_check_cancel,
            on_progress=_on_pkce_progress,
            proxy_kwargs=proxy_kwargs(proxy),
            timeout=http_timeout(),
        )
        if (
            not token
            or not str(token.get("access_token") or "").strip()
            or not str(token.get("refresh_token") or "").strip()
        ):
            raise RuntimeError(
                "本地 xAI PKCE 兑换未返回完整 AT/RT；原始 Session 已保留"
            )

        auth_payload = token_to_auth_payload(token, email=email)
        auth_payload["password"] = password

        import accounts

        output_format = "sub2api" if output_target == "sub2api" else "cpa"
        update(
            "converting",
            f"正在转换 {output_format_label}",
            registration_json_format=output_format,
        )
        import_result = accounts.import_auth_payload(
            auth_payload,
            merge=True,
            output_format=output_format,
        )
        if not import_result.get("ok"):
            raise RuntimeError(
                f"xAI auth 导入本地账户池失败：{import_result.get('error')}"
            )
        imported_rows = [
            row
            for row in (import_result.get("imported") or [])
            if isinstance(row, dict)
        ]
        imported_ids = [str(row.get("id")) for row in imported_rows if row.get("id")]
        imported_accounts = [
            {"id": row.get("id"), "email": row.get("email") or email}
            for row in imported_rows
            if row.get("id") or row.get("email")
        ]
        with ctx._lock:
            current = ctx._sessions.get(sid) or sess
            current["auth_json"] = import_result
            current["imported_account_ids"] = imported_ids
            current["imported_accounts"] = imported_accounts
            current["oauth"] = {
                "path": "local_authorization_code_pkce",
                "access_token": (token.get("access_token") or "")[:20] + "...",
                "refresh_token": bool(token.get("refresh_token")),
                "email": email,
                "pkce_session_file": str(pkce_file),
            }
            current["registration_json_format"] = output_format
            current["updated_at"] = ctx._now()
            ctx._sessions[sid] = current
            ctx._mirror_reg_sess(sid, current)
        update(
            "converted",
            f"{output_format_label} 转换完成，已写入本地账户池",
            registration_json_format=output_format,
        )
        if not auto_import_enabled:
            ctx._finish_pipeline_without_import(sid, pipeline_cfg)
        elif pre_import_probe_enabled:
            with ctx._lock:
                current = ctx._sessions.get(sid) or sess
                current["auto_import"] = {
                    "enabled": True,
                    "target": output_target,
                    "ok": None,
                    "skipped": False,
                    "queued": False,
                    "waiting_for_probe": True,
                }
                current["updated_at"] = ctx._now()
                ctx._sessions[sid] = current
                ctx._mirror_reg_sess(sid, current)

        if pre_import_probe_enabled:
            ctx._enqueue_pipeline_phase(sid, "probe")
        elif auto_import_enabled:
            ctx._enqueue_pipeline_phase(sid, "import")
        return
    except ctx._RegPaused as exc:
        with ctx._lock:
            cur = ctx._sessions.get(sid) or sess
            cur["status"] = "paused"
            cur["message"] = str(exc) or "paused by user"
            cur["error"] = None
            cur["pause_requested"] = True
            cur["cancel_requested"] = False
            cur["updated_at"] = ctx._now()
            ctx._append_session_event(cur, cur["status"], cur["message"], at=cur["updated_at"])
            ctx._sessions[sid] = cur
            ctx._mirror_reg_sess(sid, cur)
        return
    except ctx._RegCancelled as exc:
        with ctx._lock:
            cur = ctx._sessions.get(sid) or sess
            cur["status"] = "cancelled"
            cur["message"] = str(exc) or "cancelled by user"
            cur["error"] = "cancelled"
            cur["cancel_requested"] = True
            cur["updated_at"] = ctx._now()
            ctx._sessions[sid] = cur
            ctx._mirror_reg_sess(sid, cur)
        return
    except Exception as exc:  # noqa: BLE001
        try:
            _check_cancel()
            if browser_session is not None:
                browser_session.hold_failure()
            error_detail = str(exc).strip()
            error_summary = (error_detail.splitlines()[0] if error_detail else type(exc).__name__)[:500]
            update(
                "error",
                f"xAI 浏览器注册失败：{error_summary}",
                error=error_detail,
            )
        except ctx._RegPaused as paused_exc:
            with ctx._lock:
                cur = ctx._sessions.get(sid) or sess
                cur["status"] = "paused"
                cur["message"] = str(paused_exc) or "paused by user"
                cur["error"] = None
                cur["pause_requested"] = True
                cur["cancel_requested"] = False
                cur["updated_at"] = ctx._now()
                ctx._sessions[sid] = cur
                ctx._mirror_reg_sess(sid, cur)
        except ctx._RegCancelled:
            with ctx._lock:
                cur = ctx._sessions.get(sid) or sess
                cur["status"] = "cancelled"
                cur["message"] = "cancelled by user"
                cur["error"] = "cancelled"
                cur["cancel_requested"] = True
                cur["updated_at"] = ctx._now()
                ctx._sessions[sid] = cur
                ctx._mirror_reg_sess(sid, cur)
    finally:
        if browser_session is not None:
            browser_session.close()
        with ctx._lock:
            final_sess = dict(ctx._sessions.get(sid) or sess or {})
            if sid in ctx._sessions:
                ctx._sessions[sid].pop("_receiver", None)
        if hotmail_account_id and not hotmail_marked_used:
            final_status = str(final_sess.get("status") or "").lower()
            try:
                if final_status in {"cancelled", "stopped", "stopping", "paused"}:
                    from hotmail_local import release_account

                    release_account(
                        hotmail_account_id, alias_index=hotmail_alias_index
                    )
                else:
                    from hotmail_local import mark_failed

                    mark_failed(
                        hotmail_account_id,
                        str(
                            final_sess.get("error")
                            or final_sess.get("message")
                            or "xAI 浏览器注册失败"
                        ),
                        alias_index=hotmail_alias_index,
                    )
            except Exception:
                pass
        if final_sess and not final_sess.get("batch_id"):
            payload = ctx._session_task_log_payload(final_sess)
            if payload.get("finished"):
                ctx._record_register_task(
                    task_id=payload["task_id"] or sid,
                    summary=payload["summary"],
                    status=payload["status"],
                    ok=payload["ok"],
                    progress_done=payload["progress_done"],
                    progress_total=payload["progress_total"],
                    finished=True,
                    detail={**payload["detail"], "phase": "finished"},
                )
