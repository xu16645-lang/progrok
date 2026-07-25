"""ChatGPT browser automation core for account registration.

Uses Camoufox to:
1. Open chatgpt.com and start signup with email
2. Wait for email verification code (via external receiver callback)
3. Fill in name and birthdate
4. Complete registration
5. Extract session from https://chatgpt.com/api/auth/session

Supports proxy, cancellation, and error handling.
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

from chatgpt_browser_context import BrowserContext
import chatgpt_browser_registration as _registration

def _ensure_camoufox() -> None:
    """Lazy-import Camoufox so the module can report a clear startup error."""
    try:
        from camoufox.sync_api import Camoufox

        del Camoufox
    except ImportError as exc:
        raise RuntimeError("Camoufox 未安装，请安装项目浏览器依赖后重试") from exc


# --------------------------------------------------------------------------- #
# Generation helpers
# --------------------------------------------------------------------------- #
_first_names = [
    "James",
    "Mary",
    "John",
    "Patricia",
    "Robert",
    "Jennifer",
    "Michael",
    "Linda",
    "David",
    "Elizabeth",
    "William",
    "Barbara",
    "Richard",
    "Susan",
    "Joseph",
    "Jessica",
    "Thomas",
    "Sarah",
    "Christopher",
    "Karen",
]
_last_names = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Gonzalez",
    "Wilson",
    "Anderson",
    "Thomas",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
]


def _random_name() -> tuple[str, str]:
    return random.choice(_first_names), random.choice(_last_names)


def _random_birthdate(min_age: int = 20, max_age: int = 55) -> dict[str, str]:
    """Return a random adult birthdate as {month, day, year} dict."""
    today = datetime.now()
    age = random.randint(min_age, max_age)
    year = today.year - age
    month = random.randint(1, 12)
    # Simple day-of-month (ignore leap year edge cases)
    max_day = 28 if month == 2 else 30 if month in (4, 6, 9, 11) else 31
    day = random.randint(1, max_day)
    return {
        "month": str(month),
        "day": str(day),
        "year": str(year),
        "iso": f"{year:04d}-{month:02d}-{day:02d}",
    }


# --------------------------------------------------------------------------- #
# Browser automation
# --------------------------------------------------------------------------- #
class ChatGPTRegistrationError(RuntimeError):
    """ChatGPT registration flow failure."""


class ChatGPTRegistrationCancelled(ChatGPTRegistrationError):
    """User cancelled registration."""


def _find_and_fill_input(
    page: Any, selectors: list[str], value: str, *, timeout: float = 10.0
) -> bool:
    """Try multiple selectors to find an input and fill it."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(
                sel, timeout=timeout * 1000 / len(selectors), state="visible"
            )
            if el:
                el.click()
                el.fill("")
                el.fill(value)
                return True
        except Exception:
            continue
    return False


def _find_and_click(page: Any, selectors: list[str], *, timeout: float = 10.0) -> bool:
    """Try multiple selectors to find and click a button/link."""
    for sel in selectors:
        try:
            el = page.wait_for_selector(
                sel, timeout=timeout * 1000 / len(selectors), state="visible"
            )
            if el:
                el.click()
                return True
        except Exception:
            continue
    return False


def _safe_page_text(page: Any) -> str:
    try:
        return (page.locator("body").inner_text() or "")[:1000]
    except Exception:
        return ""


def _account_deactivated_visible(page: Any) -> bool:
    """Detect the OpenAI terminal account-deactivated page robustly."""
    needles = (
        "account_deactivated",
        "you do not have an account because it has been deleted or deactivated",
        "account has been deleted or deactivated",
    )
    text = _safe_page_text(page).lower()
    if any(needle in text for needle in needles):
        return True
    try:
        content = str(page.content() or "").lower()
        if any(needle in content for needle in needles):
            return True
    except Exception:
        pass
    try:
        marker = page.get_by_text(
            re.compile(r"account_deactivated|deleted or deactivated", re.I)
        )
        return marker.count() > 0
    except Exception:
        return False


_VERIFICATION_SELECTORS = [
    'input[name="code"]',
    'input[name="otp"]',
    'input[autocomplete="one-time-code"]',
    'input[id*="code" i]',
    'input[id*="otp" i]',
    'input[type="text"][maxlength="6"]',
    'input[type="tel"][maxlength="6"]',
    'input[type="number"][maxlength="6"]',
    'input[aria-label*="code" i]',
    'input[placeholder*="code" i]',
    'input[inputmode="numeric"]',
]


