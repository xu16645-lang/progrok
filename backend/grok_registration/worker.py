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
        )

        if hotmail_account_id and not hotmail_marked_used:
            from hotmail_local import mark_used

            hotmail_marked_used = mark_used(hotmail_account_id)

        output_target = str(
            pipeline_cfg.get("output_format")
            or pipeline_cfg.get("target")
            or "cpa"
        ).strip().lower()
        direct_sub2_oauth = bool(pipeline_cfg.get("auto_import_enabled")) and (
            output_target == "sub2api"
        )
        if direct_sub2_oauth:
            update("importing", "正在向 Sub2API 请求 Grok OAuth 授权链接")
            from account_pipeline import create_grok_oauth_in_sub2api

            direct_result = create_grok_oauth_in_sub2api(
                authorize=browser_session.authorize_sub2_oauth,
                email=email,
                base_url=str(pipeline_cfg.get("sub2api_base_url") or ""),
                api_key=str(pipeline_cfg.get("sub2api_api_key") or ""),
                auth_mode=str(pipeline_cfg.get("sub2api_auth_mode") or "password"),
                admin_email=str(pipeline_cfg.get("sub2api_admin_email") or ""),
                admin_password=str(pipeline_cfg.get("sub2api_admin_password") or ""),
                group_id=int(pipeline_cfg.get("sub2api_xai_group_id") or 0),
            )
            if not direct_result.get("ok"):
                raise RuntimeError(
                    f"Sub2API Grok OAuth 直接导入失败：{direct_result.get('error') or '未知错误'}；"
                    "原始 Session 已保留"
                )
            external_account_id = str(direct_result.get("account_id") or "")
            auto_import = {
                "enabled": True,
                "target": "sub2api",
                "ok": True,
                "skipped": False,
                "count": 1,
                "imported": 1,
                "failed": 0,
                "group_id": direct_result.get("group_id"),
                "path": "grok_oauth_callback",
            }
            update(
                "imported",
                "Sub2API OAuth 授权码已回填，账号已直接导入并绑定分组",
                auto_import=auto_import,
                imported_account_ids=[],
                imported_accounts=[
                    {
                        "id": external_account_id,
                        "email": direct_result.get("email") or email,
                        "site": "sub2api",
                    }
                ],
                oauth={
                    "path": "sub2api_login_callback",
                    "email": email,
                    "account_id": external_account_id,
                },
                pipeline_queue={
                    "phase": "done",
                    "position": 0,
                    "concurrency": int(
                        pipeline_cfg.get("pipeline_concurrency") or 1
                    ),
                },
            )
            try:
                from account_rotation import record_imported_session

                with ctx._lock:
                    imported_session = dict(ctx._sessions.get(sid) or sess)
                record_imported_session("xai", imported_session)
            except Exception:
                pass
            return

        update("importing", "正在浏览器中完成 xAI OAuth 设备授权")
        import sso_to_auth_json as sso_import

        conversion_failure: dict[str, Any] = {}
        token = sso_import.sso_to_token_with_browser(
            sso,
            browser_session.authorize_device,
            failure=conversion_failure,
            should_cancel=_check_cancel,
        )
        if not token or not token.get("access_token"):
            stage_labels = {
                "device_code": "设备码申请",
                "verify": "设备授权验证",
                "approve": "浏览器授权确认",
                "token_poll": "令牌获取",
            }
            reason_labels = {
                "rate_limited": "上游限流",
                "network_timeout": "网络超时",
                "invalid_sso": "SSO 已失效",
                "upstream_rejected": "上游拒绝请求",
            }
            stage = str(conversion_failure.get("stage") or "device_flow")
            reason = str(
                conversion_failure.get("reason") or "upstream_rejected"
            )
            detail = str(conversion_failure.get("detail") or "未返回可用令牌")
            if conversion_failure.get("retryable"):
                ctx._note_reg_pressure(f"browser device-flow {reason}", pause_sec=10)
            raise RuntimeError(
                "浏览器 OAuth 转换失败："
                f"{stage_labels.get(stage, stage)}（{reason_labels.get(reason, reason)}）；"
                f"上游信息：{detail}；原始 Session 已保留"
            )

        _key, entry = sso_import.token_to_auth_entry(token, email=email)
        auth_payload = {
            "key": entry["key"],
            "access_token": token.get("access_token", ""),
            "auth_mode": entry.get("auth_mode", "oidc"),
            "email": entry.get("email") or email,
            "refresh_token": entry.get("refresh_token", ""),
            "id_token": token.get("id_token", ""),
            "token_type": token.get("token_type", "Bearer"),
            "expires_in": token.get("expires_in"),
            "expires_at": entry.get("expires_at"),
            "scope": token.get("scope", ""),
            "oidc_issuer": entry.get("oidc_issuer", "https://auth.x.ai"),
            "oidc_client_id": entry.get("oidc_client_id", ""),
            "sso": sso,
            "password": password,
        }

        import accounts

        output_format = "sub2api" if output_target == "sub2api" else "cpa"
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
                "path": "browser_device_authorization",
                "access_token": (token.get("access_token") or "")[:20] + "...",
                "refresh_token": bool(token.get("refresh_token")),
                "email": email,
            }
            current["updated_at"] = ctx._now()
            ctx._sessions[sid] = current
            ctx._mirror_reg_sess(sid, current)
        update("importing", "xAI auth 转换完成，已写入本地账户池")
        if bool(pipeline_cfg.get("auto_import_enabled")):
            ctx._enqueue_pipeline_phase(sid, "import")
        else:
            ctx._finish_pipeline_without_import(sid, pipeline_cfg)
        ctx._enqueue_pipeline_phase(sid, "probe")
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
            update("error", f"xAI 浏览器注册失败：{exc}", error=str(exc))
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

                    release_account(hotmail_account_id)
                else:
                    from hotmail_local import mark_failed

                    mark_failed(
                        hotmail_account_id,
                        str(
                            final_sess.get("error")
                            or final_sess.get("message")
                            or "xAI 浏览器注册失败"
                        ),
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
