"""Browser-only xAI account registration and OAuth consent flow."""
from __future__ import annotations

import re
import time
from typing import Any, Callable


XAI_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
XAI_GROK_URL = "https://grok.com/"


def _ensure_camoufox() -> None:
    try:
        from camoufox.sync_api import Camoufox

        del Camoufox
    except ImportError as exc:
        raise RuntimeError("xAI 浏览器注册需要 Camoufox，请先安装项目依赖") from exc


class XaiBrowserError(RuntimeError):
    """Raised when xAI browser registration or consent cannot continue."""


class XaiBrowserCancelled(XaiBrowserError):
    """Raised when the owning registration task is cancelled."""


class XaiBrowserRuntime:
    """Reusable browser process; every account gets a private context."""

    def __init__(self) -> None:
        self.browser: Any = None
        self.camoufox_manager: Any = None
        self.using_camoufox = False
        self.headless = True
        self.proxy_key = ""

    @staticmethod
    def _key(proxy: dict[str, str] | None) -> str:
        if not proxy:
            return ""
        return "|".join(
            str(proxy.get(key) or "") for key in ("server", "username", "password")
        )

    def ensure(
        self,
        *,
        headless: bool,
        proxy: dict[str, str] | None,
        on_launched: Callable[[str], None] | None = None,
    ) -> tuple[Any, bool, bool]:
        key = self._key(proxy)
        browser_alive = self.browser is not None
        try:
            if browser_alive and hasattr(self.browser, "is_connected"):
                browser_alive = bool(self.browser.is_connected())
        except Exception:
            browser_alive = False
        if browser_alive and self.headless == headless and self.proxy_key == key:
            self.proxy_key = key
            return self.browser, self.using_camoufox, True

        self.close()
        _ensure_camoufox()
        self.headless = headless
        self.proxy_key = key
        width, height = 800, 560
        try:
            from camoufox.sync_api import Camoufox

            options: dict[str, Any] = {"headless": headless}
            if not headless:
                options["window"] = (width, height)
            if proxy and proxy.get("server"):
                options["proxy"] = proxy
                options["geoip"] = True
            self.camoufox_manager = Camoufox(**options)
            self.browser = self.camoufox_manager.start()
            self.using_camoufox = True
            if on_launched:
                on_launched("launched_camoufox")
        except Exception as exc:
            try:
                if self.camoufox_manager:
                    self.camoufox_manager.__exit__(None, None, None)
            except Exception:
                pass
            self.camoufox_manager = None
            self.browser = None
            raise XaiBrowserError(f"Camoufox 启动失败：{exc}") from exc
        return self.browser, self.using_camoufox, False

    def close(self) -> None:
        try:
            if self.camoufox_manager:
                self.camoufox_manager.__exit__(None, None, None)
            elif self.browser:
                self.browser.close()
        except Exception:
            pass
        self.browser = None
        self.camoufox_manager = None
        self.using_camoufox = False
        self.proxy_key = ""


