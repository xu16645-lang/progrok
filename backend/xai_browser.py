"""Browser-only xAI account registration and OAuth consent flow."""
from __future__ import annotations

import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


XAI_SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
XAI_GROK_URL = "https://grok.com/"
TURNSTILE_WAIT_SEC = 90.0
TURNSTILE_CLICK_COOLDOWN_SEC = 2.0
TURNSTILE_MAX_CLICK_ATTEMPTS = 3
TURNSTILE_CLICK_EFFECT_WAIT_MS = 400
# Password/profile pages often mount Turnstile a few hundred ms after inputs fill.
TURNSTILE_MOUNT_GRACE_SEC = 4.0
PAGE_SETTLE_DELAY_MS = 600
REGISTRATION_LANDING_TIMEOUT_SEC = 60.0
SIGNUP_FORM_TIMEOUT_SEC = 15.0
SIGNUP_SUBMIT_TIMEOUT_SEC = 35.0
SIGNUP_SUBMIT_RETRY_SEC = 4.0
SIGNUP_SUBMIT_MAX_ATTEMPTS = 3
SIGNUP_TRANSITION_STABLE_POLLS = 3

# Firefox otherwise deprioritizes fully covered windows on Windows. During a
# visible concurrent batch that can delay React/Turnstile timers until the user
# brings each browser window to the foreground.
VISIBLE_BACKGROUND_PREFS = {
    "widget.windows.window_occlusion_tracking.enabled": False,
    "dom.timeout.enable_budget_timer_throttling": False,
    "dom.min_background_timeout_value": 10,
    "media.suspend-bkgnd-video.enabled": False,
    "browser.tabs.unloadOnLowMemory": False,
}

