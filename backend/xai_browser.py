"""Visible browser support for the xAI protocol registration flow.

The xAI registration adapter talks to the upstream protocol directly.  This
module provides the optional visible browser that follows that same session:
it uses the same proxy, receives the protocol client's cookies, and shows the
real accounts.x.ai / grok.com pages in an isolated private context.  It does
not replace the protocol calls or turn the xAI account JSON into an OpenAI
Agent Identity file.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable


_sync_playwright: Any = None

XAI_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
XAI_GROK_URL = "https://grok.com/"


def _ensure_playwright() -> None:
    """Load a Playwright-compatible sync runtime only when it is requested."""
    global _sync_playwright
    if _sync_playwright is not None:
        return
    try:
        from patchright.sync_api import sync_playwright

        _sync_playwright = sync_playwright
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright

            _sync_playwright = sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "xAI 可视化浏览器需要 patchright 或 playwright。请安装项目依赖后重试。"
            ) from exc


class XaiBrowserError(RuntimeError):
    """Raised when the optional xAI browser session cannot be created."""


class XaiBrowserRuntime:
    """Reusable browser process; each account receives a fresh private context."""

    def __init__(self) -> None:
        self.browser: Any = None
        self.camoufox_manager: Any = None
        self.playwright: Any = None
        self.using_camoufox = False
        self.headless = True
        self.proxy_key = ""

    @staticmethod
    def _key(proxy: dict[str, str] | None) -> str:
        if not proxy:
            return ""
        return "|".join(
            str(proxy.get(key) or "")
            for key in ("server", "username", "password")
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
        proxy_matches = self.proxy_key == key or not self.using_camoufox
        if browser_alive and self.headless == headless and proxy_matches:
            self.proxy_key = key
            return self.browser, self.using_camoufox, True

        self.close()
        _ensure_playwright()
        self.headless = headless
        self.proxy_key = key
        width = 800
        height = 560
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--window-size={width},{height}",
        ]

        try:
            from camoufox.sync_api import Camoufox

            options: dict[str, Any] = {"headless": headless}
            if not headless:
                options["window"] = (width, height)
            if proxy and proxy.get("server"):
                options["proxy"] = proxy
            self.camoufox_manager = Camoufox(**options)
            self.browser = self.camoufox_manager.start()
            self.using_camoufox = True
            if on_launched:
                on_launched("launched_camoufox")
        except Exception:
            try:
                if self.camoufox_manager:
                    self.camoufox_manager.__exit__(None, None, None)
            except Exception:
                pass
            self.camoufox_manager = None
            self.browser = None
            self.playwright = _sync_playwright().start()
            launch_kwargs: dict[str, Any] = {"headless": headless, "args": launch_args}
            self.browser = self.playwright.chromium.launch(**launch_kwargs)
            self.using_camoufox = False
            if on_launched:
                on_launched("launched_chromium")
        return self.browser, self.using_camoufox, False

    def close(self) -> None:
        try:
            if self.camoufox_manager:
                self.camoufox_manager.__exit__(None, None, None)
            elif self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except Exception:
            pass
        self.browser = None
        self.camoufox_manager = None
        self.playwright = None
        self.using_camoufox = False
        self.proxy_key = ""


class XaiVisibleRegistration:
    """A browser context bound to a single xAI protocol registration session."""

    def __init__(
        self,
        *,
        runtime: XaiBrowserRuntime | None = None,
        proxy: dict[str, str] | None = None,
        headless: bool = True,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime or XaiBrowserRuntime()
        self.owns_runtime = runtime is None
        self.proxy = proxy
        self.headless = headless
        self.on_progress = on_progress
        self.context: Any = None
        self.page: Any = None
        self.using_camoufox = False

    def _progress(self, step: str, state: str, **extra: Any) -> None:
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
        except Exception:
            pass

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
        """Open the real xAI signup page in a clean private browser context."""
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
            self.page.goto(XAI_SIGNUP_URL, wait_until="domcontentloaded", timeout=45_000)
            self.page.wait_for_timeout(800)
            self._enforce_visible_window()
            self._progress("navigate", "ready")
        except Exception as exc:
            self.close()
            raise XaiBrowserError(f"xAI 可视化浏览器启动失败：{exc}") from exc

    def sync_cookies(
        self,
        cookies: dict[str, str] | None,
        *,
        authenticated: bool = False,
    ) -> None:
        """Copy the protocol client's current auth cookies into this context."""
        if not self.context or not isinstance(cookies, dict):
            return
        clean = {
            str(name): str(value)
            for name, value in cookies.items()
            if str(name).strip() and value is not None and str(value)
        }
        if not clean:
            return
        payloads: list[dict[str, str]] = []
        for url in ("https://accounts.x.ai", "https://grok.com"):
            payloads.extend(
                {"name": name, "value": value, "url": url}
                for name, value in clean.items()
            )
        try:
            self.context.add_cookies(payloads)
            self._progress("session" if authenticated else "init", "cookies_synced")
        except Exception:
            # Visual diagnostics must not break a registration that is otherwise valid.
            self._progress("session" if authenticated else "init", "cookie_sync_skipped")

    def _fill_first(self, selectors: list[str], value: str) -> bool:
        if self.page is None or not value:
            return False
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                count = min(8, int(locator.count()))
                for index in range(count):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        candidate.fill(value, timeout=1_500)
                        return True
            except Exception:
                continue
        return False

    def show_email(self, email: str) -> None:
        self._progress("email", "filling")
        filled = self._fill_first(
            [
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
                'input[placeholder*="email" i]',
            ],
            email,
        )
        self._progress("email", "filled" if filled else "prepared", email=email)

    def show_password(self, password: str) -> None:
        self._progress("password", "filling")
        filled = self._fill_first(
            [
                'input[type="password"]',
                'input[name*="password" i]',
                'input[autocomplete="new-password"]',
            ],
            password,
        )
        self._progress("password", "filled" if filled else "prepared")

    def show_verification_code(self, code: str | None = None) -> None:
        if not code:
            self._progress("verification", "waiting_for_code")
            return
        value = re.sub(r"[^A-Za-z0-9]", "", str(code)).upper()
        self._progress("verification", "code_received", code=value)
        filled = False
        if self.page is not None:
            try:
                split = self.page.locator('input[maxlength="1"]')
                count = int(split.count())
                if count >= len(value):
                    for index, char in enumerate(value):
                        candidate = split.nth(index)
                        if candidate.is_visible():
                            candidate.fill(char, timeout=1_000)
                            filled = True
            except Exception:
                pass
        if not filled:
            filled = self._fill_first(
                [
                    'input[name*="code" i]',
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[maxlength="6"]',
                ],
                value,
            )
        self._progress("verification", "code_filled" if filled else "code_ready")

    def show_account_creation(self) -> None:
        self._progress("account", "creating")
        try:
            if self.page is not None:
                self.page.bring_to_front()
        except Exception:
            pass

    def show_authenticated_page(self) -> None:
        """Navigate the shared cookie context to Grok after SSO is acquired."""
        if self.page is None:
            return
        try:
            self._progress("session", "opening_grok")
            self.page.goto(XAI_GROK_URL, wait_until="domcontentloaded", timeout=45_000)
            self.page.wait_for_timeout(800)
            self._enforce_visible_window()
            self._progress("session", "ready")
        except Exception:
            self._progress("session", "page_check_skipped")

    def show_complete(self) -> None:
        self._progress("completion", "done")

    def hold_failure(
        self,
        *,
        seconds: float = 20.0,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        if self.headless or self.page is None:
            return
        self._progress("flow", "debug_hold", seconds=int(max(0, seconds)))
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if should_cancel and should_cancel():
                return
            try:
                self.page.wait_for_timeout(250)
            except Exception:
                return

    def close(self) -> None:
        """Clear account storage before returning the browser process to its worker."""
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