class XaiVisibleRegistration:
    """One isolated browser context covering signup and OAuth approval."""

    def __init__(
        self,
        *,
        runtime: XaiBrowserRuntime | None = None,
        proxy: dict[str, str] | None = None,
        headless: bool = True,
        on_progress: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        operation_delay_ms: int = 0,
        timeout_sec: float = 300.0,
    ) -> None:
        self.runtime = runtime or XaiBrowserRuntime()
        self.owns_runtime = runtime is None
        self.proxy = proxy
        self.headless = headless
        self.on_progress = on_progress
        self.should_cancel = should_cancel
        self.operation_delay_ms = max(0, min(30_000, int(operation_delay_ms or 0)))
        self.deadline = time.time() + max(30.0, float(timeout_sec))
        self.context: Any = None
        self.page: Any = None
        self.using_camoufox = False
        self.steps: list[dict[str, Any]] = []

    def _progress(self, step: str, state: str, **extra: Any) -> None:
        self.steps.append({"step": step, "status": state, "at": time.time(), **extra})
        if not self.on_progress:
            return
        message = f"[xai] {step}: {state}"
        detail = " ".join(
            f"{key}={value}" for key, value in extra.items() if value not in (None, "")
        )
        if detail:
            message += f" ({detail})"
        try:
            self.on_progress(message)
        except XaiBrowserCancelled:
            raise
        except Exception:
            pass

    def _check_cancel(self) -> None:
        if self.should_cancel and self.should_cancel():
            raise XaiBrowserCancelled("registration cancelled by user")
        if time.time() > self.deadline:
            raise XaiBrowserError("xAI 浏览器注册超时")

    def _wait(self, milliseconds: int) -> None:
        remaining = max(0, int(milliseconds))
        while remaining > 0:
            self._check_cancel()
            chunk = min(200, remaining)
            if self.page is not None:
                self.page.wait_for_timeout(chunk)
            else:
                time.sleep(chunk / 1000.0)
            remaining -= chunk

    def _action_delay(self) -> None:
        self._wait(self.operation_delay_ms)

    def _enforce_visible_window(self) -> None:
        if self.headless or self.page is None:
            return
        try:
            self.page.set_viewport_size({"width": 760, "height": 480})
        except Exception:
            pass
        try:
            self.page.evaluate(
                """() => {
                    try { window.resizeTo(800, 560); } catch (e) {}
                    try { window.moveTo(24, 24); } catch (e) {}
                }"""
            )
        except Exception:
            pass

    def open(self) -> None:
        """Open the real signup page in a clean private context."""
        try:
            self._progress("init", "starting")
            browser, self.using_camoufox, reused = self.runtime.ensure(
                headless=self.headless,
                proxy=self.proxy,
                on_launched=lambda state: self._progress("init", state),
            )
            if reused:
                self._progress("init", "reused_browser")
            context_kwargs: dict[str, Any]
            if self.using_camoufox:
                context_kwargs = {"no_viewport": True}
            else:
                context_kwargs = {
                    "viewport": {
                        "width": 760 if not self.headless else 1280,
                        "height": 480 if not self.headless else 800,
                    },
                    "locale": "en-US",
                    "timezone_id": "America/New_York",
                }
                if self.proxy and self.proxy.get("server"):
                    context_kwargs["proxy"] = self.proxy
            self.context = browser.new_context(**context_kwargs)
            self._progress("init", "private_context_created")
            self.page = self.context.new_page()
            self._enforce_visible_window()
            self._progress("navigate", "loading")
            self._action_delay()
            self.page.goto(XAI_SIGNUP_URL, wait_until="domcontentloaded", timeout=45_000)
            self._wait(800)
            self._enforce_visible_window()
            self._progress("navigate", "ready")
        except (XaiBrowserCancelled, XaiBrowserError):
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise XaiBrowserError(f"xAI 浏览器启动失败：{exc}") from exc

    def sync_cookies(
        self, cookies: dict[str, str] | None, *, authenticated: bool = False
    ) -> None:
        """Compatibility helper for restoring a previously saved browser session."""
        if not self.context or not isinstance(cookies, dict):
            return
        clean = {
            str(name): str(value)
            for name, value in cookies.items()
            if str(name).strip() and value is not None and str(value)
        }
        payloads: list[dict[str, str]] = []
        for url in ("https://accounts.x.ai", "https://grok.com"):
            payloads.extend(
                {"name": name, "value": value, "url": url}
                for name, value in clean.items()
            )
        if payloads:
            self.context.add_cookies(payloads)
            self._progress("session" if authenticated else "init", "cookies_synced")

    def _first_visible(self, selectors: list[str]) -> Any | None:
        if self.page is None:
            return None
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                for index in range(min(10, int(locator.count()))):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
            except Exception:
                continue
        return None

    def _fill_first(self, selectors: list[str], value: str) -> Any | None:
        target = self._first_visible(selectors)
        if target is None or not value:
            return None
        target.fill(value, timeout=2_000)
        return target

    def _page_text(self) -> str:
        if self.page is None:
            return ""
        try:
            return str(self.page.locator("body").inner_text(timeout=1_500) or "")
        except Exception:
            return ""

    def _find_action(self, pattern: str) -> Any | None:
        if self.page is None:
            return None
        regex = re.compile(pattern, re.I)
        for role in ("button", "link"):
            try:
                locator = self.page.get_by_role(role, name=regex)
                for index in range(min(10, int(locator.count()))):
                    candidate = locator.nth(index)
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
            except Exception:
                continue
        try:
            submit = self.page.locator('button[type="submit"], input[type="submit"]')
            for index in range(min(10, int(submit.count()))):
                candidate = submit.nth(index)
                if candidate.is_visible() and candidate.is_enabled():
                    return candidate
        except Exception:
            pass
        return None

    def _click_action(self, pattern: str) -> bool:
        candidate = self._find_action(pattern)
        if candidate is None:
            return False
        candidate.click(timeout=3_000)
        return True

    def _submit(self, anchor: Any | None, pattern: str) -> None:
        self._action_delay()
        if self._click_action(pattern):
            return
        if anchor is None:
            raise XaiBrowserError("xAI 页面没有可用的继续按钮")
        anchor.press("Enter")

    def _verification_target(self) -> tuple[str, Any] | None:
        if self.page is None:
            return None
        try:
            split = self.page.locator('input[maxlength="1"]')
            visible = [
                split.nth(i)
                for i in range(min(10, int(split.count())))
                if split.nth(i).is_visible()
            ]
            if len(visible) >= 6:
                return "split", visible
        except Exception:
            pass
        target = self._first_visible(
            [
                'input[name*="code" i]',
                'input[autocomplete="one-time-code"]',
                'input[inputmode="numeric"]',
                'input[maxlength="6"]',
            ]
        )
        return ("single", target) if target is not None else None

    def _extract_sso(self) -> tuple[str | None, dict[str, str]]:
        if self.context is None:
            return None, {}
        try:
            rows = self.context.cookies()
        except TypeError:
            rows = self.context.cookies(["https://accounts.x.ai", "https://grok.com"])
        cookies = {
            str(row.get("name") or ""): str(row.get("value") or "")
            for row in (rows or [])
            if isinstance(row, dict) and row.get("name") and row.get("value")
        }
        return cookies.get("sso") or cookies.get("sso-rw"), cookies

    def _raise_page_error(self, email: str) -> None:
        text = self._page_text()
        lowered = text.lower()
        if any(
            marker in lowered
            for marker in (
                "attention required",
                "sorry, you have been blocked",
                "access denied",
            )
        ):
            raise XaiBrowserError("xAI/Cloudflare 拒绝了当前浏览器或代理")
        if re.search(r"email.*already|account.*already|already.*registered", text, re.I):
            raise XaiBrowserError(f"邮箱 {email} 已存在 xAI 账号")
        if re.search(r"unsupported.*email|email.*not supported", text, re.I):
            raise XaiBrowserError(f"xAI 不支持当前邮箱地址：{email}")
        if re.search(r"rate.?limit|too many requests|try again later", text, re.I):
            raise XaiBrowserError("xAI 上游限流，请稍后重试")

    def register_account(
        self,
        *,
        email: str,
        password: str,
        get_verification_code: Callable[..., str | None],
    ) -> dict[str, Any]:
        """Complete signup through the page and return its authenticated cookies."""
        self.open()
        assert self.page is not None
        acted: set[str] = set()
        used_codes: set[str] = set()
        last_code = ""
        while True:
            self._check_cancel()
            sso, cookies = self._extract_sso()
            if sso:
                self._progress("completion", "done", url=str(self.page.url)[:100])
                return {
                    "ok": True,
                    "email": email,
                    "sso": sso,
                    "cookies": cookies,
                    "steps": list(self.steps),
                }
            self._raise_page_error(email)

            if "signup_method" not in acted and re.search(
                r"sign up with email|使用邮箱.*注册|邮箱注册",
                self._page_text(),
                re.I,
            ):
                self._progress("signup_method", "selecting_email")
                self._action_delay()
                if not self._click_action(
                    r"^sign up with email$|使用邮箱.*注册|邮箱注册"
                ):
                    raise XaiBrowserError("未找到 xAI 邮箱注册入口")
                acted.add("signup_method")
                self._progress("signup_method", "selected")
                self._wait(800)
                continue

            verification = self._verification_target()
            if verification is not None:
                stage = "verification"
                if stage not in acted:
                    self._progress(stage, "waiting_for_code")
                    try:
                        code = get_verification_code(False)
                    except TypeError:
                        code = get_verification_code()
                    value = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
                    if len(value) != 6 or value in used_codes:
                        raise XaiBrowserError(f"无效或重复的 xAI 邮箱验证码：{value!r}")
                    used_codes.add(value)
                    last_code = value
                    kind, target = verification
                    self._progress(stage, "code_received", code=value)
                    self._action_delay()
                    if kind == "split":
                        for index, char in enumerate(value):
                            target[index].fill(char)
                        anchor = target[0]
                    else:
                        target.fill(value)
                        anchor = target
                    self._progress(stage, "code_filled")
                    self._submit(anchor, "verify|confirm|continue|submit|验证|确认|继续")
                    self._progress(stage, "submitted")
                    acted.add(stage)
                    self._wait(1_000)
                    continue

                text = self._page_text()
                if re.search(r"invalid|incorrect|expired|验证码.*(?:错误|过期)", text, re.I):
                    self._progress(stage, "retrying")
                    resend = self._click_action(
                        "resend|send.*again|new code|try again|重新发送|重发"
                    )
                    if not resend:
                        raise XaiBrowserError(f"xAI 验证码提交失败：{text[:240]}")
                    try:
                        code = get_verification_code(True)
                    except TypeError:
                        code = get_verification_code()
                    value = re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
                    if len(value) != 6 or value == last_code:
                        raise XaiBrowserError("xAI 未收到新的有效邮箱验证码")
                    acted.discard(stage)
                    self._wait(800)
                    continue

            email_input = self._first_visible(
                [
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[autocomplete="email"]',
                    'input[placeholder*="email" i]',
                ]
            )
            password_input = self._first_visible(
                [
                    'input[type="password"]',
                    'input[name*="password" i]',
                    'input[autocomplete="new-password"]',
                ]
            )
            first_name = self._first_visible(
                ['input[name*="first" i]', 'input[name*="given" i]']
            )
            last_name = self._first_visible(
                ['input[name*="last" i]', 'input[name*="family" i]']
            )

            stage_parts: list[str] = []
            anchor = None
            if email_input is not None:
                stage_parts.append("email")
                if "email" not in acted:
                    self._progress("email", "filling", email=email)
                    self._action_delay()
                    email_input.fill(email)
                    anchor = email_input
            if password_input is not None:
                stage_parts.append("password")
                if "password" not in acted:
                    self._progress("password", "filling")
                    self._action_delay()
                    password_input.fill(password)
                    anchor = password_input
            if first_name is not None or last_name is not None:
                stage_parts.append("profile")
                if "profile" not in acted:
                    self._progress("profile", "filling")
                    self._action_delay()
                    if first_name is not None:
                        first_name.fill("User")
                        anchor = first_name
                    if last_name is not None:
                        last_name.fill("Grok")
                        anchor = last_name

            pending = [part for part in stage_parts if part not in acted]
            if pending:
                self._submit(
                    anchor,
                    "continue|next|sign up|create account|submit|继续|下一步|创建",
                )
                for part in pending:
                    acted.add(part)
                    self._progress(part, "submitted")
                self._wait(1_000)
                continue

            if str(self.page.url or "").startswith(XAI_GROK_URL):
                self._wait(1_000)
                continue
            self._wait(250)

    def authorize_device(self, verify_url: str, user_code: str) -> None:
        """Approve an OAuth device request through the authenticated browser page."""
        if self.page is None:
            raise XaiBrowserError("xAI 授权浏览器未启动")
        self._progress("oauth", "opening_device_authorization")
        self._action_delay()
        self.page.goto(verify_url, wait_until="domcontentloaded", timeout=45_000)
        self._wait(600)
        submitted_code = False
        approved = False
        deadline = min(self.deadline, time.time() + 90.0)
        while time.time() < deadline:
            self._check_cancel()
            text = self._page_text()
            url = str(self.page.url or "")
            if "done" in url or re.search(
                r"device.*(?:authorized|approved)|authorization complete|you may close",
                text,
                re.I,
            ):
                self._progress("oauth", "approved")
                return
            if re.search(r"expired|invalid.*code|request.*denied", text, re.I):
                raise XaiBrowserError(f"xAI 设备授权失败：{text[:240]}")
            if not submitted_code:
                target = self._first_visible(
                    [
                        'input[name="user_code"]',
                        'input[name*="code" i]',
                        'input[autocomplete="one-time-code"]',
                    ]
                )
                if target is not None:
                    self._action_delay()
                    target.fill(user_code)
                    self._submit(target, "continue|verify|submit|继续|验证")
                    submitted_code = True
                    self._progress("oauth", "device_code_submitted")
                    self._wait(500)
                    continue
            if not approved:
                action = self._find_action(
                    "allow|authorize|approve|accept|confirm|允许|授权|同意|确认"
                )
                if action is not None:
                    self._action_delay()
                    action.click(timeout=3_000)
                    approved = True
                    self._progress("oauth", "approval_submitted")
                    self._wait(500)
                    continue
            self._wait(250)
        raise XaiBrowserError("xAI 设备授权页面等待超时")

    def hold_failure(self, *, seconds: float = 20.0) -> None:
        if self.headless or self.page is None:
            return
        self._progress("flow", "debug_hold", seconds=int(max(0, seconds)))
        try:
            self._wait(int(max(0.0, seconds) * 1000))
        except Exception:
            pass

    def close(self) -> None:
        """Clear account state before returning the browser process to its worker."""
        try:
            if self.page is not None:
                self.page.evaluate(
                    """async () => {
                        try { localStorage.clear(); } catch (e) {}
                        try { sessionStorage.clear(); } catch (e) {}
                        try {
                            const keys = await caches.keys();
                            await Promise.all(keys.map((key) => caches.delete(key)));
                        } catch (e) {}
                    }"""
                )
        except Exception:
            pass
        try:
            if self.context is not None:
                self.context.clear_cookies()
        except Exception:
            pass
        try:
            if self.page is not None:
                self.page.close()
        except Exception:
            pass
        try:
            if self.context is not None:
                self.context.close()
        except Exception:
            pass
        self.page = None
        self.context = None
        if self.owns_runtime:
            self.runtime.close()


def register_xai_account(
    *,
    email: str,
    password: str,
    get_verification_code: Callable[..., str | None],
    proxy: dict[str, str] | None = None,
    headless: bool = True,
    timeout_sec: float = 300.0,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    operation_delay_ms: int = 0,
    browser_runtime: XaiBrowserRuntime | None = None,
) -> tuple[dict[str, Any], XaiVisibleRegistration]:
    """Create an xAI account and keep its browser context for OAuth approval."""
    session = XaiVisibleRegistration(
        runtime=browser_runtime,
        proxy=proxy,
        headless=headless,
        on_progress=on_progress,
        should_cancel=should_cancel,
        operation_delay_ms=operation_delay_ms,
        timeout_sec=timeout_sec,
    )
    try:
        return (
            session.register_account(
                email=email,
                password=password,
                get_verification_code=get_verification_code,
            ),
            session,
        )
    except Exception:
        session.hold_failure()
        session.close()
        raise
