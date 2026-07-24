"""Single-session Grok registration worker."""

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
) -> None:
    with ctx._lock:
        sess = ctx._sessions.get(sid)
    if not sess:
        # Another worker may hold the durable copy; still try to load.
        sess = ctx._load_reg_sess(sid)
    if not sess:
        return
        # Re-bind process-local map so later progress stays readable on this worker.
    with ctx._lock:
        ctx._sessions[sid] = sess

    lifecycle = WorkerSessionLifecycle(ctx, sid, sess)
    update = lifecycle.update
    _check_cancel = lifecycle.check_cancel

    email = str(sess.get("email") or "").strip().lower()
    password = sess.get("password") or ""
    hotmail_account_id = str(sess.get("_hotmail_account_id") or "")
    hotmail_marked_used = False
    if not password:
        update(
            "error",
            "missing password for registration session",
            error="missing password",
        )
        if hotmail_account_id:
            try:
                from hotmail_local import mark_failed

                mark_failed(hotmail_account_id, "注册任务缺少密码")
            except Exception:
                pass
        return
    sess["email"] = email
    client = None
    try:
        _check_cancel()
        ctx.ensure_xconsole()
        from xconsole_client import (
            XConsoleAuthClient,
            YesCaptchaSolver,
            xai_oauth_login_protocol,
        )
        from xconsole_client import config as C
        from xconsole_client.oauth_protocol import extract_cookies_from_auth_client
        from xconsole_client.xai_oauth import (
            CLIPROXYAPI_GROK_HEADERS,
            build_cliproxyapi_auth_record,
        )
        import accounts
        from config import UPSTREAM_BASE

        update("registering", "visiting signup page")
        _check_cancel()
        client = XConsoleAuthClient(
            debug=True,
            proxy=proxy or "",
            signup_url="https://accounts.x.ai/sign-up?redirect=grok-com",
        )
        with ctx._lock:
            current = ctx._sessions.get(sid)
            if current is not None:
                current["_client"] = client
        client.visit_home()
        _check_cancel()
        client.load_signup_page()

        sitekey = (
            getattr(client, "turnstile_sitekey", None)
            or getattr(C, "TURNSTILE_SITEKEY", None)
            or ""
        ).strip()
        website_url = (
            getattr(client, "signup_url", None) or C.SIGNUP_URL or ""
        ).strip()
        if not sitekey:
            raise RuntimeError(
                "Turnstile sitekey missing. Signup page scrape failed and "
                "config TURNSTILE_SITEKEY is empty."
            )

        provider = (
            (
                ctx.CAPTCHA_PROVIDER
                or ctx.os.environ.get("GROK2API_CAPTCHA_PROVIDER")
                or ctx.os.environ.get("CAPTCHA_PROVIDER")
                or "local"
            )
            .strip()
            .lower()
        )
        if provider not in {"local", "yescaptcha"}:
            provider = "local"

        if provider == "local":
            # Always use in-container inline solver; ignore external/custom URL.
            endpoint = ctx._local_solver_base_url(None)
            solver_key = "local"
            auto_fallback = False

            # Re-check right before first solve so a mid-batch solver restart
            # doesn't burn mailboxes while HTTP is still down.
            def _solver_progress(message: str) -> None:
                _check_cancel()
                update("waiting_solver", message)

            wait = ctx.wait_for_local_solver(
                endpoint,
                timeout_sec=min(60.0, max(5.0, ctx.LOCAL_SOLVER_WAIT_SEC)),
                progress=_solver_progress,
            )
            if not wait.get("ready"):
                raise RuntimeError(
                    wait.get("error") or f"本地过盾未就绪，无法开始打码: {endpoint}"
                )
        else:
            # Cloud YesCaptcha only; never inherit local solver endpoint.
            endpoint = (
                ctx.os.environ.get("GROK2API_YESCAPTCHA_ENDPOINT")
                or ctx.os.environ.get("YESCAPTCHA_ENDPOINT")
                or ctx.os.environ.get("YESCAPTCHA_API_BASE")
                or ""
            ).strip() or None
            # Guard against accidental local leftover endpoint.
            if endpoint and (
                "127.0.0.1" in endpoint
                or "localhost" in endpoint
                or endpoint.rstrip("/").endswith(":5072")
            ):
                endpoint = None
            solver_key = (
                yescaptcha_key
                or ctx.YESCAPTCHA_KEY
                or ctx.os.environ.get("GROK2API_YESCAPTCHA_KEY")
                or ctx.os.environ.get("YESCAPTCHA_API_KEY")
                or ""
            ).strip()
            if not solver_key or solver_key == "local":
                raise RuntimeError("YesCaptcha 模式需要有效的 YESCAPTCHA_KEY")
            auto_fallback = True

        def _turnstile_progress(msg: str) -> None:
            # Raise cancel out of solver polling so stop doesn't wait full captcha timeout.
            _check_cancel()
            update("solving_turnstile", f"Turnstile: {msg}")

        solver = YesCaptchaSolver(
            solver_key,
            endpoint=endpoint,
            # Keep captcha wait bounded; cancel still interrupts via on_progress.
            timeout=float(
                ctx.os.environ.get("GROK2API_YESCAPTCHA_TIMEOUT", "120") or 120
            ),
            poll_interval=float(
                ctx.os.environ.get("GROK2API_YESCAPTCHA_POLL", "2") or 2
            ),
            debug=True,
            on_progress=_turnstile_progress,
            # Local: no cloud fallback. YesCaptcha: allow cn/global peer fallback.
            auto_fallback_endpoint=auto_fallback,
        )
        print(
            f"[grok-build-auth] turnstile provider={provider} website_url={website_url} "
            f"sitekey={sitekey} endpoint={getattr(solver, '_endpoint', '?')}"
        )

        # Critical ordering:
        # 1) solve Turnstile first (slow, ~20-40s)
        # 2) send email code
        # 3) wait for mailbox code
        # 4) immediately verify + create_account
        # Old order verified the code then waited for captcha; create_account then
        # failed with WKE=email:invalid-validation-code because the code expired /
        # was single-use after the slow captcha step.
        solver_label = "本地过盾" if provider == "local" else "YesCaptcha"
        update(
            "solving_turnstile",
            f"solving Turnstile via {solver_label} (before email code)",
        )
        _check_cancel()

        def _solve_turnstile(url: str, *, premium: bool = True) -> Any:
            # Bound local solves to the configured Camoufox browser slots while
            # keeping YesCaptcha requests independently parallel.
            # Local Camoufox has no premium tier — force Proxyless only.
            use_premium = bool(premium) and provider != "local"
            kwargs = {
                "website_url": url,
                "website_key": sitekey,
                "premium": use_premium,
                "fallback_non_premium": True,
            }
            if provider == "local":
                with ctx._local_captcha_slots:
                    _check_cancel()
                    try:
                        return solver.solve_turnstile(**kwargs)
                    except Exception as e:
                        # Camoufox queue meltdown / timeout → brief global pause
                        msg = str(e).lower()
                        if any(
                            k in msg
                            for k in (
                                "timeout",
                                "timed out",
                                "queue",
                                "busy",
                                "no browser",
                                "target closed",
                                "crashed",
                            )
                        ):
                            ctx._note_reg_pressure(f"local captcha: {e}")
                        raise
            return solver.solve_turnstile(**kwargs)

        try:
            # Local: Proxyless only. Remote YesCaptcha: premium M1 first.
            turnstile = _solve_turnstile(website_url, premium=(provider != "local"))
        except ctx._RegCancelled:
            raise
        except Exception as captcha_err:
            _check_cancel()
            alt_url = "https://accounts.x.ai/sign-up?redirect=cloud-console"
            if website_url.rstrip("/") == alt_url.rstrip("/"):
                alt_url = "https://accounts.x.ai/sign-up?redirect=grok-com"
            update(
                "solving_turnstile",
                f"primary Turnstile failed ({captcha_err}); retry {alt_url}",
            )
            turnstile = _solve_turnstile(alt_url, premium=False)
        if not turnstile:
            raise RuntimeError("YesCaptcha returned empty Turnstile token")
        _check_cancel()

        # Password can be validated any time before create; do it while warm.
        client.validate_password(email, password)

        update("registering", "sending email validation code")
        _check_cancel()
        if hasattr(receiver, "mark_code_request_started"):
            receiver.mark_code_request_started()
        send_res = client.create_email_validation_code(email)
        if hasattr(send_res, "ok") and send_res.ok is False:
            print(
                f"[grok-build-auth] CreateEmailValidationCode ok=False "
                f"http={getattr(send_res, 'http_status', None)} "
                f"grpc={getattr(send_res, 'grpc_status', None)}"
            )

        update("waiting_email", "waiting for xAI verification code")
        # Poll mailbox with cancel-aware receiver so stop lands in ~0.25–1s.
        _check_cancel()

        def _mail_should_cancel() -> bool:
            # _check_cancel raises _RegCancelled when stop is requested.
            _check_cancel()
            return False

        try:
            code = receiver.wait_for_code(
                timeout=120.0,
                should_cancel=_mail_should_cancel,
                poll_interval=1.0,
            )
        except TypeError:
            # Older receiver signature fallback.
            code = None
            mail_deadline = ctx.time.time() + 120.0
            while ctx.time.time() < mail_deadline:
                _check_cancel()
                try:
                    code = receiver.wait_for_code(
                        timeout=min(4.0, max(1.0, mail_deadline - ctx.time.time()))
                    )
                except Exception:
                    code = None
                if code:
                    break
        if not code:
            raise RuntimeError("email verification code timeout")
        code = str(code or "").strip().upper().replace(" ", "").replace("-", "")
        if len(code) != 6:
            raise RuntimeError(
                f"invalid email verification code shape: {code!r} "
                f"(expect 6 alnum chars)"
            )
        update(
            "registering", f"code received: {code}; verifying + creating immediately"
        )

        # Prefer empty castle token (YesCaptcha cannot mint Castle fingerprints).
        # Retry create_account once with a fresh Turnstile + fresh email code when
        # the first flight is a structured hard error (expired code / turnstile).
        create_attempts = 2
        res = None
        sc: list[str] = []
        rsc_body = ""
        rsc_preview = ""
        http_status = 0
        signup_err: str | None = None
        for ca in range(1, create_attempts + 1):
            if ca > 1:
                # Full refresh path for invalid code / captcha failures.
                update(
                    "solving_turnstile",
                    f"create_account hard error ({signup_err}); refreshing Turnstile+email code",
                )
                try:
                    turnstile = _solve_turnstile(
                        website_url, premium=(provider != "local")
                    )
                except Exception as captcha_err:  # noqa: BLE001
                    print(f"[grok-build-auth] turnstile refresh failed: {captcha_err}")
                    break
                    # New email code required after invalid-validation-code.
                try:
                    if hasattr(receiver, "mark_code_request_started"):
                        receiver.mark_code_request_started()
                    client.create_email_validation_code(email)
                    update("waiting_email", "waiting for fresh xAI verification code")
                    try:
                        code = receiver.wait_for_code(
                            timeout=120,
                            should_cancel=_mail_should_cancel,
                            poll_interval=1.0,
                            exclude_codes={code},
                        )
                    except TypeError:
                        code = receiver.wait_for_code(timeout=120)
                    code = (
                        str(code or "")
                        .strip()
                        .upper()
                        .replace(" ", "")
                        .replace("-", "")
                    )
                    if len(code) != 6:
                        raise RuntimeError(f"fresh email code invalid: {code!r}")
                    update("registering", f"fresh code received: {code}")
                except Exception as mail_err:  # noqa: BLE001
                    print(f"[grok-build-auth] email code refresh failed: {mail_err}")
                    break

                    # verify immediately before create_account (same second when possible)
            try:
                vres = client.verify_email_validation_code(email, code)
                print(
                    f"[grok-build-auth] VerifyEmailValidationCode "
                    f"ok={getattr(vres, 'ok', None)} "
                    f"http={getattr(vres, 'http_status', None)} "
                    f"grpc={getattr(vres, 'grpc_status', None)}"
                )
            except Exception as v_err:  # noqa: BLE001
                print(f"[grok-build-auth] verify_email error: {v_err}")

            update(
                "creating_account",
                f"creating xAI account (attempt {ca}/{create_attempts})",
            )
            res = client.create_account(
                email=email,
                given_name="User",
                family_name="Grok",
                password=password,
                email_validation_code=code,
                turnstile_token=turnstile,
                castle_request_token="",
                conversion_id=str(ctx.uuid.uuid4()),
            )
            sc = list(getattr(res, "set_cookies", None) or [])
            rsc_body = getattr(res, "rsc_body", "") or ""
            rsc_preview = rsc_body[:800]
            http_status = int(getattr(res, "http_status", 0) or 0)
            try:
                signup_err = client.extract_signup_error(rsc_body)
            except Exception:
                signup_err = None
            print(f"[grok-build-auth] create_account HTTP={http_status}")
            print(f"[grok-build-auth] create_account set-cookies count={len(sc)}")
            print(
                f"[grok-build-auth] create_account ok={bool(getattr(res, 'ok', False))}"
            )
            print(f"[grok-build-auth] create_account error={signup_err!r}")
            print(f"[grok-build-auth] create_account rsc_body preview: {rsc_preview}")
            print(f"[grok-build-auth] adapter_build={ctx.ADAPTER_BUILD}")
            sess["create_account_http"] = http_status
            sess["create_account_ok_flag"] = bool(getattr(res, "ok", False))
            sess["create_account_set_cookies"] = len(sc)
            sess["create_account_error"] = signup_err

            # Persist full body for offline diagnosis (truncated).
            try:
                debug_path = (
                    ctx.RUNTIME_DATA_DIR
                    / "register_sso"
                    / f"{sid}.create_account.rsc.txt"
                )
                debug_path.parent.mkdir(parents=True, exist_ok=True)
                debug_path.write_text(rsc_body[:200_000], encoding="utf-8")
            except Exception:
                pass

            if http_status != 200:
                # Non-200 is terminal for this attempt; try once more only on 5xx.
                if http_status >= 500 and ca < create_attempts:
                    continue
                raise RuntimeError(
                    "create_account transport failed. "
                    f"adapter_build={ctx.ADAPTER_BUILD}; HTTP {http_status}; "
                    f"error={signup_err!r}; set_cookies={len(sc)}; "
                    f"body_preview={rsc_preview!r}"
                )

                # Structured hard error: retry with fresh captcha when recoverable.
            if signup_err:
                recoverable = any(
                    x in str(signup_err).lower()
                    for x in (
                        "turnstile",
                        "rate_limited",
                        "rate limit",
                        "captcha",
                        "account_signup_error",
                    )
                )
                if recoverable and ca < create_attempts:
                    continue
                raise RuntimeError(
                    "create_account rejected by xAI. "
                    f"adapter_build={ctx.ADAPTER_BUILD}; HTTP {http_status}; "
                    f"error={signup_err!r}; set_cookies={len(sc)}; "
                    f"body_preview={rsc_preview!r}"
                )

                # HTTP 200 without structured error — proceed even if res.ok is False
                # due to historical false negatives on RSC-only flights.
            break

        if res is None or http_status != 200 or signup_err:
            raise RuntimeError(
                "create_account did not complete before session acquisition. "
                f"HTTP {http_status}; error={signup_err!r}"
            )

        if hotmail_account_id and not hotmail_marked_used:
            try:
                from hotmail_local import mark_used

                hotmail_marked_used = mark_used(hotmail_account_id)
            except Exception:
                pass

        update(
            "fetching_sso",
            f"create_account HTTP {http_status} accepted; extracting SSO [{ctx.ADAPTER_BUILD}]",
        )

        sso = None
        try:
            sso = client.fetch_sso_token(
                email=email,
                password=password,
                save=True,
                output_dir=str(ctx.RUNTIME_DATA_DIR / "sso_output"),
                retries=4,
            )
        except Exception as sso_fetch_err:  # noqa: BLE001
            print(f"[grok-build-auth] fetch_sso_token error: {sso_fetch_err}")

        if not sso:
            try:
                from xconsole_client.sso import (
                    SSOExtractor,
                    parse_all_set_cookie_urls,
                    parse_sso_from_set_cookies,
                    parse_sso_jwt_url,
                    parse_sso_token_from_text,
                )

                sso = parse_sso_from_set_cookies(sc) or parse_sso_token_from_text(
                    rsc_body
                )
                if not sso and rsc_body:
                    print(
                        f"[grok-build-auth] set-cookie candidates="
                        f"{parse_all_set_cookie_urls(rsc_body)[:3]}"
                    )
                    print(
                        f"[grok-build-auth] primary set-cookie url="
                        f"{parse_sso_jwt_url(rsc_body)}"
                    )
                    extractor = SSOExtractor(
                        transport_request=client._request,
                        base_headers=client._base_headers,
                        cookie_jar=client._t.cookies,
                        debug=True,
                    )
                    sso = extractor.extract(
                        rsc_body, email=email, password=password, save=False
                    )
            except Exception as recover_err:  # noqa: BLE001
                print(f"[grok-build-auth] SSO recover failed: {recover_err}")

                # Current xAI create_account often returns only RSC chunks + CF cookies,
                # with no set-cookie JWT chain. Fall back to password CreateSession and
                # treat the returned session JWT as the sso cookie for sso_to_auth_json.
        if not sso:
            update(
                "fetching_sso",
                f"RSC has no sso chain; CreateSession password fallback [{ctx.ADAPTER_BUILD}]",
            )
            try:
                # Fresh turnstile for sign-in page improves CreateSession success.
                # Allow account propagation delay before first login attempt.
                ctx.time.sleep(2.0)
                signin_url = "https://accounts.x.ai/sign-in?redirect=grok-com"
                try:
                    signin_turnstile = _solve_turnstile(
                        signin_url, premium=(provider != "local")
                    )
                except Exception:
                    signin_turnstile = turnstile
                sso = client.obtain_session_via_password(
                    email=email,
                    password=password,
                    turnstile_token=signin_turnstile,
                    referer=signin_url,
                    retries=4,
                )
                # One more captcha + login if first CreateSession returned empty.
                if not sso:
                    try:
                        signin_turnstile = _solve_turnstile(signin_url, premium=False)
                        ctx.time.sleep(1.5)
                        sso = client.obtain_session_via_password(
                            email=email,
                            password=password,
                            turnstile_token=signin_turnstile,
                            referer=signin_url,
                            retries=2,
                        )
                    except Exception as cs2_err:  # noqa: BLE001
                        print(
                            f"[grok-build-auth] CreateSession second pass failed: {cs2_err}"
                        )
                print(
                    f"[grok-build-auth] CreateSession fallback sso="
                    f"{(sso[:60] if sso else None)}"
                )
            except Exception as cs_err:  # noqa: BLE001
                print(f"[grok-build-auth] CreateSession fallback failed: {cs_err}")

        print(f"[grok-build-auth] fetch_sso_token result: {sso[:60] if sso else None}")
        sess["sso"] = sso
        session_cookies = extract_cookies_from_auth_client(client)
        print(
            f"[grok-build-auth] session cookies after signup: "
            f"{sorted((session_cookies or {}).keys())}"
        )
        if sso:
            session_cookies = dict(session_cookies or {})
            session_cookies["sso"] = sso
            session_cookies["sso-rw"] = sso

        if not sso:
            raise RuntimeError(
                "SSO_COOKIE_MISSING after create_account. "
                f"adapter_build={ctx.ADAPTER_BUILD}; HTTP {http_status}; "
                f"create_ok={bool(getattr(res, 'ok', False))}; "
                f"signup_error={signup_err!r}; set_cookies={len(sc)}; "
                f"cookie_keys={sorted((session_cookies or {}).keys())}; "
                f"body_preview={rsc_preview!r}. "
                "Account may have been created, but neither RSC set-cookie chain "
                "nor CreateSession password fallback produced an sso cookie. "
                "Common causes: turnstile_failed, rate_limited, or account not yet "
                "visible to CreateSession."
            )

            # Required path: SSO/session JWT -> sso_to_auth_json device flow -> auth.json
        update(
            "importing",
            f"SSO obtained; converting via sso_to_auth_json [{ctx.ADAPTER_BUILD}]",
        )
        import sso_to_auth_json as sso_import

        conversion_failure: dict[str, Any] = {}
        token = sso_import.sso_to_token(
            sso,
            failure=conversion_failure,
            should_cancel=_check_cancel,
        )
        if not token or not token.get("access_token"):
            stage_labels = {
                "sso_validation": "SSO 校验",
                "device_code": "设备码申请",
                "verify": "设备授权验证",
                "approve": "设备授权确认",
                "token_poll": "令牌获取",
            }
            reason_labels = {
                "rate_limited": "上游限流",
                "network_timeout": "网络超时",
                "invalid_sso": "SSO 已失效",
                "upstream_rejected": "上游拒绝请求",
            }
            stage = str(conversion_failure.get("stage") or "device_flow")
            reason = str(conversion_failure.get("reason") or "upstream_rejected")
            detail = str(conversion_failure.get("detail") or "未返回可用令牌")
            retryable = bool(conversion_failure.get("retryable"))
            if retryable:
                ctx._note_reg_pressure(f"device-flow {reason}", pause_sec=10)
            raise RuntimeError(
                "SSO 已获取，但转换失败："
                f"{stage_labels.get(stage, stage)}（{reason_labels.get(reason, reason)}）；"
                f"上游信息：{detail}"
                + ("；该 SSO 已保留，可稍后仅重试转换" if retryable else "")
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
        pipeline_cfg = dict(sess.get("_post_registration") or {})
        output_target = (
            str(
                pipeline_cfg.get("output_format") or pipeline_cfg.get("target") or "cpa"
            )
            .strip()
            .lower()
        )
        output_format = "sub2api" if output_target == "sub2api" else "cpa"
        import_result = accounts.import_auth_payload(
            auth_payload,
            merge=True,
            output_format=output_format,
        )
        if not import_result.get("ok"):
            raise RuntimeError(
                f"SSO account import failed: {import_result.get('error')}; "
                f"adapter_build={ctx.ADAPTER_BUILD}"
            )
            # Standalone build persists one auth file per account plus data/auth.json.
        if import_result.get("storage") != "filesystem":
            print(
                f"[grok-build-auth] WARN: unexpected standalone import storage="
                f"{import_result.get('storage')}"
            )
        imported_rows = [
            x for x in (import_result.get("imported") or []) if isinstance(x, dict)
        ]
        imported_ids = [str(x.get("id")) for x in imported_rows if x.get("id")]
        imported_accounts = [
            {"id": x.get("id"), "email": x.get("email") or email}
            for x in imported_rows
            if x.get("id") or x.get("email")
        ]
        sess["auth_json"] = import_result
        sess["imported_account_ids"] = imported_ids
        sess["imported_accounts"] = imported_accounts
        sess["oauth"] = {
            "path": "sso_to_auth_json",
            "access_token": (token.get("access_token") or "")[:20] + "...",
            "refresh_token": bool(token.get("refresh_token")),
            "email": email,
        }
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
            ctx._append_session_event(
                cur, cur["status"], cur["message"], at=cur["updated_at"]
            )
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
            update("error", f"failed: {exc}", error=str(exc))
        except ctx._RegPaused as paused_exc:
            with ctx._lock:
                cur = ctx._sessions.get(sid) or sess
                cur["status"] = "paused"
                cur["message"] = str(paused_exc) or "paused by user"
                cur["error"] = None
                cur["pause_requested"] = True
                cur["cancel_requested"] = False
                cur["updated_at"] = ctx._now()
                ctx._append_session_event(
                    cur, cur["status"], cur["message"], at=cur["updated_at"]
                )
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
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        with ctx._lock:
            final_sess = dict(ctx._sessions.get(sid) or sess or {})
            if sid in ctx._sessions:
                ctx._sessions[sid].pop("_receiver", None)
                ctx._sessions[sid].pop("_client", None)
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
                            or "xAI 注册失败"
                        ),
                    )
            except Exception:
                pass
                # Single sessions write their own terminal task log. Batch sessions are
                # summarized once by the batch finalizer (avoids N noise rows).
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