_FIRST_NAMES = (
    "Aiden",
    "Caleb",
    "Ethan",
    "Lucas",
    "Mason",
    "Nolan",
    "Owen",
    "Ryan",
    "Sophie",
    "Chloe",
    "Emma",
    "Grace",
    "Mia",
    "Nora",
    "Ruby",
    "Zoe",
)
_LAST_NAMES = (
    "Bennett",
    "Carter",
    "Collins",
    "Foster",
    "Hayes",
    "Morgan",
    "Parker",
    "Reed",
    "Ross",
    "Scott",
    "Turner",
    "Walker",
    "Ward",
    "Wells",
    "Young",
)


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
                options["firefox_user_prefs"] = dict(VISIBLE_BACKGROUND_PREFS)
            if proxy and proxy.get("server"):
                options["proxy"] = proxy
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

    def _settle_page(self) -> None:
        """Wait for navigation and rendering without requiring permanent network idleness."""
        self._check_cancel()
        if self.page is None:
            return
        self._wait(250)
        for state, timeout in (("domcontentloaded", 5_000), ("networkidle", 2_000)):
            self._check_cancel()
            try:
                self.page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                # xAI keeps background requests open, so networkidle is best effort.
                pass
        self._wait(PAGE_SETTLE_DELAY_MS)

    @staticmethod
    def _generate_profile() -> tuple[str, str]:
        return secrets.choice(_FIRST_NAMES), secrets.choice(_LAST_NAMES)

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

    def _open_private_context(self) -> None:
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

    def open(self) -> None:
        """Open the real signup page in a clean private context."""
        try:
            self._open_private_context()
            assert self.page is not None
            self._progress("navigate", "loading")
            self._action_delay()
            self.page.goto(XAI_SIGNUP_URL, wait_until="domcontentloaded", timeout=45_000)
            self._settle_page()
            self._enforce_visible_window()
            self._progress("navigate", "ready")
        except (XaiBrowserCancelled, XaiBrowserError):
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise XaiBrowserError(f"xAI 浏览器启动失败：{exc}") from exc

    def _login_for_authorization(self, email: str, password: str) -> None:
        """Create a real browser login session before starting Device Flow."""
        if self.page is None:
            raise XaiBrowserError("xAI 授权浏览器未启动")
        self._progress("session", "full_login_starting")
        self.page.goto(
            "https://accounts.x.ai/sign-in?redirect=grok-com",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        submitted_forms: set[str] = set()
        email_method_selected = False
        deadline = min(self.deadline, time.time() + 120.0)
        while time.time() < deadline:
            self._check_cancel()
            current_url = str(self.page.url or "")
            sso, _cookies = self._extract_sso()
            if sso and not re.search(r"/sign-in(?:[/?#]|$)", current_url, re.I):
                self._progress("session", "full_login_complete")
                return

            text = self._page_text()
            if re.search(
                r"invalid credentials|incorrect (?:email|password)|email or password.*incorrect|"
                r"邮箱或密码.*(?:错误|不正确)",
                text,
                re.I,
            ):
                raise XaiBrowserError("xAI 浏览器完整登录失败：邮箱或密码不正确")
            self._raise_page_error(email)

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
                    'input[autocomplete="current-password"]',
                ]
            )
            if email_input is None and password_input is None:
                if not email_method_selected and re.search(
                    r"sign in with email|log\s*in with email|login with email|"
                    r"使用邮箱.*登录|邮箱登录",
                    text,
                    re.I,
                ):
                    if self._click_action(
                        r"^sign in with email$|^log\s*in with email$|^login with email$|"
                        r"使用邮箱.*登录|邮箱登录"
                    ):
                        email_method_selected = True
                        self._progress("session", "email_login_selected")
                        self._settle_page()
                        continue
                self._wait(250)
                continue

            form_key = (
                ("email" if email_input is not None else "")
                + "+"
                + ("password" if password_input is not None else "")
            )
            if form_key in submitted_forms:
                self._wait(250)
                continue

            anchor = password_input or email_input
            self._action_delay()
            if self._input_is_editable(email_input):
                email_input.fill(email)
                self._progress("session", "login_email_filled")
            elif email_input is not None:
                self._progress("session", "login_email_confirmed_readonly")
            if password_input is not None:
                password_input.fill(password)
                self._progress("session", "login_password_filled")
                self._wait_for_turnstile()
            self._submit(
                anchor,
                "sign in|log\\s*in|login|continue|next|submit|登录|继续|下一步",
            )
            submitted_forms.add(form_key)
            self._progress("session", "login_form_submitted", form=form_key)
            self._settle_page()
        raise XaiBrowserError("xAI 浏览器完整登录超时，未进入 Grok 登录成功页")

    def open_authorization(
        self,
        cookies: dict[str, str],
        *,
        email: str = "",
        password: str = "",
        force_full_login: bool = False,
    ) -> None:
        """Open a private context with protocol cookies ready for Device Allow."""
        try:
            self._open_private_context()
            if force_full_login:
                if not email or not password:
                    raise XaiBrowserError("浏览器完整登录缺少邮箱或密码")
                self._login_for_authorization(email, password)
            else:
                self.sync_cookies(cookies, authenticated=True)
            assert self.page is not None
            # Browser registration reaches an authenticated Grok landing page
            # before Device Flow starts. Reproduce that browser state after
            # restoring a protocol session instead of navigating straight from
            # an empty context to the device form.
            if not force_full_login:
                self.page.goto(
                    XAI_GROK_URL, wait_until="domcontentloaded", timeout=45_000
                )
                self._wait(1_000)
            landing_url = str(self.page.url or "")
            if re.search(r"/(?:sign-in|sign-up)(?:[/?#]|$)", landing_url, re.I):
                raise XaiBrowserError("协议登录会话未被授权浏览器接受，无法进入 Grok 登录页")
            self._raise_page_error("")
            self._progress("session", "authenticated_landing_ready")
            self._progress("session", "authorization_ready")
        except (XaiBrowserCancelled, XaiBrowserError):
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise XaiBrowserError(f"xAI 授权浏览器启动失败：{exc}") from exc

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

    @staticmethod
    def _input_is_editable(target: Any | None) -> bool:
        if target is None:
            return False
        try:
            return bool(target.is_editable(timeout=500))
        except Exception:
            return False

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

    def _device_action_busy(self) -> bool:
        """Return True while the Device Flow page is processing a submitted action."""
        if self.page is None:
            return False
        for selector in (
            '[aria-busy="true"]',
            '[role="progressbar"]',
            'button:disabled',
            'button[aria-disabled="true"]',
        ):
            try:
                locator = self.page.locator(selector)
                for index in range(min(10, int(locator.count()))):
                    if locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _click_action(self, pattern: str) -> bool:
        candidate = self._find_action(pattern)
        if candidate is None:
            return False
        # xAI re-renders buttons while Locator.click performs its stability and
        # navigation checks. A trusted mouse click at the resolved center avoids
        # treating that successful transition as a Playwright timeout.
        return self._click_locator_center(candidate, timeout=3_000)

    def _wait_for_email_signup_form(self) -> bool:
        """Confirm the email form rendered after choosing email signup."""
        deadline = min(self.deadline, time.time() + SIGNUP_FORM_TIMEOUT_SEC)
        selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="email"]',
            'input[placeholder*="email" i]',
        ]
        while time.time() < deadline:
            self._check_cancel()
            if self._first_visible(selectors) is not None:
                return True
            self._wait(200)
        return False

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
        # The profile page contains password-related inputs whose generated
        # names can include "code". It is not the email-code page until the
        # visible password field has disappeared.
        if self._first_visible(
            [
                'input[type="password"]',
                'input[name*="password" i]',
                'input[autocomplete="new-password"]',
            ]
        ) is not None:
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

    def _turnstile_status(self) -> str:
        """Return absent, pending, finishing, or passed for the current widget."""
        if self.page is None:
            return "absent"
        seen = False
        try:
            responses = self.page.locator(
                'input[name="cf-turnstile-response"], '
                'textarea[name="cf-turnstile-response"]'
            )
            seen = int(responses.count()) > 0
            for index in range(min(5, int(responses.count()))):
                value = str(
                    responses.nth(index).input_value(timeout=500) or ""
                ).strip()
                if len(value) >= 20:
                    return "passed"
        except Exception:
            pass
        try:
            containers = self.page.locator(
                '.cf-turnstile, [data-sitekey], [data-turnstile-widget]'
            )
            for index in range(min(5, int(containers.count()))):
                if containers.nth(index).is_visible(timeout=300):
                    seen = True
                    break
        except Exception:
            pass
        try:
            frames = self.page.locator('iframe[src*="challenges.cloudflare.com"]')
            for index in range(min(5, int(frames.count()))):
                frame = frames.nth(index)
                if frame.is_visible():
                    seen = True
        except Exception:
            pass
        success_seen = False
        try:
            for frame in self.page.frames:
                if "challenges.cloudflare.com" not in str(frame.url or ""):
                    continue
                seen = True
                text = str(frame.locator("body").inner_text(timeout=500) or "")
                if re.search(r"success|验证成功", text, re.I):
                    success_seen = True
        except Exception:
            pass
        if success_seen:
            # The widget can show Success before its callback writes the response
            # token into the parent form. Only the token above authorizes submit.
            return "finishing"
        return "pending" if seen else "absent"

    def _locator_identity(self, locator: Any) -> str | None:
        """Stable DOM identity for de-duplicating overlapping locators."""
        try:
            handle = locator.element_handle(timeout=300)
        except Exception:
            handle = None
        if handle is not None:
            try:
                identity = handle.evaluate(
                    """(node) => {
                        if (!node) return '';
                        const src = String(node.getAttribute?.('src') || node.src || '');
                        const title = String(node.getAttribute?.('title') || node.title || '');
                        const name = String(node.getAttribute?.('name') || node.name || '');
                        const idAttr = String(node.id || '');
                        const cls = String(node.className || '');
                        const tag = String(node.tagName || '').toLowerCase();
                        const rect = node.getBoundingClientRect?.() || {};
                        const box = [
                            Math.round(rect.x || 0),
                            Math.round(rect.y || 0),
                            Math.round(rect.width || 0),
                            Math.round(rect.height || 0),
                        ].join(',');
                        return [tag, idAttr, name, title, src, cls, box].join('|');
                    }"""
                )
                value = str(identity or "").strip()
                if value:
                    return value
            except Exception:
                pass
        try:
            box = locator.bounding_box(timeout=300)
        except Exception:
            box = None
        if isinstance(box, dict):
            return "|".join(
                str(round(float(box.get(key) or 0.0), 1))
                for key in ("x", "y", "width", "height")
            )
        return None

    def _turnstile_iframe_locators(self) -> list[Any]:
        """Return visible Cloudflare challenge iframes only."""
        if self.page is None:
            return []
        # Keep selectors specific. Generic title*=widget matches unrelated embeds.
        iframe_selectors = (
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            'iframe[title*="cloudflare" i]',
            'iframe[title*="turnstile" i]',
            'iframe[title*="security challenge" i]',
        )
        iframes: list[Any] = []
        seen: set[str] = set()
        for selector in iframe_selectors:
            try:
                frames = self.page.locator(selector)
                count = min(3, int(frames.count()))
            except Exception:
                continue
            for index in range(count):
                iframe = frames.nth(index)
                try:
                    if not iframe.is_visible(timeout=300):
                        continue
                except Exception:
                    continue
                identity = self._locator_identity(iframe) or f"{selector}#{index}"
                if identity in seen:
                    continue
                seen.add(identity)
                iframes.append(iframe)
        return iframes

    def _turnstile_frame_content(self, iframe: Any) -> Any | None:
        try:
            handle = iframe.element_handle(timeout=500)
            if handle is None:
                return None
            return handle.content_frame()
        except Exception:
            return None

    def _turnstile_interactive_markers(self, content: Any) -> bool:
        """Heuristic: widget is the human-click checkbox challenge, not pure managed."""
        try:
            text = str(content.locator("body").inner_text(timeout=400) or "")
        except Exception:
            text = ""
        if re.search(
            r"verify you are human|i'?m not a robot|确认您是真人|进行人机验证|请完成以下操作",
            text,
            re.I,
        ):
            return True
        marker_selectors = (
            'input[type="checkbox"]',
            ".cb-lb",
            ".cb-i",
            'label[class*="cb"]',
            "#challenge-stage",
        )
        for selector in marker_selectors:
            try:
                node = content.locator(selector).first
                if int(node.count()) > 0:
                    return True
            except Exception:
                continue
        return False

    def _turnstile_click_targets(self) -> list[tuple[str, Any]]:
        """Return clickable Turnstile targets for confirmed interactive widgets.

        New Turnstile often hides the real checkbox input and paints the box via
        label/pseudo-elements. Only emit targets when interactive markers exist;
        managed auto-pass widgets must not be clicked.
        """
        if self.page is None:
            return []

        targets: list[tuple[str, Any]] = []
        seen_ids: set[str] = set()

        def _add(kind: str, locator: Any) -> None:
            marker = self._locator_identity(locator) or f"{kind}:{len(targets)}"
            key = f"{kind}:{marker}"
            if key in seen_ids:
                return
            seen_ids.add(key)
            targets.append((kind, locator))

        checkbox_selectors = (
            'input[type="checkbox"]',
            '.cb-lb input[type="checkbox"]',
            'label input[type="checkbox"]',
            "#challenge-stage input",
        )
        surface_selectors = (
            "label.cb-lb",
            ".cb-lb",
            ".cb-i",
            'label[class*="cb"]',
            "#challenge-stage",
        )

        for iframe in self._turnstile_iframe_locators():
            content = self._turnstile_frame_content(iframe)
            if content is None:
                # Unreadable frame content is not proof of an interactive challenge.
                continue
            if not self._turnstile_interactive_markers(content):
                continue

            for checkbox_selector in checkbox_selectors:
                try:
                    checkbox = content.locator(checkbox_selector).first
                    if int(checkbox.count()) <= 0:
                        continue
                    _add("checkbox", checkbox)
                    break
                except Exception:
                    continue
            for surface_selector in surface_selectors:
                try:
                    surface = content.locator(surface_selector).first
                    if int(surface.count()) <= 0:
                        continue
                    _add("surface", surface)
                    break
                except Exception:
                    continue
            _add("iframe", iframe)
        return targets

    def _turnstile_needs_click(self) -> bool:
        """True only when interactive markers are readable inside a CF iframe."""
        if self.page is None:
            return False
        for iframe in self._turnstile_iframe_locators():
            content = self._turnstile_frame_content(iframe)
            if content is None:
                # Loading/unreadable content: keep waiting for auto-pass or markers.
                continue
            if self._turnstile_interactive_markers(content):
                return True
        return False

    def _click_point(self, x: float, y: float) -> bool:
        if self.page is None:
            return False
        try:
            self.page.mouse.click(x, y)
            return True
        except Exception:
            return False

    def _click_locator_center(self, locator: Any, *, timeout: int = 1_500) -> bool:
        """Dispatch a click; True only means the browser accepted the event."""
        try:
            locator.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass
        try:
            box = locator.bounding_box(timeout=timeout)
        except Exception:
            box = None
        if isinstance(box, dict) and box.get("width") and box.get("height"):
            x = float(box["x"]) + float(box["width"]) / 2.0
            y = float(box["y"]) + float(box["height"]) / 2.0
            if self._click_point(x, y):
                return True
        try:
            locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            return False

    def _click_iframe_checkbox_region(self, iframe: Any, *, timeout: int = 1_500) -> bool:
        """Click the left checkbox zone of a Turnstile iframe widget."""
        try:
            if not iframe.is_visible(timeout=timeout):
                return False
            iframe.scroll_into_view_if_needed(timeout=timeout)
        except Exception:
            pass
        try:
            box = iframe.bounding_box(timeout=timeout)
        except Exception:
            box = None
        if not isinstance(box, dict) or not box.get("width") or not box.get("height"):
            return self._click_locator_center(iframe, timeout=timeout)
        # Checkbox sits near the left edge of the managed/interactive widget.
        x = float(box["x"]) + min(28.0, max(12.0, float(box["width"]) * 0.12))
        y = float(box["y"]) + float(box["height"]) / 2.0
        if self._click_point(x, y):
            return True
        return self._click_locator_center(iframe, timeout=timeout)

    def _turnstile_click_advanced(self, before: str, after: str | None = None) -> bool:
        """True only when status advanced to finishing/passed.

        pending -> absent is NOT success: the widget may remount, briefly detach,
        or status probing can fail. Those cases must keep trying other strategies.
        """
        del before  # retained for call-site clarity / future transitions
        current = after if after is not None else self._turnstile_status()
        return current in {"passed", "finishing"}

    def _signup_form_present(self) -> bool:
        return self._first_visible(
            [
                'input[type="password"]',
                'input[name*="password" i]',
                'input[autocomplete="new-password"]',
                'input[name*="first" i]',
                'input[name*="given" i]',
                'input[name*="last" i]',
                'input[name*="family" i]',
                'input[name="name"]',
                'input[name*="fullName" i]',
            ]
        ) is not None

    def _wait_for_signup_transition(self, pattern: str) -> None:
        """Confirm the profile submit changed state, retrying only if still actionable."""
        if self.page is None:
            raise XaiBrowserError("xAI 注册页面已经关闭")
        initial_url = str(self.page.url or "")
        deadline = min(self.deadline, time.time() + SIGNUP_SUBMIT_TIMEOUT_SEC)
        attempts = 1
        next_retry_at = time.time() + SIGNUP_SUBMIT_RETRY_SEC
        form_absent_polls = 0
        self._progress("completion", "waiting_for_transition", attempt=attempts)
        while time.time() < deadline:
            self._check_cancel()
            sso, _cookies = self._extract_sso()
            current_url = str(self.page.url or "")
            if sso:
                self._progress(
                    "completion",
                    "transition_detected",
                    attempt=attempts,
                    evidence="sso_cookie",
                )
                return
            if not self._signup_form_present():
                form_absent_polls += 1
                if form_absent_polls >= SIGNUP_TRANSITION_STABLE_POLLS:
                    self._progress(
                        "completion",
                        "transition_detected",
                        attempt=attempts,
                        evidence="form_absent",
                        url_changed=current_url != initial_url,
                    )
                    return
                self._wait(250)
                continue
            form_absent_polls = 0
            now = time.time()
            if now >= next_retry_at and attempts < SIGNUP_SUBMIT_MAX_ATTEMPTS:
                action = self._find_action(pattern)
                if action is not None:
                    attempts += 1
                    self._progress("completion", "retrying_submit", attempt=attempts)
                    if not self._click_locator_center(action, timeout=3_000):
                        self._progress("completion", "retry_click_failed", attempt=attempts)
                    next_retry_at = now + SIGNUP_SUBMIT_RETRY_SEC
            self._wait(250)
        raise XaiBrowserError(
            f"xAI 资料提交后页面未变化，已尝试点击 {attempts} 次"
        )

    def _dispatch_turnstile_target_click(self, kind: str, target: Any) -> bool:
        """Fire one click strategy. True only means the event was dispatched."""
        if kind == "iframe":
            return self._click_iframe_checkbox_region(target, timeout=1_200)
        if kind == "checkbox":
            # Hidden checkbox force-click is attempted, but never treated as the
            # only strategy: caller falls through when status stays pending.
            try:
                target.click(timeout=1_200, force=True)
                return True
            except Exception:
                return self._click_locator_center(target, timeout=1_200)
        return self._click_locator_center(target, timeout=1_200)

    def _click_turnstile_challenge(self) -> bool:
        """Click interactive Turnstile surfaces until status advances or options end.

        A Playwright click without exception is not success. Only finishing/passed
        counts. pending -> absent is treated as no-effect so strategies continue.
        """
        before = self._turnstile_status()
        if before in {"passed", "finishing"}:
            return True
        if before == "absent":
            return False

        attempted = False
        for kind, target in self._turnstile_click_targets():
            attempted = True
            self._progress("human_verification", f"click_attempt_{kind}")
            dispatched = self._dispatch_turnstile_target_click(kind, target)
            if not dispatched:
                self._progress("human_verification", f"click_dispatch_failed_{kind}")
                continue
            self._wait(TURNSTILE_CLICK_EFFECT_WAIT_MS)
            after = self._turnstile_status()
            if self._turnstile_click_advanced(before, after):
                self._progress("human_verification", f"click_effect_{kind}")
                return True
            if after == "absent":
                self._progress("human_verification", f"click_widget_missing_{kind}")
            else:
                self._progress("human_verification", f"click_no_effect_{kind}")
        return False if attempted else False

    def _wait_for_turnstile(self) -> None:
        """Wait for token-backed pass; allow late widget mount before giving up."""
        status = self._turnstile_status()
        if status == "passed":
            self._progress("human_verification", "passed")
            return

        # Do not treat the first absent sample as "not required". Profile pages
        # often mount Turnstile shortly after password fill.
        if status == "absent":
            self._progress("human_verification", "waiting_for_widget_mount")
        else:
            self._progress("human_verification", "detected")

        if not self.headless and self.page is not None:
            try:
                self.page.bring_to_front()
            except Exception:
                pass

        # Managed/non-interactive Turnstile usually resolves itself. Only click
        # after interactive markers are readable. Success text alone is not pass:
        # the parent form needs cf-turnstile-response before submit is safe.
        if status != "absent":
            self._progress("human_verification", "waiting_for_auto_pass")
        deadline = min(self.deadline, time.time() + TURNSTILE_WAIT_SEC)
        mount_deadline = time.time() + TURNSTILE_MOUNT_GRACE_SEC
        click_attempts = 0
        last_click_at = 0.0
        finishing_logged = False
        seen_widget = status != "absent"
        while time.time() < deadline:
            self._check_cancel()
            status = self._turnstile_status()
            if status == "passed":
                self._progress("human_verification", "passed")
                return
            if status == "absent":
                # Still within grace: widget may be mounting. After grace and never
                # seeing a widget, treat as not required. If we already saw one and
                # it disappeared, keep waiting until the main deadline.
                if not seen_widget and time.time() >= mount_deadline:
                    self._progress("human_verification", "not_required")
                    return
                self._wait(250)
                continue
            if not seen_widget:
                seen_widget = True
                self._progress("human_verification", "detected")
                self._progress("human_verification", "waiting_for_auto_pass")
            if status == "finishing":
                if not finishing_logged:
                    self._progress("human_verification", "waiting_for_response_token")
                    finishing_logged = True
                # Keep waiting for the response token. Do not treat "Success" text
                # plus an enabled submit button as passed.
                self._wait(250)
                continue
            finishing_logged = False
            can_retry_click = (
                click_attempts < TURNSTILE_MAX_CLICK_ATTEMPTS
                and time.time() - last_click_at >= TURNSTILE_CLICK_COOLDOWN_SEC
            )
            if can_retry_click and self._turnstile_needs_click():
                self._progress("human_verification", "manual_click_required")
                click_attempts += 1
                last_click_at = time.time()
                if self._click_turnstile_challenge():
                    self._progress("human_verification", "clicked_challenge")
                    # Effect already observed inside the click helper; re-check now.
                    if self._turnstile_status() == "passed":
                        self._progress("human_verification", "passed")
                        return
                elif not self.headless:
                    self._progress("human_verification", "waiting_for_manual_action")
            self._wait(250)
        if self.headless:
            raise XaiBrowserError(
                "xAI 人机验证未自动通过；请开启“显示注册浏览器”后手工完成验证"
            )
        raise XaiBrowserError("等待手工完成人机验证超时")

    @staticmethod
    def _input_has_value(target: Any | None) -> bool:
        if target is None:
            return False
        try:
            return bool(str(target.input_value(timeout=500) or ""))
        except Exception:
            return False

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
        first_name_value, last_name_value = self._generate_profile()
        self._progress("profile", "generated")
        sso_seen_at: float | None = None
        while True:
            self._check_cancel()
            sso, cookies = self._extract_sso()
            if sso:
                current_url = str(self.page.url or "")
                still_on_signup = "/sign-up" in current_url.lower()
                if not still_on_signup:
                    self._progress("completion", "done", url=current_url[:100])
                    return {
                        "ok": True,
                        "email": email,
                        "sso": sso,
                        "cookies": cookies,
                        "steps": list(self.steps),
                    }
                if sso_seen_at is None:
                    sso_seen_at = time.time()
                    self._progress("completion", "waiting_for_landing")
                if time.time() - sso_seen_at >= REGISTRATION_LANDING_TIMEOUT_SEC:
                    raise XaiBrowserError(
                        "xAI 已生成登录 Session，但注册页未自然跳转到登录成功页面"
                    )
                self._wait(500)
                continue
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
                if not self._wait_for_email_signup_form():
                    raise XaiBrowserError("已点击 xAI 邮箱注册入口，但邮箱表单未加载")
                acted.add("signup_method")
                self._progress("signup_method", "selected")
                self._settle_page()
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
                    self._settle_page()
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
                    self._settle_page()
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
            full_name = None
            if first_name is None and last_name is None:
                full_name = self._first_visible(
                    [
                        'input[name="name"]',
                        'input[name*="fullName" i]',
                        'input[name*="displayName" i]',
                        'input[autocomplete="name"]',
                    ]
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
            if first_name is not None or last_name is not None or full_name is not None:
                stage_parts.append("profile")
                if "profile" not in acted:
                    self._progress("profile", "filling")
                    self._action_delay()
                    if first_name is not None:
                        first_name.fill(first_name_value)
                        anchor = first_name
                    if last_name is not None:
                        last_name.fill(last_name_value)
                        anchor = last_name
                    if full_name is not None:
                        full_name.fill(f"{first_name_value} {last_name_value}")
                        anchor = full_name
                    self._progress("profile", "filled")
            if password_input is not None:
                stage_parts.append("password")
                if "password" not in acted:
                    self._progress("password", "filling")
                    self._action_delay()
                    password_input.fill(password)
                    anchor = password_input
                    self._progress("password", "filled")

            pending = [part for part in stage_parts if part not in acted]
            if pending:
                if password_input is not None:
                    # The profile form may re-render while Turnstile finishes and
                    # clear React-controlled inputs. Re-check the actual DOM value
                    # before and after verification instead of sleeping blindly.
                    for attempt in range(2):
                        if not self._input_has_value(password_input):
                            self._progress("password", "refilling_after_render")
                            password_input.fill(password)
                            anchor = password_input
                        self._wait_for_turnstile()
                        if self._input_has_value(password_input):
                            break
                        if attempt == 1:
                            raise XaiBrowserError("xAI 页面反复重绘并清空密码输入框")
                submit_pattern = (
                    "complete sign up|continue|next|sign up|create account|submit|"
                    "继续|下一步|创建"
                )
                self._submit(
                    anchor,
                    submit_pattern,
                )
                self._wait_for_signup_transition(submit_pattern)
                for part in pending:
                    acted.add(part)
                    self._progress(part, "submitted")
                self._settle_page()
                continue

            if str(self.page.url or "").startswith(XAI_GROK_URL):
                self._wait(1_000)
                continue
            self._wait(250)

    @staticmethod
    def _callback_from_url(url: str, expected_state: str) -> str | None:
        try:
            parsed = urlparse(str(url or ""))
            values = parse_qs(parsed.query)
        except Exception:
            return None
        code = str((values.get("code") or [""])[0]).strip()
        state = str((values.get("state") or [""])[0]).strip()
        if not code:
            return None
        if expected_state and state and state != expected_state:
            raise XaiBrowserError("Sub2API Grok OAuth 回调 state 不匹配")
        return str(url)

    def _authorization_code_from_page(self) -> str | None:
        if self.page is None:
            return None
        text = self._page_text()
        if not re.search(
            r"copy.*code|input.*code|code.*complete.*login|复制.*代码|输入.*代码|完成登录",
            text,
            re.I,
        ):
            return None
        try:
            candidates = self.page.locator("input, textarea, code")
            for index in range(min(20, int(candidates.count()))):
                candidate = candidates.nth(index)
                if not candidate.is_visible():
                    continue
                try:
                    value = str(candidate.input_value(timeout=1_000) or "").strip()
                except Exception:
                    try:
                        value = str(candidate.inner_text(timeout=1_000) or "").strip()
                    except Exception:
                        continue
                if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
                    return value
        except Exception:
            pass
        return None

    def authorize_sub2_oauth(self, auth_url: str, expected_state: str) -> str:
        """Authorize Sub2API's PKCE request and return its callback URL or code."""
        if self.page is None:
            raise XaiBrowserError("xAI 授权浏览器未启动")
        self._progress("sub2_oauth", "opening_authorization")
        self._action_delay()
        try:
            self.page.goto(auth_url, wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            # A localhost callback can fail to connect after the URL already contains code.
            callback = self._callback_from_url(str(self.page.url or ""), expected_state)
            if callback:
                self._progress("sub2_oauth", "code_captured")
                return callback
        self._settle_page()
        clicked_signatures: set[str] = set()
        deadline = min(self.deadline, time.time() + 120.0)
        while time.time() < deadline:
            self._check_cancel()
            current_url = str(self.page.url or "")
            callback = self._callback_from_url(current_url, expected_state)
            if callback:
                self._progress("sub2_oauth", "code_captured")
                return callback
            page_code = self._authorization_code_from_page()
            if page_code:
                self._progress("sub2_oauth", "code_captured")
                return page_code
            text = self._page_text()
            if re.search(
                r"access denied|authorization.*failed|request.*denied|授权.*失败|拒绝授权",
                text,
                re.I,
            ):
                raise XaiBrowserError(f"Sub2API Grok OAuth 授权失败：{text[:240]}")
            action = self._find_action(
                r"allow|authorize|approve|accept|confirm|continue|grant|允许|授权|同意|确认|继续"
            )
            if action is not None:
                signature = f"{current_url}|{text[:160]}"
                if signature not in clicked_signatures:
                    clicked_signatures.add(signature)
                    self._action_delay()
                    action.click(timeout=3_000)
                    self._progress("sub2_oauth", "approval_submitted")
                    self._settle_page()
                    continue
            self._wait(500)
        raise XaiBrowserError("等待 Sub2API Grok OAuth 回调或授权码超时")

    def authorize_device(self, verify_url: str, user_code: str) -> None:
        """Approve an OAuth device request through the authenticated browser page."""
        if self.page is None:
            raise XaiBrowserError("xAI 授权浏览器未启动")
        self._progress("oauth", "opening_device_authorization")
        self._action_delay()
        self.page.goto(verify_url, wait_until="domcontentloaded", timeout=45_000)
        self._wait(600)
        code_filled = False
        code_attempts = 0
        approval_attempts = 0
        last_code_submit_at = 0.0
        last_approval_at = 0.0
        busy_state = ""
        waiting_approval_logged = False
        approval_processing_seen = False
        action_settle_sec = 5.0
        deadline = min(self.deadline, time.time() + 90.0)
        while time.time() < deadline:
            self._check_cancel()
            text = self._page_text()
            url = str(self.page.url or "")
            if re.search(r"/(?:sign-in|sign-up)(?:[/?#]|$)", url, re.I):
                raise XaiBrowserError("协议登录会话未被授权浏览器接受，页面跳转到登录入口")
            if "done" in url or re.search(
                r"device.*(?:authorized|approved)|authorization complete|you may close",
                text,
                re.I,
            ):
                self._progress("oauth", "approved")
                return
            if re.search(r"expired|invalid.*code|request.*denied", text, re.I):
                raise XaiBrowserError(f"xAI 设备授权失败：{text[:240]}")
            action_busy = self._device_action_busy()
            if action_busy and code_attempts > 0 and approval_attempts == 0:
                if busy_state != "code":
                    busy_state = "code"
                    self._progress("oauth", "device_code_processing")
            elif action_busy and approval_attempts > 0:
                approval_processing_seen = True
                if busy_state != "approval":
                    busy_state = "approval"
                    self._progress("oauth", "approval_processing")
            target = self._first_visible(
                [
                    'input[name="user_code"]',
                    'input[name*="code" i]',
                    'input[autocomplete="one-time-code"]',
                ]
            )
            if target is not None:
                if not code_filled:
                    self._action_delay()
                    target.fill(user_code)
                    code_filled = True
                    self._progress("oauth", "device_code_filled")
                    self._wait(250)
                if action_busy:
                    self._wait(500)
                    continue
                busy_state = ""
                if code_attempts == 0:
                    self._action_delay()
                    self._submit(target, "continue|verify|submit|继续|验证")
                    code_attempts += 1
                    last_code_submit_at = time.time()
                    self._progress(
                        "oauth", "device_code_submitted", attempt=code_attempts
                    )
                    self._wait(800)
                else:
                    if not waiting_approval_logged:
                        waiting_approval_logged = True
                        self._progress("oauth", "waiting_for_approval_page")
                    self._wait(200)
                continue
            action = self._find_action(
                "allow|authorize|approve|accept|confirm|允许|授权|同意|确认"
            )
            if action is not None:
                if action_busy:
                    self._wait(500)
                    continue
                if approval_attempts > 0 and (
                    approval_processing_seen
                    or time.time() - last_approval_at >= action_settle_sec
                ):
                    self._progress("oauth", "approval_roundtrip_complete")
                    return
                busy_state = ""
                if approval_attempts == 0:
                    self._action_delay()
                    if not self._click_locator_center(action, timeout=3_000):
                        action.click(timeout=3_000)
                    approval_attempts += 1
                    last_approval_at = time.time()
                    self._progress(
                        "oauth", "approval_submitted", attempt=approval_attempts
                    )
                    self._wait(800)
                else:
                    self._wait(200)
                continue
            if not action_busy and approval_attempts > 0 and (
                approval_processing_seen
                or time.time() - last_approval_at >= action_settle_sec
            ):
                self._progress("oauth", "approval_roundtrip_complete")
                return
            self._wait(250)
        if code_attempts > 0 and approval_attempts == 0:
            raise XaiBrowserError("Device Code 已提交，但页面始终未进入 Allow 授权确认页")
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