def _first_visible(page: Any, selectors: list[str]) -> Any:
    for selector in selectors:
        try:
            # Auth pages often retain a hidden input from the previous React
            # render. Inspect every match instead of rejecting the selector
            # when its first element happens to be that stale hidden node.
            elements = page.query_selector_all(selector)
            for element in elements:
                if element.is_visible():
                    return element
            # Keep compatibility with lightweight page adapters that only
            # implement query_selector (also useful for non-browser tests).
            if not elements:
                element = page.query_selector(selector)
                if element and element.is_visible():
                    return element
        except Exception:
            continue
    return None


def _visible_elements(page: Any, selector: str) -> list[Any]:
    try:
        return [
            element
            for element in page.query_selector_all(selector)
            if element.is_visible()
        ]
    except Exception:
        return []


def _element_text(element: Any) -> str:
    values: list[str] = []
    for attribute in (None, "value", "aria-label", "title", "data-dd-action-name"):
        try:
            value = (
                element.text_content()
                if attribute is None
                else element.get_attribute(attribute)
            )
            if value:
                values.append(str(value))
        except Exception:
            pass
    return " ".join(values).strip()


def _find_action(page: Any, pattern: str, *, include_disabled: bool = False) -> Any:
    regex = re.compile(pattern, re.I)
    selector = 'button, a, [role="button"], [role="link"], input[type="button"], input[type="submit"]'
    for element in _visible_elements(page, selector):
        try:
            disabled = (
                bool(element.get_attribute("disabled"))
                or element.get_attribute("aria-disabled") == "true"
            )
            if disabled and not include_disabled:
                continue
            if regex.search(_element_text(element)):
                return element
        except Exception:
            continue
    return None


def _verification_target(page: Any) -> tuple[str, Any] | None:
    # Use Locators for OTP fields. Unlike ElementHandle, a Locator resolves the
    # current DOM node again when React replaces the verification form after a
    # resend, so the second fill does not fail with "not attached to the DOM".
    def visible_locators(selector: str) -> list[Any]:
        result: list[Any] = []
        try:
            candidates = page.locator(selector)
            for index in range(candidates.count()):
                candidate = candidates.nth(index)
                if candidate.is_visible():
                    result.append(candidate)
        except Exception:
            pass
        return result

    split = visible_locators('input[maxlength="1"]')
    singles: list[Any] = []
    for selector in _VERIFICATION_SELECTORS:
        singles.extend(visible_locators(selector))
        if singles:
            break
    if not singles:
        try:
            labelled = page.get_by_label(re.compile(r"code|verification|验证码", re.I))
            for index in range(labelled.count()):
                candidate = labelled.nth(index)
                if candidate.is_visible():
                    singles.append(candidate)
        except Exception:
            pass
    single = singles[0] if singles else None
    if single:
        try:
            if int(single.get_attribute("maxlength") or 0) == 1 and len(split) >= 4:
                return "split", split
        except Exception:
            pass
        return "single", single
    if len(split) >= 4:
        return "split", split
    return None


def _verification_submission_pending(page: Any) -> bool:
    """Return True while the verification form visibly reports processing."""
    action = _find_action(
        page,
        r"verify|confirm|submit|continue|确认|验证|继续|verifying|loading|处理中",
        include_disabled=True,
    )
    if not action:
        return False
    try:
        attributes = " ".join(
            str(action.get_attribute(name) or "").strip().lower()
            for name in (
                "aria-busy",
                "aria-disabled",
                "data-loading",
                "data-pending",
                "data-submitting",
                "data-state",
            )
        )
        text = _element_text(action).lower()
        return bool(
            re.search(
                r"\b(?:true|loading|pending|submitting|busy|processing)\b", attributes
            )
            or re.search(
                r"verifying|loading|processing|正在验证|处理中|请稍候", text, re.I
            )
        )
    except Exception:
        return False


def _profile_visible(page: Any) -> bool:
    selectors = [
        'input[name="name"]',
        'input[autocomplete="name"]',
        'input[name="age"]',
        'input[name="birthday"]',
        '[role="spinbutton"][data-type="year"]',
    ]
    return _first_visible(page, selectors) is not None


def _is_logged_in_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(str(raw_url or ""))
        if parsed.hostname not in {"chatgpt.com", "www.chatgpt.com", "chat.openai.com"}:
            return False
        return not re.match(
            r"^/(?:auth/|create-account/|email-verification|log-in|add-phone)",
            parsed.path or "",
        )
    except Exception:
        return False


