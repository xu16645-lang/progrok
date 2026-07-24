"""Single-session ChatGPT registration worker."""

from __future__ import annotations

from typing import Any


def _run_registration(ctx, sid, proxy, receiver, browser_runtime=None):
    """Execute the ChatGPT registration flow for one session."""
    with ctx._lock:
        sess = ctx._sessions.get(sid)
    if not sess:
        return
    with ctx._lock:
        ctx._sessions[sid] = sess

    def _check_cancel() -> None:
        with ctx._lock:
            cur = ctx._sessions.get(sid) or sess
        if ctx._session_cancel_requested(cur):
            raise ctx._RegCancelled(cur.get("message") or "cancelled by user")

    def update(status: str, message: str, **kwargs: Any) -> None:
        _check_cancel()
        with ctx._lock:
            cur = ctx._sessions.get(sid) or sess
            if ctx._session_cancel_requested(cur) and status not in (
                "cancelled",
                "stopped",
                "error",
                "imported",
            ):
                raise ctx._RegCancelled(cur.get("message") or "cancelled by user")
            now = ctx._now()
            cur["status"] = status
            cur["message"] = message
            cur["updated_at"] = now
            cur.update(kwargs)
            ctx._append_session_event(cur, status, message, at=now)
            ctx._sessions[sid] = cur

    email = str(sess.get("email") or "").strip().lower()
    headless = bool(sess.get("_headless", True))
    pipeline_cfg = dict(sess.get("_post_registration") or {})
    step_delay_ms = max(0, min(30000, int(pipeline_cfg.get("step_delay_ms") or 3000)))
    hotmail_account_id = str(sess.get("_hotmail_account_id") or "")
    hotmail_marked_used = False

    def _pipeline_step_delay() -> None:
        remaining = step_delay_ms
        while remaining > 0:
            _check_cancel()
            chunk = min(200, remaining)
            ctx.time.sleep(chunk / 1000.0)
            remaining -= chunk

    if not email:
        update("error", "missing email for registration session", error="missing email")
        return
    try:
        _check_cancel()
        update("starting", f"starting ChatGPT registration; email={email}")
        from chatgpt_browser import register_chatgpt_account

        proxy_dict = ctx._proxy_for_browser(proxy) if proxy else None
        used_codes: set[str] = set()
        code_lock = ctx.threading.Lock()
        code_state: dict[str, Any] = {
            "code": None,
            "error": "",
            "thread": None,
            "event": ctx.threading.Event(),
            "generation": 0,
        }

        def _start_code_prefetch(force_refresh: bool = False) -> None:
            with code_lock:
                if force_refresh:
                    if code_state.get("code"):
                        used_codes.add(str(code_state["code"]))
                    code_state["generation"] = (
                        int(code_state.get("generation") or 0) + 1
                    )
                    code_state["code"] = None
                    code_state["error"] = ""
                    code_state["thread"] = None
                    code_state["event"] = ctx.threading.Event()
                current = code_state.get("thread")
                if isinstance(current, ctx.threading.Thread) and current.is_alive():
                    return
                if code_state.get("code"):
                    return
                generation = int(code_state.get("generation") or 0)

                def _prefetch() -> None:
                    code = None
                    error = ""
                    try:
                        code = receiver.wait_for_code(
                            timeout=180,
                            should_cancel=lambda: ctx._session_cancel_requested(
                                ctx._sessions.get(sid)
                            ),
                            poll_interval=2.5,
                            exclude_codes=set(used_codes),
                        )
                    except Exception as exc:
                        error = str(exc)[:300]
                    with code_lock:
                        if generation != int(code_state.get("generation") or 0):
                            return
                        code_state["code"] = code
                        code_state["error"] = error
                        code_state["event"].set()

                thread = ctx.threading.Thread(
                    target=_prefetch, daemon=True, name=f"cgpt-mail-code-{sid[-8:]}"
                )
                code_state["thread"] = thread
                thread.start()

        def _get_code(force_refresh: bool = False) -> str | None:
            _check_cancel()
            if force_refresh and hasattr(receiver, "mark_code_request_started"):
                receiver.mark_code_request_started()
            _start_code_prefetch(force_refresh)
            deadline = ctx.time.time() + 185
            while ctx.time.time() < deadline:
                _check_cancel()
                with code_lock:
                    event = code_state["event"]
                    code = code_state.get("code")
                    thread = code_state.get("thread")
                if code:
                    return str(code)
                if event.wait(timeout=0.25):
                    with code_lock:
                        code = code_state.get("code")
                        error = str(code_state.get("error") or "")
                    if code:
                        return str(code)
                    if error:
                        if (
                            error
                            == "timeout waiting for ChatGPT email verification code"
                        ):
                            return None
                        raise RuntimeError(f"邮箱验证码预取失败: {error}")
                    return None
                if isinstance(thread, ctx.threading.Thread) and (not thread.is_alive()):
                    break
            with code_lock:
                error = str(code_state.get("error") or "")
            if error:
                if error == "timeout waiting for ChatGPT email verification code":
                    return None
                raise RuntimeError(f"邮箱验证码预取失败: {error}")
            return None

        def _on_progress(msg: str) -> None:
            nonlocal hotmail_marked_used
            try:
                update("registering", msg)
                progress = str(msg or "").strip().lower()
                if progress.startswith("[chatgpt] email: submitted") and hasattr(
                    receiver, "mark_code_request_started"
                ):
                    receiver.mark_code_request_started()
                if any(
                    (
                        marker in progress
                        for marker in (
                            "[chatgpt] email: submitted",
                            "[chatgpt] password: submitted",
                            "[chatgpt] verification: waiting_for_form",
                        )
                    )
                ):
                    _start_code_prefetch(False)
                if (
                    hotmail_account_id
                    and (not hotmail_marked_used)
                    and str(msg or "")
                    .strip()
                    .lower()
                    .startswith("[chatgpt] completion: done")
                ):
                    from hotmail_local import mark_used

                    if mark_used(hotmail_account_id):
                        hotmail_marked_used = True
            except ctx._RegCancelled:
                raise

        def _should_cancel() -> bool:
            return ctx._session_cancel_requested(ctx._sessions.get(sid))

        update("registering", "launching browser for ChatGPT signup")
        result = register_chatgpt_account(
            email=email,
            password=str(sess.get("password") or ""),
            get_verification_code=_get_code,
            proxy=proxy_dict,
            headless=headless,
            timeout_sec=300.0,
            should_cancel=_should_cancel,
            on_progress=_on_progress,
            operation_delay_ms=step_delay_ms,
            browser_runtime=browser_runtime,
        )
        if not result.get("ok"):
            error_msg = result.get("error", "registration failed")
            if result.get("cancelled"):
                update("cancelled", error_msg, error="cancelled")
            elif "account_deactivated" in str(
                error_msg
            ).lower() or "账号已删除或停用" in str(error_msg):
                update(
                    "account_error",
                    error_msg,
                    error=error_msg,
                    error_type="account_deactivated",
                )
            else:
                update("protocol_error", error_msg, error=error_msg)
            return
        if hotmail_account_id and (not hotmail_marked_used):
            from hotmail_local import mark_used

            hotmail_marked_used = mark_used(hotmail_account_id)
        session_data = result.get("session")
        if (
            not isinstance(session_data, dict)
            or not str(session_data.get("accessToken") or "").strip()
        ):
            error_msg = "ChatGPT Session 缺少 accessToken，已停止转换和导入"
            update("error", error_msg, error="session_missing_access_token")
            return
        try:
            session_file = ctx._save_original_chatgpt_session(
                session_data, email=email, session_id=sid
            )
        except Exception:
            error_msg = "ChatGPT 原始 Session 保存失败，已停止转换和导入"
            update("error", error_msg, error="session_save_failed")
            return
        update(
            "fetching_sso",
            "ChatGPT Session 获取并已保存",
            session_data=session_data,
            session_file=session_file,
        )
        _pipeline_step_delay()
        update("converting", "正在注册 Codex Agent Identity")
        import chatgpt_session_to_auth as cs2a

        try:
            auth_dict = cs2a.session_to_auth_entry(
                session_data, proxy=proxy, verify_task=True
            )
        except cs2a.AgentIdentityUpstreamError as exc:
            error_msg = str(exc)[:500]
            if exc.error_code == "token_revoked":
                update(
                    "account_error",
                    "Agent Identity 注册失败：ChatGPT Session 已被上游撤销，账号已标记异常",
                    error=error_msg,
                    error_type="session_token_revoked",
                    session_file=session_file,
                )
            else:
                update("error", error_msg, error=error_msg, session_file=session_file)
            return
        first_entry = next(iter(auth_dict.values()), {})
        agent_id = first_entry.get("agent_identity", {})
        if (
            not isinstance(agent_id, dict)
            or not str(agent_id.get("task_id") or "").strip()
        ):
            error_msg = "Agent Identity 任务注册未返回 task_id，已阻止保存和导入"
            update("error", error_msg, error=error_msg)
            return
        update("converting", "Codex Agent Identity 注册完成，正在转换 auth.json")
        _pipeline_step_delay()
        auth_payload = {
            "key": agent_id.get("agent_runtime_id", ""),
            "access_token": session_data.get("accessToken", ""),
            "auth_mode": "agent_identity",
            "email": agent_id.get("email") or email,
            "refresh_token": "",
            "id_token": "",
            "token_type": "Bearer",
            "expires_at": agent_id.get("expires_at"),
            "oidc_issuer": "https://auth.openai.com",
            "oidc_client_id": ctx.CHATGPT_ADAPTER_BUILD,
            "sso": ctx.json.dumps(session_data) if session_data else "",
            "password": sess.get("password", ""),
            "agent_identity": agent_id,
        }
        output_target = str(
            pipeline_cfg.get("output_format") or pipeline_cfg.get("target") or "cpa"
        ).lower()
        output_format = "sub2api" if output_target == "sub2api" else "cpa"
        import accounts

        import_result = accounts.import_auth_payload(
            auth_payload, merge=True, output_format=output_format
        )
        if not import_result.get("ok"):
            update(
                "error",
                f"account save failed: {import_result.get('error')}",
                error=import_result.get("error"),
            )
            return
        imported_rows = import_result.get("imported") or []
        imported_ids = [str(r.get("id") or "") for r in imported_rows if r.get("id")]
        imported_accounts = [
            {"id": r.get("id"), "email": r.get("email")}
            for r in imported_rows
            if r.get("id") or r.get("email")
        ]
        auto_enabled = bool(pipeline_cfg.get("auto_import_enabled"))
        update(
            "converted",
            "Agent Identity auth.json 转换完成",
            auth_json=import_result,
            imported_account_ids=imported_ids,
            imported_accounts=imported_accounts,
            session_data=session_data,
            session_file=session_file,
            auto_import={
                "enabled": auto_enabled,
                "target": pipeline_cfg.get("target", "sub2api"),
                "ok": None,
            },
        )
        if auto_enabled and imported_ids:
            target_name = (
                "Sub2API"
                if str(pipeline_cfg.get("target") or "sub2api").lower() == "sub2api"
                else "CPA"
            )
            update("auto_importing", f"正在导入 {target_name}；测活结果不影响导入")
            _pipeline_step_delay()
            from account_pipeline import import_account

            site_result = import_account(auth_payload, pipeline_cfg)
            auto_import = {
                "enabled": True,
                "target": pipeline_cfg.get("target", "sub2api"),
                "ok": bool(site_result.get("ok")),
                "imported": int(
                    site_result.get("created")
                    or site_result.get("updated")
                    or (1 if site_result.get("ok") else 0)
                ),
                "failed": 0 if site_result.get("ok") else 1,
                "result": site_result,
            }
            if not site_result.get("ok"):
                error_text = str(site_result.get("error") or "站点导入失败")[:300]
                update(
                    "error",
                    f"{target_name} 导入失败：{error_text}",
                    error=error_text,
                    auto_import=auto_import,
                )
                return
            update(
                "probing",
                f"正在通过 {target_name} 使用 {pipeline_cfg.get('model') or ctx.CHATGPT_DEFAULT_PROBE_MODEL} 发送消息测活",
            )
            _pipeline_step_delay()
            probe_result = ctx._probe_registration_session(sid, pipeline_cfg)
            probe_note = ""
            if not probe_result.get("ok"):
                error_text = str(probe_result.get("error") or "ChatGPT 账号测活失败")[
                    :300
                ]
                probe_note = f"；测活失败已独立记录：{error_text}"
            update(
                "imported",
                f"{target_name} 导入成功，测活结果不影响导入{probe_note}",
                auto_import=auto_import,
            )
            try:
                from account_rotation import record_imported_session

                with ctx._lock:
                    rotation_session = dict(ctx._sessions.get(sid) or {})
                record_imported_session("chatgpt", rotation_session)
            except Exception:
                pass
        else:
            update(
                "probing",
                f"正在使用 {pipeline_cfg.get('model') or ctx.CHATGPT_DEFAULT_PROBE_MODEL} 测活 ChatGPT 账号",
            )
            _pipeline_step_delay()
            probe_result = ctx._probe_registration_session(sid, pipeline_cfg)
            probe_note = ""
            if not probe_result.get("ok"):
                error_text = str(probe_result.get("error") or "ChatGPT 账号测活失败")[
                    :300
                ]
                probe_note = f"；测活失败已独立记录：{error_text}"
            update(
                "imported",
                f"注册及 Agent Identity JSON 转换完成（未启用站点导入）{probe_note}",
                auto_import={
                    "enabled": False,
                    "target": pipeline_cfg.get("target", "sub2api"),
                    "ok": None,
                    "skipped": True,
                },
            )
    except ctx._RegCancelled:
        update("cancelled", "registration cancelled", error="cancelled")
    except Exception as e:
        msg = str(e)[:300]
        update("error", f"注册流程失败：{msg}", error=msg)
    finally:
        hotmail_account_id = str(sess.get("_hotmail_account_id") or "")
        if hotmail_account_id:
            with ctx._lock:
                final_session = dict(ctx._sessions.get(sid) or {})
                final_status = str(final_session.get("status") or "").lower()
                completed = (
                    hotmail_marked_used
                    or final_status == "imported"
                    or bool(final_session.get("session_data"))
                )
            if not completed:
                if final_status in {"cancelled", "stopped", "stopping"}:
                    from hotmail_local import release_account

                    release_account(hotmail_account_id)
                else:
                    from hotmail_local import mark_failed

                    mark_failed(
                        hotmail_account_id,
                        str(
                            final_session.get("error")
                            or final_session.get("message")
                            or "注册失败"
                        ),
                    )
        with ctx._lock:
            if sid in ctx._sessions:
                ctx._sessions[sid].pop("_receiver", None)
