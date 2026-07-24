"""Single-account ChatGPT browser registration workflow."""

from __future__ import annotations

from typing import Any, Callable

from chatgpt_browser_context import BrowserContext


def register_chatgpt_account(
    ctx,
    *,
    email,
    password,
    get_verification_code,
    proxy=None,
    headless=True,
    timeout_sec=300.0,
    should_cancel=None,
    on_progress=None,
    browser_data_dir=None,
    operation_delay_ms=3000,
    browser_runtime=None,
):
    """Register a new ChatGPT account via browser automation.

    Parameters
    ----------
    email : str
        Email address to register.
    get_verification_code : callable
        Blocking callback that returns the verification code from email (or None on timeout).
        Will be called repeatedly until it returns a non-empty string.
    proxy : dict | None
        Proxy config like {"server": "http://127.0.0.1:7890"} for the browser context.
    headless : bool
        Run browser headless (True) or visible (False).
    timeout_sec : float
        Total flow timeout in seconds.
    should_cancel : callable | None
        Called periodically; if returns True, cancels the registration.
    on_progress : callable | None
        Called with status messages for UI updates.
    browser_data_dir : str | None
        Persistent browser data directory for profile isolation.

    Returns
    -------
    dict with keys: ok, session, email, error, steps
    """
    ctx._ensure_camoufox()
    deadline = ctx.time.time() + timeout_sec
    steps: list[dict[str, Any]] = []

    def _cancel_check() -> None:
        if should_cancel and should_cancel():
            raise ctx.ChatGPTRegistrationCancelled("registration cancelled by user")
        if ctx.time.time() > deadline:
            raise ctx.ChatGPTRegistrationError("registration timed out")

    def _step(name: str, status: str = "ok", **extra: Any) -> None:
        entry = {"step": name, "status": status, "at": ctx.time.time(), **extra}
        steps.append(entry)
        msg = f"[chatgpt] {name}: {status}"
        if extra:
            detail = " ".join((f"{k}={v}" for k, v in extra.items() if v))
            if detail:
                msg += f" ({detail})"
        if on_progress:
            try:
                on_progress(msg)
            except Exception:
                pass

    action_delay_ms = max(0, min(30000, int(operation_delay_ms or 0)))

    def _action_delay() -> None:
        """Cancellable delay before each direct browser operation."""
        remaining = action_delay_ms
        while remaining > 0:
            _cancel_check()
            chunk = min(200, remaining)
            if page is not None:
                page.wait_for_timeout(chunk)
            else:
                ctx.time.sleep(chunk / 1000.0)
            remaining -= chunk

    def _hold_visible_failure() -> None:
        """Keep a failed visible browser open briefly for visual diagnosis."""
        if headless or page is None:
            return
        try:
            _step("flow", "debug_hold", seconds=20)
            page.wait_for_timeout(20000)
        except Exception:
            pass

    def _enforce_visible_window() -> None:
        if headless or page is None:
            return
        try:
            page.set_viewport_size({"width": 760, "height": 480})
        except Exception:
            pass
        try:
            page.evaluate(
                "() => {\n                try { window.resizeTo(800, 560); } catch(e) {}\n                try { window.moveTo(24, 24); } catch(e) {}\n            }"
            )
        except Exception:
            pass

    owned_runtime = browser_runtime is None
    runtime = browser_runtime or ctx.ChatGPTBrowserRuntime()
    browser = None
    context = None
    page = None
    using_camoufox = False
    try:
        _step("init", "starting")
        _cancel_check()
        browser, using_camoufox, reused_browser = runtime.ensure(
            headless=headless,
            proxy=proxy,
            on_launched=lambda status: _step("init", status),
        )
        if reused_browser:
            _step("init", "reused_browser")
        _cancel_check()
        context_kwargs: dict[str, Any] = (
            {"no_viewport": True}
            if using_camoufox
            else {
                "viewport": {
                    "width": 760 if not headless else 1280,
                    "height": 480 if not headless else 800,
                },
                "locale": "en-US",
                "timezone_id": "America/New_York",
            }
        )
        if not using_camoufox and proxy and proxy.get("server"):
            context_kwargs["proxy"] = proxy
        if browser_data_dir:
            context_kwargs["storage_state"] = None
        context = browser.new_context(**context_kwargs)
        _step("init", "private_context_created")
        page = context.new_page()
        _enforce_visible_window()
        _step("navigate", "loading")
        _cancel_check()
        _action_delay()
        page.goto(
            "https://chatgpt.com/auth/login",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        page.wait_for_timeout(2000)
        _enforce_visible_window()
        _step("signup_page", "finding_signup")
        _cancel_check()
        signup_selectors = [
            'a[href*="signup"]',
            'button:has-text("Sign up")',
            'a:has-text("Sign up")',
            "text=Sign up",
            'button[data-testid="signup-button"]',
        ]
        _action_delay()
        if not ctx._find_and_click(page, signup_selectors, timeout=10):
            _step("signup_page", "already_on_signup")
        page.wait_for_timeout(2000)
        _step("email", "filling")
        _cancel_check()
        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            "input#email",
            'input[placeholder*="email" i]',
            'input[placeholder*="Email" i]',
        ]
        _action_delay()
        if not ctx._find_and_fill_input(page, email_selectors, email, timeout=10):
            raise ctx.ChatGPTRegistrationError("找不到邮箱输入框")
        _step("email", "filled", email=email)
        _cancel_check()
        email_input = ctx._first_visible(page, email_selectors)
        _action_delay()
        if not ctx._submit_for_element(
            page, email_input, "^continue$|^next$|^submit$|继续|下一步"
        ):
            if not email_input:
                raise ctx.ChatGPTRegistrationError("邮箱输入框在提交前消失")
            email_input.press("Enter")
        page.wait_for_timeout(3000)
        _step("email", "submitted")
        new_password_selectors = [
            'input[name="new-password"]',
            'input[autocomplete="new-password"]',
            'input[type="password"][placeholder*="password" i]',
        ]
        first_name, last_name = ctx._random_name()
        birthdate = ctx._random_birthdate()
        profile_filled = False
        password_submit_attempts = 0

        def _fill_and_submit_password() -> None:
            nonlocal password_submit_attempts
            if not password:
                raise ctx.ChatGPTRegistrationError(
                    "ChatGPT 要求设置密码，但注册密码为空"
                )
            if password_submit_attempts >= 3:
                raise ctx.ChatGPTRegistrationError(
                    f"密码页连续提交失败: {ctx._safe_page_text(page)[:250]}"
                )
            password_submit_attempts += 1
            _step("password", "filling", attempt=password_submit_attempts)
            _action_delay()
            if not ctx._find_and_fill_input(
                page, new_password_selectors, password, timeout=10
            ):
                raise ctx.ChatGPTRegistrationError("找不到新账号密码输入框")
            password_input = ctx._first_visible(page, new_password_selectors)
            _step("password", "filled")
            _cancel_check()
            _action_delay()
            if not ctx._submit_for_element(
                page, password_input, "continue|sign\\s*up|submit|create|继续|创建"
            ):
                password_input.press("Enter")
            _step("password", "submitted", attempt=password_submit_attempts)
            page.wait_for_timeout(1200)

        state = ctx._wait_for_signup_state(page, _cancel_check, 20)
        while state in {"new_password", "retry"}:
            if state == "retry":
                _step("recovery", "auth_retry")
                if not ctx._recover_retry_page(page, _cancel_check):
                    raise ctx.ChatGPTRegistrationError(
                        f"认证重试页自动恢复失败: {page.url}"
                    )
            else:
                _fill_and_submit_password()
            state = ctx._wait_for_signup_state(page, _cancel_check, 12)
        if state == "security_blocked":
            raise ctx.ChatGPTRegistrationError(
                "已触发 OpenAI/Cloudflare 临时安全限制，请等待 15-30 分钟或更换代理后重试"
            )
        if state == "existing_account":
            raise ctx.ChatGPTRegistrationError(f"邮箱 {email} 已存在 ChatGPT 账号")
        if state == "account_deactivated":
            raise ctx.ChatGPTRegistrationError(
                f"OpenAI 账户错误（account_deactivated）：该邮箱关联的账号已删除或停用，无法继续注册，请更换邮箱账号：{email}"
            )
        if state == "add_phone":
            raise ctx.ChatGPTRegistrationError(
                "OpenAI 要求绑定手机号，当前项目未配置短信验证流程"
            )
        if state == "external_idp":
            raise ctx.ChatGPTRegistrationError(
                f"邮箱被路由到外部 SSO 登录页面，不能用于密码注册: {page.url}"
            )
        if state == "unknown":
            raise ctx.ChatGPTRegistrationError(
                f"提交邮箱和密码后页面状态未知: {ctx._safe_page_text(page)[:250] or page.url}"
            )
        if state == "verification":
            _step("verification", "waiting_for_form")
            verification_target = None
            verification_deadline = ctx.time.time() + 20
            while ctx.time.time() < verification_deadline:
                _cancel_check()
                verification_target = ctx._verification_target(page)
                if verification_target:
                    break
                if ctx._signup_state(page) == "retry" and (
                    not ctx._recover_retry_page(page, _cancel_check)
                ):
                    break
                page.wait_for_timeout(200)
            if not verification_target:
                raise ctx.ChatGPTRegistrationError(
                    f"未找到验证码输入框: {ctx._safe_page_text(page)[:250] or page.url}"
                )
            if ctx._profile_visible(page):
                _step("profile", "prefilling_combined", birthdate=birthdate["iso"])
                _action_delay()
                ctx._fill_profile_fields(page, first_name, last_name, birthdate)
                profile_filled = True

            def _read_code(force_refresh: bool = False) -> str | None:
                try:
                    return get_verification_code(force_refresh)
                except TypeError:
                    try:
                        return get_verification_code()
                    except Exception as exc:
                        raise ctx.ChatGPTRegistrationError(
                            f"邮箱验证码读取失败: {str(exc)[:240]}"
                        ) from exc
                except Exception as exc:
                    raise ctx.ChatGPTRegistrationError(
                        f"邮箱验证码读取失败: {str(exc)[:240]}"
                    ) from exc

            verification_attempt = 0
            while verification_attempt < 2:
                verification_attempt += 1
                _cancel_check()
                _step("verification", "waiting_for_code", attempt=verification_attempt)
                code = _read_code(verification_attempt > 1)
                if not code:
                    raise ctx.ChatGPTRegistrationError("邮箱验证码在 180 秒内未送达")
                code = ctx.re.sub("[^0-9]", "", str(code))
                if len(code) != 6:
                    raise ctx.ChatGPTRegistrationError(f"无效的验证码格式: {code!r}")
                _step(
                    "verification",
                    "code_received",
                    code=code,
                    code_len=len(code),
                    attempt=verification_attempt,
                )
                current_target = None
                target_deadline = ctx.time.time() + 5
                while ctx.time.time() < target_deadline:
                    current_target = ctx._verification_target(page)
                    if current_target:
                        break
                    page.wait_for_timeout(150)
                if not current_target:
                    raise ctx.ChatGPTRegistrationError(
                        f"验证码输入框在填写前消失: {ctx._safe_page_text(page)[:250]}"
                    )
                verification_target = current_target
                kind, target = current_target
                _action_delay()
                if kind == "split":
                    for index, digit in enumerate(code[: len(target)]):
                        target[index].fill(digit)
                    submit_anchor = target[0]
                else:
                    target.fill(code)
                    submit_anchor = target
                _step("verification", "code_filled", mode=kind)
                _cancel_check()
                page.wait_for_timeout(800)
                _action_delay()
                if not ctx._submit_for_element(
                    page, submit_anchor, "verify|confirm|submit|continue|确认|验证|继续"
                ):
                    submit_anchor.press("Enter")
                _step("verification", "code_submitted", attempt=verification_attempt)
                _step("verification", "waiting_for_result", timeout_sec=60)
                outcome_deadline = ctx.time.time() + 60
                while ctx.time.time() < outcome_deadline:
                    _cancel_check()
                    next_state = ctx._signup_state(page)
                    text = ctx._safe_page_text(page)
                    if ctx.re.search(
                        "invalid\\s+code|incorrect\\s+code|code.*incorrect|验证码.*(?:错误|不正确)",
                        text,
                        ctx.re.I,
                    ):
                        next_state = "invalid_code"
                    if next_state not in {"verification", "unknown"}:
                        break
                    page.wait_for_timeout(200)
                else:
                    next_state = ctx._signup_state(page)
                if next_state in {
                    "verification",
                    "unknown",
                } and ctx._verification_submission_pending(page):
                    _step("verification", "still_processing")
                    processing_deadline = ctx.time.time() + 15
                    while ctx.time.time() < processing_deadline:
                        _cancel_check()
                        next_state = ctx._signup_state(page)
                        if next_state not in {"verification", "unknown"}:
                            break
                        if not ctx._verification_submission_pending(page):
                            break
                        page.wait_for_timeout(250)
                if next_state == "invalid_code" or next_state == "verification":
                    if verification_attempt >= 2:
                        raise ctx.ChatGPTRegistrationError(
                            f"验证码提交后仍停留在验证页: {ctx._safe_page_text(page)[:250]}"
                        )
                    resend = None
                    resend_deadline = ctx.time.time() + 20
                    while ctx.time.time() < resend_deadline:
                        _cancel_check()
                        current_state = ctx._signup_state(page)
                        if current_state not in {"verification", "unknown"}:
                            next_state = current_state
                            break
                        resend = ctx._find_action(
                            page,
                            "resend|send.*again|new\\s+code|try\\s+again|重新发送|重发|重新获取|再次发送|发送新代码",
                        )
                        if resend:
                            break
                        page.wait_for_timeout(250)
                    if next_state not in {"invalid_code", "verification", "unknown"}:
                        state = next_state
                        break
                    if not resend:
                        raise ctx.ChatGPTRegistrationError(
                            f"验证码提交后页面长时间未继续，且未出现重新发送按钮: {ctx._safe_page_text(page)[:250]}"
                        )
                    _action_delay()
                    resend.click()
                    page.wait_for_timeout(1500)
                    verification_target = (
                        ctx._verification_target(page) or verification_target
                    )
                    _step("verification", "resent")
                    continue
                if next_state == "retry":
                    if not ctx._recover_retry_page(page, _cancel_check):
                        raise ctx.ChatGPTRegistrationError(
                            "验证码提交后认证重试页恢复失败"
                        )
                    next_state = ctx._wait_for_signup_state(page, _cancel_check, 15)
                state = next_state
                break
        _step("completion", "waiting")
        completion_deadline = ctx.time.time() + 120
        profile_submit_attempts = 0
        last_profile_submit = 0.0
        while ctx.time.time() < completion_deadline:
            _cancel_check()
            state = ctx._signup_state(page)
            if state == "logged_in":
                break
            if state == "security_blocked":
                raise ctx.ChatGPTRegistrationError(
                    "已触发 OpenAI/Cloudflare 临时安全限制，请稍后重试"
                )
            if state == "existing_account":
                raise ctx.ChatGPTRegistrationError(f"邮箱 {email} 已存在 ChatGPT 账号")
            if state == "account_deactivated":
                raise ctx.ChatGPTRegistrationError(
                    f"OpenAI 账户错误（account_deactivated）：该邮箱关联的账号已删除或停用，无法继续注册，请更换邮箱账号：{email}"
                )
            if state == "add_phone":
                raise ctx.ChatGPTRegistrationError(
                    "注册后被要求绑定手机号，当前项目未配置短信验证流程"
                )
            if state == "external_idp":
                raise ctx.ChatGPTRegistrationError(
                    f"注册流程被重定向到外部 SSO: {page.url}"
                )
            if state == "retry":
                if not ctx._recover_retry_page(page, _cancel_check):
                    raise ctx.ChatGPTRegistrationError(
                        f"资料提交后的重试页恢复失败: {page.url}"
                    )
                continue
            if state == "new_password":
                _fill_and_submit_password()
                continue
            if state == "passkey":
                _step("passkey", "skipping")
                _action_delay()
                if not ctx._skip_passkey(page):
                    raise ctx.ChatGPTRegistrationError(
                        f"进入通行密钥页面但未找到跳过按钮: {page.url}"
                    )
                continue
            if state == "profile":
                if not profile_filled:
                    _step("profile", "filling", birthdate=birthdate["iso"])
                    _action_delay()
                    ctx._fill_profile_fields(page, first_name, last_name, birthdate)
                    profile_filled = True
                    _cancel_check()
                if profile_submit_attempts == 0:
                    submit_deadline = ctx.time.time() + 5
                    submit_button = None
                    while ctx.time.time() < submit_deadline:
                        _cancel_check()
                        submit_button = ctx._profile_submit_button(page)
                        if ctx._action_clickable(submit_button):
                            break
                        page.wait_for_timeout(150)
                    if not ctx._action_clickable(submit_button):
                        raise ctx.ChatGPTRegistrationError(
                            f"未找到完成账号创建按钮: {page.url}"
                        )
                    profile_submit_attempts += 1
                    _action_delay()
                    submit_button.click()
                    last_profile_submit = ctx.time.time()
                    _step("profile", "submitted", attempt=profile_submit_attempts)
                elif (
                    profile_submit_attempts < 3
                    and ctx.time.time() - last_profile_submit >= 3.5
                ):
                    submit_button = ctx._profile_submit_button(page)
                    if ctx._action_clickable(submit_button):
                        profile_submit_attempts += 1
                        _action_delay()
                        submit_button.click()
                        last_profile_submit = ctx.time.time()
                        _step("profile", "submitted", attempt=profile_submit_attempts)
            text = ctx._safe_page_text(page)
            if ctx.re.search(
                "unsupported_email|email you provided is not supported|不支持.*邮箱",
                text,
                ctx.re.I,
            ):
                raise ctx.ChatGPTRegistrationError(
                    f"OpenAI 不支持当前邮箱地址或域名，账号未创建成功: {email}"
                )
            if ctx.re.search(
                "unable to create|couldn.t create|invalid birthday|无法.*创建|生日.*错误",
                text,
                ctx.re.I,
            ):
                raise ctx.ChatGPTRegistrationError(f"资料提交失败: {text[:250]}")
            page.wait_for_timeout(250)
        else:
            raise ctx.ChatGPTRegistrationError(
                f"等待注册完成超时: {ctx._safe_page_text(page)[:250] or page.url}"
            )
        _step("completion", "done", url=page.url[:100])
        _action_delay()
        _step("session", "extracting")
        _cancel_check()
        session = None
        session_error = None
        try:
            resp = context.request.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            if resp.status == 200:
                try:
                    session = resp.json()
                except Exception:
                    session_text = resp.text()
                    try:
                        session = ctx.json.loads(session_text)
                    except Exception:
                        session_error = (
                            f"session response not JSON: {session_text[:200]}"
                        )
            else:
                session_error = f"session endpoint returned HTTP {resp.status}"
        except Exception as e:
            session_error = f"session request failed: {e}"
        if not session or not session.get("accessToken"):
            try:
                if ctx.urlparse(str(page.url or "")).hostname not in {
                    "chatgpt.com",
                    "www.chatgpt.com",
                }:
                    _action_delay()
                    page.goto(
                        "https://chatgpt.com/",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_timeout(2000)
                session_raw = page.evaluate(
                    "async () => {\n                    try {\n                        const resp = await fetch('/api/auth/session', {\n                            credentials: 'include',\n                            cache: 'no-store'\n                        });\n                        return await resp.json();\n                    } catch(e) {\n                        return null;\n                    }\n                }"
                )
                if session_raw and isinstance(session_raw, dict):
                    session = session_raw
                    session_error = None
            except Exception:
                pass
        if not session or not session.get("accessToken"):
            _step("session", "failed", error=session_error)
            raise ctx.ChatGPTRegistrationError(
                f"ChatGPT Session 中缺少 accessToken: {session_error or '请确认注册后已完成登录'}"
            )
        _step("session", "extracted")
        return {
            "ok": True,
            "email": email,
            "session": session,
            "name": f"{first_name} {last_name}",
            "steps": steps,
        }
    except ctx.ChatGPTRegistrationCancelled:
        _step("flow", "cancelled")
        return {
            "ok": False,
            "email": email,
            "error": "cancelled",
            "cancelled": True,
            "steps": steps,
        }
    except ctx.ChatGPTRegistrationError as e:
        _step("flow", "error", error=str(e)[:200])
        _hold_visible_failure()
        return {"ok": False, "email": email, "error": str(e), "steps": steps}
    except Exception as e:
        _step(
            "flow",
            "exception",
            error=str(e)[:200],
            traceback=ctx.traceback.format_exc()[-500:],
        )
        _hold_visible_failure()
        return {
            "ok": False,
            "email": email,
            "error": f"浏览器注册异常: {e}",
            "steps": steps,
        }
    finally:
        try:
            if page:
                page.evaluate(
                    "async () => {\n                    try { localStorage.clear(); } catch(e) {}\n                    try { sessionStorage.clear(); } catch(e) {}\n                    try {\n                        if ('caches' in window) {\n                            const keys = await caches.keys();\n                            await Promise.all(keys.map(key => caches.delete(key)));\n                        }\n                    } catch(e) {}\n                }"
                )
        except Exception:
            pass
        try:
            if context:
                context.clear_cookies()
        except Exception:
            pass
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        if owned_runtime:
            runtime.close()