def _signup_state(page: Any) -> str:
    url = str(page.url or "")
    parsed = urlparse(url)
    path = parsed.path.lower()
    text = _safe_page_text(page).lower()
    if _account_deactivated_visible(page):
        return "account_deactivated"
    if parsed.hostname and parsed.hostname not in {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
        "auth.openai.com",
        "auth0.openai.com",
        "accounts.openai.com",
    }:
        return "external_idp"
    if "max_check_attempts" in text:
        return "security_blocked"
    if "user_already_exists" in text or re.search(
        r"account .*already exists|email address.*already exists", text
    ):
        return "existing_account"
    if "account_deactivated" in text or re.search(
        r"account (?:has been |was )?(?:deleted|deactivated)|"
        r"do not have an account because it has been (?:deleted|deactivated)|"
        r"账号.*(?:删除|停用)|账户.*(?:删除|停用)",
        text,
        re.I,
    ):
        return "account_deactivated"
    if re.search(
        r"something went wrong|operation timed out|failed to fetch|405 method not allowed",
        text,
    ):
        if _find_action(page, r"try\s+again|retry|重试", include_disabled=True):
            return "retry"
    if "create-account-enroll-passkey" in path or re.search(
        r"add passkey|通行密钥", text
    ):
        return "passkey"
    if "/add-phone" in path or re.search(
        r"add (?:a )?phone|phone number required|添加手机号", text
    ):
        return "add_phone"
    if parsed.hostname in {
        "chatgpt.com",
        "www.chatgpt.com",
        "chat.openai.com",
    } and re.search(r"you(?:'| a)?re all set|ready to go|你已准备就绪", text, re.I):
        return "logged_in"
    # The auth app can keep the /email-verification URL (and even the old OTP
    # input in the DOM) after rendering the profile step. Prefer the visible
    # profile controls so a successful code is not mistaken for a failed one.
    if _profile_visible(page) or re.search(
        r"/(?:create-account/profile|signup/profile|about-you)", path
    ):
        return "profile"
    if _verification_target(page) or "/email-verification" in path:
        return "verification"
    if _first_visible(
        page, ['input[name="new-password"]', 'input[autocomplete="new-password"]']
    ):
        return "new_password"
    if _first_visible(page, ['input[autocomplete="current-password"]']):
        return "existing_account"
    if _is_logged_in_url(url):
        return "logged_in"
    return "unknown"


def _wait_for_signup_state(
    page: Any, check_cancel: Callable[[], None], timeout: float
) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        check_cancel()
        state = _signup_state(page)
        if state != "unknown":
            return state
        page.wait_for_timeout(200)
    return _signup_state(page)


def _recover_retry_page(
    page: Any, check_cancel: Callable[[], None], *, attempts: int = 2
) -> bool:
    for _ in range(attempts):
        check_cancel()
        if _signup_state(page) != "retry":
            return True
        button = _find_action(page, r"try\s+again|retry|重试")
        if not button:
            page.wait_for_timeout(500)
            continue
        button.click()
        page.wait_for_timeout(1500)
    return _signup_state(page) != "retry"


def _skip_passkey(page: Any) -> bool:
    button = _find_action(page, r"^\s*skip\s*$|跳过")
    if not button:
        return False
    button.click()
    page.wait_for_timeout(1200)
    return True


def _fill_profile_fields(
    page: Any, first_name: str, last_name: str, birthdate: dict[str, str]
) -> None:
    full_name = f"{first_name} {last_name}"
    first = _first_visible(
        page,
        [
            'input[name="firstname"]',
            'input[name="given-name"]',
            'input[aria-label*="first name" i]',
            'input[placeholder*="first name" i]',
        ],
    )
    last = _first_visible(
        page,
        [
            'input[name="lastname"]',
            'input[name="family-name"]',
            'input[aria-label*="last name" i]',
            'input[placeholder*="last name" i]',
        ],
    )
    if first and last:
        first.fill(first_name)
        last.fill(last_name)
    else:
        name = _first_visible(
            page,
            [
                'input[name="name"]',
                'input[autocomplete="name"]',
                'input[aria-label*="full name" i]',
                'input[placeholder*="full name" i]',
            ],
        )
        if not name:
            raise ChatGPTRegistrationError(f"未找到姓名输入框: {page.url}")
        name.fill(full_name)

    age = _first_visible(page, ['input[name="age"]'])
    if age:
        age.fill(str(max(18, datetime.now().year - int(birthdate["year"]))))
    else:
        parts = {
            "year": birthdate["year"],
            "month": birthdate["month"].zfill(2),
            "day": birthdate["day"].zfill(2),
        }
        filled_parts = 0
        for key, value in parts.items():
            element = _first_visible(
                page,
                [
                    f'[role="spinbutton"][data-type="{key}"]',
                    f'input[name="{key}"]',
                    f'input[aria-label*="{key}" i]',
                    f'select[name="{key}"]',
                    f'select[aria-label*="{key}" i]',
                ],
            )
            if not element:
                continue
            tag = str(element.evaluate("el => el.tagName.toLowerCase()"))
            if tag == "select":
                element.select_option(str(int(value)))
            else:
                try:
                    element.fill(value)
                except Exception:
                    element.click()
                    element.press("Control+A")
                    element.type(value)
            filled_parts += 1

        hidden_birthday = page.query_selector('input[name="birthday"]')
        if hidden_birthday:
            hidden_birthday.evaluate(
                "(el, value) => { el.value=value; el.dispatchEvent(new Event('input',{bubbles:true})); "
                "el.dispatchEvent(new Event('change',{bubbles:true})); }",
                birthdate["iso"],
            )
        if filled_parts < 3 and not hidden_birthday:
            raise ChatGPTRegistrationError(f"未找到完整的生日或年龄输入项: {page.url}")

    for checkbox in _visible_elements(page, 'input[type="checkbox"]'):
        try:
            parent_text = str(
                checkbox.evaluate(
                    "el => (el.closest('label,section,div')?.innerText || '')"
                )
            )
            if (
                re.search(r"agree|同意", parent_text, re.I)
                and not checkbox.is_checked()
            ):
                checkbox.check()
        except Exception:
            continue


def _submit_for_element(page: Any, element: Any, pattern: str) -> bool:
    try:
        form = (
            element.evaluate_handle("el => el.form || el.closest('form')")
            if element
            else None
        )
        form_element = form.as_element() if form else None
        if form_element:
            candidates = [
                candidate
                for candidate in form_element.query_selector_all(
                    'button[type="submit"], input[type="submit"]'
                )
                if candidate.is_visible() and candidate.is_enabled()
            ]
            regex = re.compile(pattern, re.I)
            submit = next(
                (
                    candidate
                    for candidate in candidates
                    if regex.search(_element_text(candidate))
                ),
                candidates[0] if len(candidates) == 1 else None,
            )
            if submit:
                submit.click()
                return True
    except Exception:
        pass
    action = _find_action(page, pattern)
    if action:
        action.click()
        return True
    return False


def _profile_submit_button(page: Any) -> Any:
    direct = _first_visible(page, ['button[type="submit"]', 'input[type="submit"]'])
    return direct or _find_action(page, r"完成|创建|create|continue|finish|done|agree")


def _action_clickable(element: Any) -> bool:
    if not element:
        return False
    try:
        if not element.is_visible() or not element.is_enabled():
            return False
        if element.get_attribute("aria-busy") == "true":
            return False
        pending = " ".join(
            str(element.get_attribute(name) or "").strip().lower()
            for name in (
                "data-loading",
                "data-pending",
                "data-submitting",
                "data-state",
            )
        )
        return not re.search(r"\b(?:true|loading|pending|submitting|busy)\b", pending)
    except Exception:
        return False


class ChatGPTBrowserRuntime:
    """Reusable browser process; every account still gets a fresh context."""

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
        debug_window_width = 800
        debug_window_height = 560
        try:
            from camoufox.sync_api import Camoufox

            options: dict[str, Any] = {"headless": headless}
            if not headless:
                options["window"] = (debug_window_width, debug_window_height)
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
            raise RuntimeError(f"Camoufox 启动失败：{exc}") from exc
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


def _browser_context() -> BrowserContext:
    """Expose live browser helpers to the registration workflow."""
    return BrowserContext(globals())


def register_chatgpt_account(
    *,
    email: str,
    password: str,
    get_verification_code: Callable[..., str | None],
    proxy: dict[str, str] | None = None,
    headless: bool = True,
    timeout_sec: float = 300.0,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[str], None] | None = None,
    browser_data_dir: str | None = None,
    operation_delay_ms: int = 3000,
    browser_runtime: ChatGPTBrowserRuntime | None = None,
) -> dict[str, Any]:
    return _registration.register_chatgpt_account(
        _browser_context(),
        email=email,
        password=password,
        get_verification_code=get_verification_code,
        proxy=proxy,
        headless=headless,
        timeout_sec=timeout_sec,
        should_cancel=should_cancel,
        on_progress=on_progress,
        browser_data_dir=browser_data_dir,
        operation_delay_ms=operation_delay_ms,
        browser_runtime=browser_runtime,
    )
