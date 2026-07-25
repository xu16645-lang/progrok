import sys
import threading
import time
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app
import grok_build_adapter as grok_adapter
import sso_to_auth_json
from grok_build_adapter import _make_email_receiver, _snapshot_reg_config
from xai_browser import (
    XAI_GROK_URL,
    XAI_SIGNUP_URL,
    XaiBrowserRuntime,
    XaiVisibleRegistration,
)


class _Page:
    def __init__(self):
        self.gotos = []
        self.viewport = None
        self.closed = False
        self.url = XAI_SIGNUP_URL

    def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))
        self.url = url

    def wait_for_timeout(self, _milliseconds):
        return None

    def set_viewport_size(self, value):
        self.viewport = value

    def evaluate(self, _script):
        return None

    def close(self):
        self.closed = True


class _Action:
    def __init__(self):
        self.click_kwargs = None
        self.scrolled = False

    def is_visible(self, **_kwargs):
        return True

    def scroll_into_view_if_needed(self, **_kwargs):
        self.scrolled = True

    def bounding_box(self, **_kwargs):
        if not self.scrolled:
            return {"x": 10, "y": 700, "width": 100, "height": 40}
        return {"x": 10, "y": 300, "width": 100, "height": 40}

    def click(self, **kwargs):
        self.click_kwargs = kwargs


class _Context:
    def __init__(self):
        self.page = _Page()
        self.cookies = []
        self.cleared = False
        self.closed = False

    def new_page(self):
        return self.page

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def clear_cookies(self):
        self.cleared = True

    def close(self):
        self.closed = True


class _Browser:
    def __init__(self):
        self.context = _Context()
        self.kwargs = None

    def new_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class _Runtime:
    def __init__(self):
        self.browser = _Browser()

    def ensure(self, **_kwargs):
        return self.browser, False, False

    def close(self):
        return None


class XaiBrowserTests(unittest.TestCase):
    def test_click_action_uses_resolved_element_center(self):
        visual = XaiVisibleRegistration()
        action = _Action()
        with (
            patch.object(visual, "_find_action", return_value=action),
            patch.object(
                visual,
                "_click_locator_center",
                return_value=True,
            ) as click_center,
        ):
            self.assertTrue(visual._click_action("sign up with email"))

        click_center.assert_called_once_with(action, timeout=3_000)

    def test_click_locator_scrolls_offscreen_action_before_mouse_click(self):
        class _Mouse:
            def __init__(self):
                self.clicks = []

            def click(self, x, y):
                self.clicks.append((x, y))

        visual = XaiVisibleRegistration()
        visual.page = _Page()
        visual.page.mouse = _Mouse()
        action = _Action()

        self.assertTrue(visual._click_locator_center(action))
        self.assertTrue(action.scrolled)
        self.assertEqual(visual.page.mouse.clicks, [(60.0, 320.0)])

    def test_signup_form_waits_until_email_input_is_visible(self):
        visual = XaiVisibleRegistration()
        visual.page = _Page()
        email_input = object()
        with (
            patch.object(
                visual,
                "_first_visible",
                side_effect=[None, None, email_input],
            ),
            patch.object(visual, "_wait") as wait,
        ):
            self.assertTrue(visual._wait_for_email_signup_form())

        self.assertEqual(wait.call_count, 2)
        wait.assert_called_with(200)

    def test_signup_transition_ignores_transient_form_disappearance(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        visual.page = _Page()
        sso_results = iter([(None, {}), (None, {}), ("sso-token", {})])

        with (
            patch.object(visual, "_extract_sso", side_effect=lambda: next(sso_results)),
            patch.object(visual, "_signup_form_present", side_effect=[False, True]),
            patch.object(visual, "_find_action", return_value=None),
            patch.object(visual, "_wait") as wait,
        ):
            visual._wait_for_signup_transition("complete sign up")

        self.assertEqual(wait.call_count, 2)
        wait.assert_called_with(250)
        self.assertTrue(any("evidence=sso_cookie" in event for event in events))
        self.assertFalse(any("evidence=form_absent" in event for event in events))

    def test_signup_transition_does_not_accept_url_change_while_form_remains(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        visual.page = _Page()
        initial_url = visual.page.url
        sso_results = iter([(None, {}), ("sso-token", {})])

        def extract_sso():
            result = next(sso_results)
            visual.page.url = f"{initial_url}#validation"
            return result

        with (
            patch.object(visual, "_extract_sso", side_effect=extract_sso),
            patch.object(visual, "_signup_form_present", return_value=True),
            patch.object(visual, "_find_action", return_value=None),
            patch.object(visual, "_wait") as wait,
        ):
            visual._wait_for_signup_transition("complete sign up")

        wait.assert_called_once_with(250)
        self.assertTrue(any("evidence=sso_cookie" in event for event in events))

    def test_signup_transition_accepts_stably_absent_form(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        visual.page = _Page()

        with (
            patch.object(visual, "_extract_sso", return_value=(None, {})),
            patch.object(visual, "_signup_form_present", return_value=False),
            patch.object(visual, "_wait") as wait,
        ):
            visual._wait_for_signup_transition("complete sign up")

        self.assertEqual(wait.call_count, 2)
        self.assertTrue(any("evidence=form_absent" in event for event in events))

    def test_hidden_turnstile_container_does_not_mark_widget_pending(self):
        class _Node:
            def is_visible(self, **_kwargs):
                return False

        class _Collection:
            def __init__(self, nodes=None):
                self.nodes = list(nodes or [])

            def count(self):
                return len(self.nodes)

            def nth(self, index):
                return self.nodes[index]

        class _TurnstilePage(_Page):
            frames = []

            def locator(self, selector):
                if "data-sitekey" in selector:
                    return _Collection([_Node()])
                return _Collection()

        visual = XaiVisibleRegistration()
        visual.page = _TurnstilePage()

        self.assertEqual(visual._turnstile_status(), "absent")

    def test_generated_profile_is_not_the_old_fixed_placeholder(self):
        with patch("xai_browser.secrets.choice", side_effect=["Aiden", "Carter"]):
            generated = XaiVisibleRegistration._generate_profile()

        self.assertEqual(generated, ("Aiden", "Carter"))
        self.assertNotEqual(generated, ("User", "Grok"))

    def test_profile_and_password_are_filled_before_verification_and_submit(self):
        events = []

        class _Input:
            def __init__(self, name):
                self.name = name
                self.value = ""

            def fill(self, value):
                self.value = value
                events.append(("fill", self.name, value))

            def input_value(self, **_kwargs):
                return self.value

        visual = XaiVisibleRegistration(on_progress=lambda _message: None)
        visual.page = _Page()
        targets = [None, _Input("password"), _Input("first"), _Input("last")]
        sso_results = iter([(None, {}), ("sso-token", {"sso": "sso-token"})])

        def extract_sso():
            result = next(sso_results)
            if result[0]:
                visual.page.url = XAI_GROK_URL
            return result

        with (
            patch.object(visual, "open"),
            patch.object(visual, "_extract_sso", side_effect=extract_sso),
            patch.object(visual, "_first_visible", side_effect=targets),
            patch.object(visual, "_verification_target", return_value=None),
            patch.object(visual, "_raise_page_error"),
            patch.object(visual, "_action_delay"),
            patch.object(
                visual,
                "_wait_for_turnstile",
                side_effect=lambda: events.append(("turnstile",)),
            ),
            patch.object(visual, "_wait", side_effect=lambda ms: events.append(("wait", ms))),
            patch.object(visual, "_settle_page", side_effect=lambda: events.append(("settle",))),
            patch.object(visual, "_submit", side_effect=lambda *_args: events.append(("submit",))),
            patch.object(visual, "_wait_for_signup_transition"),
        ):
            result = visual.register_account(
                email="person@example.test",
                password="random-password",
                get_verification_code=lambda *_args: None,
            )

        fill_names = [event[1] for event in events if event[0] == "fill"]
        self.assertEqual(fill_names, ["first", "last", "password"])
        password_fill = events.index(("fill", "password", "random-password"))
        verification = events.index(("turnstile",))
        submit = events.index(("submit",))
        self.assertLess(password_fill, verification)
        self.assertLess(verification, submit)
        self.assertTrue(result["ok"])

    def test_turnstile_absent_waits_grace_before_skipping(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        visual.page = _Page()
        visual.deadline = time.time() + 30.0
        with (
            patch.object(visual, "_turnstile_status", return_value="absent"),
            patch.object(visual, "_wait") as wait,
            patch.object(visual, "_click_turnstile_challenge") as click,
            patch("xai_browser.TURNSTILE_MOUNT_GRACE_SEC", 0.0),
        ):
            visual._wait_for_turnstile()

        wait.assert_not_called()
        click.assert_not_called()
        self.assertTrue(any("waiting_for_widget_mount" in x for x in events))
        self.assertTrue(any("human_verification: not_required" in x for x in events))

    def test_turnstile_delayed_mount_is_not_skipped(self):
        events = []
        visual = XaiVisibleRegistration(headless=False, on_progress=events.append)
        visual.page = _Page()
        with (
            patch.object(
                visual,
                "_turnstile_status",
                side_effect=["absent", "pending", "passed"],
            ),
            patch.object(visual, "_turnstile_needs_click", return_value=False),
            patch.object(visual, "_click_turnstile_challenge") as click,
            patch.object(visual, "_wait") as wait,
            patch("xai_browser.TURNSTILE_MOUNT_GRACE_SEC", 5.0),
        ):
            visual._wait_for_turnstile()

        click.assert_not_called()
        wait.assert_called()
        self.assertTrue(any("waiting_for_widget_mount" in x for x in events))
        self.assertTrue(any("human_verification: detected" in x for x in events))
        self.assertTrue(any("human_verification: passed" in x for x in events))
        self.assertFalse(any("not_required" in x for x in events))

    def test_turnstile_auto_pass_does_not_click(self):
        events = []
        visual = XaiVisibleRegistration(headless=False, on_progress=events.append)
        visual.page = _Page()
        with (
            patch.object(
                visual,
                "_turnstile_status",
                side_effect=["pending", "pending", "passed"],
            ),
            patch.object(visual, "_turnstile_needs_click", return_value=False) as needs_click,
            patch.object(visual, "_click_turnstile_challenge") as click,
            patch.object(visual, "_wait") as wait,
        ):
            visual._wait_for_turnstile()

        needs_click.assert_called()
        click.assert_not_called()
        wait.assert_called_once_with(250)
        self.assertTrue(any("waiting_for_auto_pass" in x for x in events))
        self.assertTrue(any("human_verification: passed" in x for x in events))
        self.assertFalse(any("clicked_challenge" in x for x in events))

    def test_turnstile_clicks_only_when_checkbox_required(self):
        events = []
        visual = XaiVisibleRegistration(headless=False, on_progress=events.append)
        visual.page = _Page()
        with (
            patch.object(
                visual,
                "_turnstile_status",
                # initial + loop(no click) + loop(click) + post-click recheck
                side_effect=["pending", "pending", "pending", "passed"],
            ),
            patch.object(
                visual,
                "_turnstile_needs_click",
                side_effect=[False, True],
            ),
            patch.object(visual, "_click_turnstile_challenge", return_value=True) as click,
            patch.object(visual, "_wait") as wait,
        ):
            visual._wait_for_turnstile()

        click.assert_called_once()
        # One settle wait before the effective click; post-click pass returns immediately.
        self.assertEqual(wait.call_count, 1)
        wait.assert_called_with(250)
        self.assertTrue(any("manual_click_required" in x for x in events))
        self.assertTrue(any("clicked_challenge" in x for x in events))
        self.assertTrue(any("human_verification: passed" in x for x in events))

    def test_hidden_checkbox_force_click_without_status_change_is_not_success(self):
        class _Locator:
            def __init__(
                self,
                *,
                visible=True,
                box=None,
                children=None,
                count=1,
                text="",
            ):
                self._visible = visible
                self._box = box
                self._children = children or []
                self._count = count
                self._text = text
                self.clicks = 0
                self.force_clicks = 0

            def count(self):
                return self._count

            def nth(self, _index):
                return self

            @property
            def first(self):
                return self

            def is_visible(self, **_kwargs):
                return self._visible

            def bounding_box(self, **_kwargs):
                return self._box

            def scroll_into_view_if_needed(self, **_kwargs):
                return None

            def click(self, **kwargs):
                self.clicks += 1
                if kwargs.get("force"):
                    self.force_clicks += 1

            def element_handle(self, **_kwargs):
                return self

            def content_frame(self):
                return self

            def locator(self, selector):
                for key, child in self._children:
                    if key == selector:
                        return child
                if selector == "body":
                    return _Locator(visible=True, text=self._text, count=1)
                return _Locator(visible=False, count=0)

            def inner_text(self, **_kwargs):
                return self._text

        checkbox = _Locator(
            visible=False,
            box={"x": 10, "y": 20, "width": 24, "height": 24},
        )
        label = _Locator(
            visible=True,
            box={"x": 12, "y": 22, "width": 40, "height": 40},
        )
        iframe = _Locator(
            visible=True,
            box={"x": 100, "y": 200, "width": 300, "height": 65},
            text="Verify you are human",
            children=[
                ('input[type="checkbox"]', checkbox),
                ("label.cb-lb", label),
                (".cb-lb", label),
            ],
        )

        class _Mouse:
            def __init__(self):
                self.clicks = []

            def click(self, x, y):
                self.clicks.append((x, y))

        class _TurnstilePage(_Page):
            def __init__(self):
                super().__init__()
                self.mouse = _Mouse()
                self._iframe = iframe

            def locator(self, selector):
                if "challenges.cloudflare.com" in selector or "turnstile" in selector:
                    return self._iframe
                return _Locator(visible=False, count=0)

        visual = XaiVisibleRegistration(on_progress=lambda _msg: None)
        visual.page = _TurnstilePage()
        statuses = iter(["pending", "pending", "pending", "pending", "pending"])

        with (
            patch.object(visual, "_turnstile_status", side_effect=lambda: next(statuses)),
            patch.object(visual, "_wait"),
        ):
            # Every strategy dispatches, but status never advances -> not success.
            self.assertTrue(visual._turnstile_needs_click())
            self.assertFalse(visual._click_turnstile_challenge())

        self.assertEqual(checkbox.force_clicks, 1)
        # After no-effect checkbox, surface/iframe strategies continue.
        self.assertGreaterEqual(len(visual.page.mouse.clicks), 1)

    def test_click_turnstile_falls_through_until_status_advances(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        visual.page = _Page()
        statuses = iter(["pending", "pending", "finishing"])
        targets = [
            ("checkbox", object()),
            ("surface", object()),
            ("iframe", object()),
        ]

        def dispatch(kind, _target):
            events.append(("dispatch", kind))
            return True

        with (
            patch.object(visual, "_turnstile_status", side_effect=lambda: next(statuses)),
            patch.object(visual, "_turnstile_click_targets", return_value=targets),
            patch.object(visual, "_dispatch_turnstile_target_click", side_effect=dispatch),
            patch.object(visual, "_wait") as wait,
        ):
            self.assertTrue(visual._click_turnstile_challenge())

        # First click has no effect; second advances status.
        self.assertEqual(
            [item for item in events if item[0] == "dispatch"],
            [("dispatch", "checkbox"), ("dispatch", "surface")],
        )
        self.assertTrue(any("click_no_effect_checkbox" in x for x in events if isinstance(x, str)))
        self.assertTrue(any("click_effect_surface" in x for x in events if isinstance(x, str)))
        self.assertEqual(wait.call_count, 2)

    def test_pending_to_absent_is_not_click_success(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        visual.page = _Page()
        statuses = iter(["pending", "absent", "absent", "absent"])
        targets = [
            ("checkbox", object()),
            ("surface", object()),
            ("iframe", object()),
        ]

        def dispatch(kind, _target):
            events.append(("dispatch", kind))
            return True

        with (
            patch.object(visual, "_turnstile_status", side_effect=lambda: next(statuses)),
            patch.object(visual, "_turnstile_click_targets", return_value=targets),
            patch.object(visual, "_dispatch_turnstile_target_click", side_effect=dispatch),
            patch.object(visual, "_wait"),
        ):
            self.assertFalse(visual._click_turnstile_challenge())

        self.assertEqual(len([item for item in events if item[0] == "dispatch"]), 3)
        self.assertTrue(any("click_widget_missing_" in x for x in events if isinstance(x, str)))
        self.assertFalse(any("click_effect_" in x for x in events if isinstance(x, str)))

    def test_turnstile_iframe_locators_dedupe_by_dom_identity(self):
        class _Handle:
            def __init__(self, identity):
                self.identity = identity

            def evaluate(self, _script):
                return self.identity

        class _Iframe:
            def __init__(self, identity):
                self.identity = identity

            def is_visible(self, **_kwargs):
                return True

            def element_handle(self, **_kwargs):
                return _Handle(self.identity)

            def bounding_box(self, **_kwargs):
                return {"x": 1, "y": 2, "width": 3, "height": 4}

        class _Locator:
            def __init__(self, frames):
                self._frames = frames

            def count(self):
                return len(self._frames)

            def nth(self, index):
                return self._frames[index]

        shared = _Iframe("iframe|same-dom")
        frames_by_selector = {
            'iframe[src*="challenges.cloudflare.com"]': [shared],
            'iframe[src*="turnstile"]': [shared],
            'iframe[title*="cloudflare" i]': [shared],
        }

        class _PageWithDupes(_Page):
            def locator(self, selector):
                return _Locator(frames_by_selector.get(selector, []))

        visual = XaiVisibleRegistration()
        visual.page = _PageWithDupes()
        iframes = visual._turnstile_iframe_locators()
        self.assertEqual(len(iframes), 1)

    def test_unreadable_iframe_content_does_not_require_click(self):
        class _Iframe:
            def is_visible(self, **_kwargs):
                return True

            def element_handle(self, **_kwargs):
                return self

            def content_frame(self):
                return None

        class _Locator:
            def __init__(self, iframe):
                self._iframe = iframe

            def count(self):
                return 1

            def nth(self, _index):
                return self._iframe

        class _Empty:
            def count(self):
                return 0

            def nth(self, _i):
                return _Iframe()

        class _PageWithLocator(_Page):
            def locator(self, selector):
                if "challenges.cloudflare.com" in selector or "turnstile" in selector:
                    return _Locator(_Iframe())
                return _Empty()

        visual = XaiVisibleRegistration()
        visual.page = _PageWithLocator()
        self.assertFalse(visual._turnstile_needs_click())
        self.assertEqual(visual._turnstile_click_targets(), [])

    def test_turnstile_success_text_waits_for_response_token(self):
        events = []
        visual = XaiVisibleRegistration(headless=False, on_progress=events.append)
        visual.page = _Page()
        with (
            patch.object(
                visual,
                "_turnstile_status",
                side_effect=["pending", "finishing", "passed"],
            ),
            patch.object(visual, "_turnstile_needs_click") as needs_click,
            patch.object(visual, "_click_turnstile_challenge") as click,
            patch.object(visual, "_wait") as wait,
        ):
            visual._wait_for_turnstile()

        needs_click.assert_not_called()
        click.assert_not_called()
        wait.assert_called_once_with(250)
        self.assertTrue(any("waiting_for_response_token" in x for x in events))
        self.assertTrue(any("human_verification: passed" in x for x in events))
        self.assertFalse(any("passed_after_widget_success" in x for x in events))

    def test_turnstile_widget_success_without_token_does_not_pass(self):
        events = []
        visual = XaiVisibleRegistration(headless=False, on_progress=events.append)
        visual.page = _Page()
        visual.deadline = time.time() + 1.0
        with (
            patch.object(visual, "_turnstile_status", return_value="finishing"),
            patch.object(visual, "_find_action", return_value=object()) as find_action,
            patch.object(visual, "_wait", side_effect=lambda _ms: None),
            patch("xai_browser.TURNSTILE_WAIT_SEC", 0.01),
            self.assertRaisesRegex(Exception, "超时|手工|自动"),
        ):
            visual._wait_for_turnstile()

        find_action.assert_not_called()
        self.assertTrue(any("waiting_for_response_token" in x for x in events))
        self.assertFalse(any("passed_after_widget_success" in x for x in events))
        self.assertFalse(any(x.endswith(": passed") for x in events))

    def test_password_profile_page_is_not_misdetected_as_verification(self):
        visual = XaiVisibleRegistration()
        visual.page = _Page()
        password = object()
        with patch.object(visual, "_first_visible", return_value=password):
            self.assertIsNone(visual._verification_target())

    def test_turnstile_click_retries_are_bounded(self):
        visual = XaiVisibleRegistration(headless=False)
        visual.page = _Page()
        with (
            patch.object(
                visual,
                "_turnstile_status",
                # Each attempt: loop status(pending) + post-click recheck(pending),
                # then final pass after third click.
                side_effect=[
                    "pending",  # initial
                    "pending",
                    "pending",  # attempt 1 + recheck
                    "pending",
                    "pending",  # attempt 2 + recheck
                    "pending",
                    "passed",  # attempt 3 + recheck
                ],
            ),
            patch.object(visual, "_turnstile_needs_click", return_value=True),
            patch.object(visual, "_click_turnstile_challenge", return_value=True) as click,
            patch.object(visual, "_wait"),
            patch("xai_browser.TURNSTILE_CLICK_COOLDOWN_SEC", 0.0),
        ):
            visual._wait_for_turnstile()

        self.assertEqual(click.call_count, 3)

    def test_sub2_oauth_callback_requires_matching_state(self):
        callback = "http://127.0.0.1:56121/callback?code=code-1&state=state-1"

        self.assertEqual(
            XaiVisibleRegistration._callback_from_url(callback, "state-1"),
            callback,
        )
        with self.assertRaisesRegex(Exception, "state"):
            XaiVisibleRegistration._callback_from_url(callback, "other-state")

    def test_grok_monitor_projection_redacts_runtime_fields(self):
        session = {
            "id": "session-1",
            "password": "secret",
            "yescaptcha_key": "captcha-secret",
            "_receiver": object(),
            "auth_json": {
                "imported": [
                    {"id": "account-1", "email": "user@example.test"},
                ]
            },
        }

        result = grok_adapter._compact_session(session)

        self.assertNotIn("password", result)
        self.assertNotIn("yescaptcha_key", result)
        self.assertNotIn("_receiver", result)
        self.assertNotIn("auth_json", result)
        self.assertEqual(result["auth_json_count"], 1)
        self.assertEqual(result["imported_account_ids"], ["account-1"])

    def test_proxy_key_keeps_credentials_distinct(self):
        first = XaiBrowserRuntime._key(
            {"server": "http://proxy", "username": "one", "password": "secret"}
        )
        second = XaiBrowserRuntime._key(
            {"server": "http://proxy", "username": "two", "password": "secret"}
        )
        self.assertNotEqual(first, second)

    def test_visible_runtime_disables_background_window_throttling(self):
        captured = {}

        class _Manager:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def start(self):
                return _Browser()

            def __exit__(self, *_args):
                return None

        runtime = XaiBrowserRuntime()
        camoufox_module = types.ModuleType("camoufox")
        sync_api_module = types.ModuleType("camoufox.sync_api")
        sync_api_module.Camoufox = _Manager
        camoufox_module.sync_api = sync_api_module
        with (
            patch("xai_browser._ensure_camoufox"),
            patch.dict(
                sys.modules,
                {
                    "camoufox": camoufox_module,
                    "camoufox.sync_api": sync_api_module,
                },
            ),
        ):
            runtime.ensure(headless=False, proxy=None)
            runtime.close()

        prefs = captured["firefox_user_prefs"]
        self.assertFalse(prefs["widget.windows.window_occlusion_tracking.enabled"])
        self.assertFalse(prefs["dom.timeout.enable_budget_timer_throttling"])

    def test_visible_context_uses_real_signup_url_and_clears_cookies(self):
        events = []
        runtime = _Runtime()
        visual = XaiVisibleRegistration(
            runtime=runtime,
            headless=False,
            on_progress=events.append,
        )

        visual.open()
        visual.sync_cookies({"sso": "token"})

        self.assertEqual(runtime.browser.context.page.gotos[0][0], XAI_SIGNUP_URL)
        self.assertEqual(runtime.browser.kwargs["viewport"], {"width": 760, "height": 480})
        self.assertEqual(len(runtime.browser.context.cookies), 2)
        self.assertTrue(any("private_context_created" in event for event in events))
        self.assertTrue(any("init: cookies_synced" in event for event in events))

        visual.close()

        self.assertTrue(runtime.browser.context.cleared)
        self.assertTrue(runtime.browser.context.closed)

    def test_protocol_authorization_injects_cookies_before_first_navigation(self):
        events = []
        runtime = _Runtime()
        visual = XaiVisibleRegistration(
            runtime=runtime,
            headless=True,
            on_progress=events.append,
        )

        visual.open_authorization({"sso": "session-token", "sso-rw": "session-token"})

        self.assertEqual(runtime.browser.context.page.gotos[0][0], XAI_GROK_URL)
        self.assertEqual(len(runtime.browser.context.cookies), 4)
        self.assertTrue(any("session: cookies_synced" in event for event in events))
        self.assertTrue(
            any("session: authenticated_landing_ready" in event for event in events)
        )
        self.assertTrue(any("session: authorization_ready" in event for event in events))
        visual.close()

    def test_protocol_authorization_can_require_full_browser_login(self):
        events = []
        runtime = _Runtime()
        visual = XaiVisibleRegistration(
            runtime=runtime,
            headless=True,
            on_progress=events.append,
        )

        def complete_login(_email, _password):
            runtime.browser.context.page.url = XAI_GROK_URL

        with patch.object(
            visual, "_login_for_authorization", side_effect=complete_login
        ) as login:
            visual.open_authorization(
                {"sso": "session-token"},
                email="account@example.test",
                password="secret-password",
                force_full_login=True,
            )

        login.assert_called_once_with("account@example.test", "secret-password")
        self.assertEqual(runtime.browser.context.cookies, [])
        self.assertTrue(any("session: authorization_ready" in event for event in events))
        visual.close()

    def test_full_login_recognizes_login_with_email_entry(self):
        visual = XaiVisibleRegistration()
        visual.page = _Page()
        clock = {"value": 100.0}
        clicked = []

        def wait(milliseconds):
            clock["value"] += max(0.2, milliseconds / 1000.0)
            if clicked:
                visual.page.url = XAI_GROK_URL

        def click_action(pattern):
            clicked.append(pattern)
            return True

        with (
            patch("xai_browser.time.time", side_effect=lambda: clock["value"]),
            patch.object(visual, "_wait", side_effect=wait),
            patch.object(visual, "_page_text", return_value="Login with email"),
            patch.object(visual, "_first_visible", return_value=None),
            patch.object(visual, "_click_action", side_effect=click_action),
            patch.object(
                visual,
                "_extract_sso",
                side_effect=[(None, {}), ("browser-sso", {})],
            ),
            patch.object(visual, "_raise_page_error"),
            patch.object(visual, "_settle_page", side_effect=lambda: wait(600)),
        ):
            visual._login_for_authorization(
                "account@example.test", "secret-password"
            )

        self.assertEqual(len(clicked), 1)
        self.assertIn("login with email", clicked[0])

    def test_full_login_skips_readonly_email_and_fills_password(self):
        visual = XaiVisibleRegistration()
        visual.page = _Page()
        clock = {"value": 100.0}

        class _Field:
            def __init__(self, editable):
                self.editable = editable
                self.values = []

            def is_editable(self, **_kwargs):
                return self.editable

            def fill(self, value):
                self.values.append(value)

        email_field = _Field(False)
        password_field = _Field(True)

        def first_visible(selectors):
            joined = " ".join(selectors)
            return password_field if "password" in joined else email_field

        def submit(_anchor, _pattern):
            visual.page.url = XAI_GROK_URL

        def wait(milliseconds):
            clock["value"] += max(0.2, milliseconds / 1000.0)

        with (
            patch("xai_browser.time.time", side_effect=lambda: clock["value"]),
            patch.object(visual, "_wait", side_effect=wait),
            patch.object(visual, "_page_text", return_value=""),
            patch.object(visual, "_first_visible", side_effect=first_visible),
            patch.object(visual, "_submit", side_effect=submit),
            patch.object(visual, "_wait_for_turnstile"),
            patch.object(visual, "_settle_page", side_effect=lambda: wait(600)),
            patch.object(
                visual,
                "_extract_sso",
                side_effect=[(None, {}), ("browser-sso", {})],
            ),
            patch.object(visual, "_raise_page_error"),
        ):
            visual._login_for_authorization(
                "account@example.test", "secret-password"
            )

        self.assertEqual(email_field.values, [])
        self.assertEqual(password_field.values, ["secret-password"])

    def test_device_authorization_submits_each_page_once_until_state_changes(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        page = _Page()
        state = {"value": "code", "code_clicks": 0, "allow_clicks": 0}
        clock = {"value": 100.0}

        def goto(url, **kwargs):
            page.gotos.append((url, kwargs))
            page.url = url

        def wait(milliseconds):
            clock["value"] += max(0.2, milliseconds / 1000.0)

        class _CodeInput:
            def fill(self, _value):
                return None

        def submit(_target, _pattern):
            state["code_clicks"] += 1
            state["value"] = "consent"

        def click_allow(_action, **_kwargs):
            state["allow_clicks"] += 1
            state["value"] = "done"
            return True

        page.goto = goto
        visual.page = page
        visual.deadline = 1000.0
        with (
            patch("xai_browser.time.time", side_effect=lambda: clock["value"]),
            patch.object(visual, "_wait", side_effect=wait),
            patch.object(
                visual,
                "_page_text",
                side_effect=lambda: (
                    "authorization complete" if state["value"] == "done" else ""
                ),
            ),
            patch.object(
                visual,
                "_first_visible",
                side_effect=lambda _selectors: (
                    _CodeInput() if state["value"] == "code" else None
                ),
            ),
            patch.object(visual, "_submit", side_effect=submit),
            patch.object(
                visual,
                "_find_action",
                side_effect=lambda _pattern: (
                    _Action() if state["value"] == "consent" else None
                ),
            ),
            patch.object(visual, "_click_locator_center", side_effect=click_allow),
        ):
            visual.authorize_device(
                "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
                "ABCD-1234",
            )

        self.assertEqual(state["code_clicks"], 1)
        self.assertEqual(state["allow_clicks"], 1)
        self.assertTrue(any("oauth: approved" in event for event in events))

    def test_device_authorization_waits_for_allow_without_resubmitting_code(self):
        events = []
        visual = XaiVisibleRegistration(on_progress=events.append)
        page = _Page()
        clock = {"value": 100.0}
        state = {
            "value": "code",
            "code_clicks": 0,
            "allow_clicks": 0,
            "busy_polls": 0,
        }

        class _CodeInput:
            def fill(self, _value):
                return None

        def goto(url, **kwargs):
            page.gotos.append((url, kwargs))
            page.url = url

        def wait(milliseconds):
            clock["value"] += max(0.2, milliseconds / 1000.0)
            if state["busy_polls"] >= 1 and clock["value"] >= 102.0:
                state["value"] = "consent"

        def submit(_target, _pattern):
            state["code_clicks"] += 1

        def click_allow(_action, **_kwargs):
            state["allow_clicks"] += 1
            state["value"] = "done"
            return True

        def busy():
            if state["code_clicks"] == 0:
                return False
            state["busy_polls"] += 1
            return state["busy_polls"] == 1

        page.goto = goto
        visual.page = page
        visual.deadline = 1000.0
        with (
            patch("xai_browser.time.time", side_effect=lambda: clock["value"]),
            patch.object(visual, "_wait", side_effect=wait),
            patch.object(
                visual,
                "_page_text",
                side_effect=lambda: (
                    "authorization complete" if state["value"] == "done" else ""
                ),
            ),
            patch.object(
                visual,
                "_first_visible",
                side_effect=lambda _selectors: (
                    _CodeInput() if state["value"] == "code" else None
                ),
            ),
            patch.object(visual, "_submit", side_effect=submit),
            patch.object(visual, "_device_action_busy", side_effect=busy),
            patch.object(
                visual,
                "_find_action",
                side_effect=lambda _pattern: (
                    _Action() if state["value"] == "consent" else None
                ),
            ),
            patch.object(visual, "_click_locator_center", side_effect=click_allow),
        ):
            visual.authorize_device(
                "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
                "ABCD-1234",
            )

        self.assertEqual(state["code_clicks"], 1)
        self.assertEqual(state["allow_clicks"], 1)
        self.assertTrue(any("oauth: approval_submitted" in event for event in events))

    def test_grok_batch_snapshot_keeps_browser_mode(self):
        snapshot = _snapshot_reg_config(
            captcha_provider="local",
            yescaptcha_key="local",
            proxy="",
            moemail_api_key="key",
            moemail_base_url="https://mail.example.test",
            prefix="",
            domain="",
            expiry_ms=86400000,
            concurrency=1,
            stagger_ms=0,
            headless=False,
        )

        self.assertFalse(snapshot["headless"])

    def test_xai_uses_hotmail_receiver_with_xai_code_profile(self):
        receiver = object()
        with patch(
            "hotmail_local.create_receiver",
            return_value=("mail@example.com", receiver),
        ) as create_receiver:
            email, actual_receiver = _make_email_receiver(
                mail_provider="hotmail_local",
                hotmail_local_base_url="http://127.0.0.1:17373",
            )

        self.assertEqual(email, "mail@example.com")
        self.assertIs(actual_receiver, receiver)
        create_receiver.assert_called_once_with(
            "http://127.0.0.1:17373",
            verification_target="xai",
        )


class XaiRegistrationConfigTests(unittest.TestCase):
    def test_xai_registration_defaults_to_browser_mode(self):
        self.assertEqual(app.Settings().registration_mode, "browser")
        self.assertEqual(app.DEFAULT_CONFIG["registration_mode"], "browser")
        self.assertTrue(app.Settings().pre_import_probe_enabled)
        self.assertTrue(app.DEFAULT_CONFIG["pre_import_probe_enabled"])

    def test_chatgpt_registration_is_rejected_while_disabled(self):
        settings = app.Settings(
            registration_target="chatgpt",
            mail_provider="yyds",
            count=1,
            concurrency=1,
        )
        with self.assertRaises(app.HTTPException) as caught:
            app.start_register(settings)
        self.assertIn("ChatGPT 注册暂时停用", str(caught.exception.detail))

    def test_start_registration_rejects_hybrid_protocol_request(self):
        settings = app.Settings(
            registration_target="grok",
            registration_mode="protocol",
            mail_provider="yyds",
            count=1,
            concurrency=1,
        )
        with self.assertRaises(app.HTTPException) as caught:
            app.start_register(settings)

        self.assertIn("半协议注册暂时停用", str(caught.exception.detail))

    def test_post_registration_mode_is_normalized_and_chatgpt_stays_browser(self):
        grok = app._post_registration_config(
            {"registration_target": "grok", "registration_mode": "invalid"}
        )
        chatgpt = app._post_registration_config(
            {"registration_target": "chatgpt", "registration_mode": "protocol"}
        )

        self.assertEqual(grok["registration_mode"], "browser")
        self.assertEqual(chatgpt["registration_mode"], "browser")

    def test_post_registration_keeps_selected_xai_json_format(self):
        for output_format in ("cpa", "sub2api"):
            with self.subTest(output_format=output_format):
                config = app._post_registration_config(
                    {
                        "registration_target": "grok",
                        "registration_mode": "protocol",
                        "registration_json_format": output_format,
                    }
                )
                self.assertEqual(config["registration_mode"], "protocol")
                self.assertEqual(config["output_format"], output_format)

    def test_registration_mode_persists_through_config_save_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            with (
                patch.object(app, "CONFIG_FILE", config_path),
                patch.object(app, "apply_environment"),
                patch.object(app, "_sync_solver_proxy_file"),
            ):
                saved = app.save_config({"registration_mode": "protocol"})
                loaded = app.load_config()

        self.assertEqual(saved["registration_mode"], "protocol")
        self.assertEqual(loaded["registration_mode"], "protocol")

    def test_xai_worker_routes_protocol_and_browser_modes_separately(self):
        sid = f"unit-mode-{time.monotonic_ns()}"
        receiver = object()
        try:
            with (
                patch.object(grok_adapter._protocol_worker, "_run_registration") as protocol,
                patch.object(grok_adapter._worker, "_run_registration") as browser,
            ):
                grok_adapter._sessions[sid] = {
                    "_post_registration": {"registration_mode": "protocol"}
                }
                grok_adapter._run_registration(sid, "key", "proxy", receiver)
                protocol.assert_called_once()
                browser.assert_not_called()

                browser.reset_mock()
                protocol.reset_mock()
                grok_adapter._sessions[sid] = {
                    "_post_registration": {"registration_mode": "browser"}
                }
                grok_adapter._run_registration(sid, "key", "proxy", receiver)
                browser.assert_called_once()
                protocol.assert_not_called()
        finally:
            grok_adapter._sessions.pop(sid, None)

    def test_xai_legacy_session_without_mode_keeps_browser_worker(self):
        sid = f"unit-legacy-mode-{time.monotonic_ns()}"
        try:
            grok_adapter._sessions[sid] = {"_post_registration": {}}
            with (
                patch.object(grok_adapter._protocol_worker, "_run_registration") as protocol,
                patch.object(grok_adapter._worker, "_run_registration") as browser,
            ):
                grok_adapter._run_registration(sid, "key", "proxy", object())

            browser.assert_called_once()
            protocol.assert_not_called()
        finally:
            grok_adapter._sessions.pop(sid, None)

    def test_protocol_worker_uses_browser_device_allow_and_pipeline_queue(self):
        source = (
            BACKEND_DIR / "grok_registration" / "protocol_worker.py"
        ).read_text(encoding="utf-8")

        self.assertIn("XaiVisibleRegistration(", source)
        self.assertIn("browser_session.open_authorization(", source)
        self.assertIn("force_full_login=True", source)
        self.assertIn("sso_to_token_with_browser(", source)
        self.assertIn('"refresh_token"', source)
        self.assertIn(".protocol-session.json", source)
        self.assertNotIn("sso_to_direct_token", source)
        self.assertNotIn("xai_oauth_login_protocol(", source)
        self.assertNotIn("register_xai_account", source)
        self.assertLess(
            source.index('ctx._enqueue_pipeline_phase(sid, "probe")'),
            source.index('ctx._enqueue_pipeline_phase(sid, "import")'),
        )

    def test_grok_browser_registration_preserves_visible_mode(self):
        captured = {}

        class _Adapter:
            def start_registration(self, **kwargs):
                captured.update(kwargs)
                return {"ok": True, "id": "gba_test"}

        settings = app.Settings(
            registration_target="grok",
            registration_mode="browser",
            mail_provider="yyds",
            count=1,
            concurrency=1,
            grok_headless=False,
        )
        with (
            patch.object(app, "_get_registration_adapter", return_value=_Adapter()),
            patch.object(app, "apply_environment"),
            patch.object(app, "_sync_solver_proxy_file"),
            patch.object(app, "load_config", return_value=dict(app.DEFAULT_CONFIG)),
            patch.object(app, "save_config"),
        ):
            result = app.start_register(settings)

        self.assertTrue(result["ok"])
        self.assertFalse(captured["headless"])
        self.assertEqual(
            captured["post_registration"]["registration_mode"], "browser"
        )
        self.assertEqual(
            captured["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )

    def test_xai_browser_toggle_is_rendered(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="grok-browser-visible"', html)
        self.assertNotIn(
            'data-registration-mode="browser" title="开启后注册时会弹出浏览器窗口',
            html,
        )
        self.assertNotIn('data-registration-mode="browser"', html)

    def test_hybrid_protocol_mode_is_disabled_in_the_ui(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('<option value="protocol" disabled>半协议注册</option>', html)
        self.assertIn('<option value="browser" selected>浏览器注册</option>', html)
        self.assertIn("out.registration_mode='browser'", script)
        self.assertIn("data={...data,registration_mode:'browser',mail_provider:", script)

    def test_mail_provider_selector_only_exposes_custom_and_hotmail(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('<option value="custom">自定义</option>', html)
        self.assertIn(
            '<option value="hotmail_local">微软邮箱账户池（本地助手）</option>',
            html,
        )
        self.assertNotIn('>YYDS Mail</option>', html)
        self.assertNotIn('>Stalwart 域名邮箱</option>', html)
        self.assertNotIn('<option value="cloudflare_grokfree">', html)
        self.assertNotIn('自定义（Cloudflare 邮箱）', html)
        self.assertIn("function normalizeMailProvider", script)

    def test_chatgpt_download_format_is_disabled(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            '<option value="chatgpt_auth_json" disabled>',
            html,
        )
        with self.assertRaises(app.HTTPException) as caught:
            app.download_accounts(export_format="chatgpt_auth_json")
        self.assertIn("ChatGPT 相关下载暂时停用", str(caught.exception.detail))

    def test_xai_registration_monitor_includes_cpa_pipeline_steps(self):
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("jsonStep.title='转换 CPA JSON'", script)
        self.assertIn("siteStep.title='导入 CPA'", script)
        self.assertIn("{key:'probe',title:'导入前账号测活'}", script)

    def test_pre_import_probe_setting_and_manual_import_list_are_rendered(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('name="pre_import_probe_enabled"', html)
        self.assertIn('id="manual-import-list"', html)
        self.assertIn("function renderManualImportList", script)
        self.assertIn("scrollIntoView({block:'nearest',inline:'nearest'})", script)

    def test_hotmail_registration_count_remains_editable(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("count.readOnly=false", script)
        self.assertIn("注册数量（可用 ${hotmailAvailableCount}）", script)
        self.assertIn('<select name="concurrency">', html)
        self.assertIn('<option value="8">8</option>', html)
        self.assertIn("并发数（最多 8）", script)
        self.assertIn("并发数不能大于注册数量", script)
        self.assertIn("function hotmailVerificationHtml", script)
        self.assertIn("<span>验证码</span>", html)
        self.assertNotIn("count.readOnly=local", script)
        self.assertNotIn(
            "count.value=String(hotmailAvailableCount);if(hotmailAvailableCount>0",
            script,
        )

    def test_successful_pre_import_probe_enqueues_site_import(self):
        sid = f"unit-pre-probe-ok-{time.monotonic_ns()}"
        grok_adapter._sessions[sid] = {
            "id": sid,
            "status": "converted",
            "imported_account_ids": ["account-1"],
            "_post_registration": {
                "auto_import_enabled": True,
                "pre_import_probe_enabled": True,
                "target": "sub2api",
            },
            "auto_import": {"enabled": True, "waiting_for_probe": True},
        }
        try:
            with (
                patch("account_rotation.probe_registration_account", return_value={"ok": True, "available": True, "model": "grok-4.5", "status_code": 200, "reply": "hi"}),
                patch.object(grok_adapter, "wait_pipeline_stagger") as stagger,
                patch.object(grok_adapter, "_enqueue_pipeline_phase") as enqueue,
            ):
                result = grok_adapter._retry_probe_now(sid, manual_retry=False)

            self.assertTrue(result["ok"])
            stagger.assert_not_called()
            enqueue.assert_called_once_with(sid, "import")
            self.assertEqual(grok_adapter._sessions[sid]["status"], "converted")
            self.assertFalse(
                grok_adapter._sessions[sid]["auto_import"]["waiting_for_probe"]
            )
            messages = [
                str(item.get("message") or "")
                for item in grok_adapter._sessions[sid].get("events") or []
            ]
            self.assertTrue(any("测活上游请求" in item for item in messages))
            self.assertTrue(any("测活上游回复" in item and "hi" in item for item in messages))
        finally:
            grok_adapter._sessions.pop(sid, None)

    def test_failed_pre_import_probe_blocks_site_import(self):
        sid = f"unit-pre-probe-fail-{time.monotonic_ns()}"
        grok_adapter._sessions[sid] = {
            "id": sid,
            "status": "converted",
            "imported_account_ids": ["account-1"],
            "_post_registration": {
                "auto_import_enabled": True,
                "pre_import_probe_enabled": True,
                "target": "sub2api",
            },
            "auto_import": {"enabled": True, "waiting_for_probe": True},
        }
        try:
            with (
                patch(
                    "account_rotation.probe_registration_account",
                    return_value={
                        "ok": False,
                        "available": False,
                        "status_code": 401,
                        "error": "invalid token",
                    },
                ),
                patch.object(grok_adapter, "_enqueue_pipeline_phase") as enqueue,
            ):
                result = grok_adapter._retry_probe_now(sid, manual_retry=False)

            self.assertFalse(result["ok"])
            enqueue.assert_not_called()
            session = grok_adapter._sessions[sid]
            self.assertEqual(session["status"], "error")
            self.assertIn("导入前测活失败", session["error"])
            self.assertFalse(session["auto_import"]["waiting_for_probe"])
        finally:
            grok_adapter._sessions.pop(sid, None)

    def test_manual_import_requires_pre_import_probe_success(self):
        sid = f"unit-pre-probe-required-{time.monotonic_ns()}"
        grok_adapter._sessions[sid] = {
            "id": sid,
            "status": "imported",
            "imported_account_ids": ["account-1"],
            "_post_registration": {"pre_import_probe_enabled": True},
        }
        try:
            result = grok_adapter.retry_registration_import(sid)
            self.assertFalse(result["ok"])
            self.assertIn("尚未通过", result["error"])
        finally:
            grok_adapter._sessions.pop(sid, None)

    def test_xai_registration_monitor_tracks_concurrent_sessions_without_stale_refresh(self):
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("function concurrentWorkflowHtml(items)", script)
        self.assertIn("if(refreshInFlight){refreshRequested=true", script)
        self.assertIn("api('/api/sessions',{cache:'no-store'})", script)

    def test_header_duration_uses_latest_registration_round_only(self):
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("function currentRegistrationRound(batches=[],sessions=[])", script)
        self.assertIn("function registrationHeaderStats(batches=[],sessions=[])", script)
        self.assertIn("renderRegistrationHeaderStats(batches,sessions)", script)

    def test_header_success_requires_local_at_rt_credential(self):
        script = (BACKEND_DIR.parent / "web" / "static" / "app.js").read_text(
            encoding="utf-8"
        )

        success_rule = (
            "function reachedRegistrationSuccess(session){return "
            "Number(session.auth_json_count||0)>0||"
            "(session.imported_account_ids||[]).length>0}"
        )
        self.assertIn(success_rule, script)
        self.assertNotIn(
            "Number(session.registration_succeeded_at||0)>0||session.session_file",
            script,
        )

    def test_xai_oauth_consent_uses_browser_callback(self):
        authorized = []
        token = {"access_token": "access", "refresh_token": "refresh"}
        device = {
            "user_code": "ABC123",
            "device_code": "device-token",
            "verification_uri_complete": "https://auth.x.ai/device?code=ABC123",
            "interval": 1,
            "expires_in": 120,
        }

        with (
            patch.object(sso_to_auth_json, "_acquire_device_flow_sequence_slot"),
            patch.object(sso_to_auth_json, "_wait_device_flow_slot"),
            patch.object(sso_to_auth_json, "request_device_code", return_value=device),
            patch.object(
                sso_to_auth_json, "poll_token", return_value=token
            ) as poll_token,
            patch.object(sso_to_auth_json._DEVICE_FLOW_SEQUENCE_SLOTS, "release"),
        ):
            result = sso_to_auth_json.sso_to_token_with_browser(
                "sso-cookie",
                lambda url, code: authorized.append((url, code)),
                quiet=True,
            )

        self.assertEqual(result, token)
        self.assertEqual(
            authorized,
            [("https://auth.x.ai/device?code=ABC123", "ABC123")],
        )
        self.assertFalse(poll_token.call_args.kwargs["immediate"])

    def test_grok_allows_hotmail_local_pool(self):
        captured = {}

        class _Adapter:
            def start_registration(self, **kwargs):
                captured.update(kwargs)
                return {"ok": True, "id": "gba_test"}

        settings = app.Settings(
            registration_target="grok",
            mail_provider="hotmail_local",
            count=1,
            concurrency=1,
        )
        with (
            patch.object(app, "_get_registration_adapter", return_value=_Adapter()),
            patch.object(app, "apply_environment"),
            patch.object(app, "_sync_solver_proxy_file"),
            patch.object(app, "load_config", return_value=dict(app.DEFAULT_CONFIG)),
            patch.object(app, "save_config"),
            patch("hotmail_local.list_accounts", return_value={"available": 2}),
        ):
            result = app.start_register(settings)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["count"], 1)
        self.assertEqual(captured["mail_provider"], "hotmail_local")
        self.assertEqual(
            captured["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )


class XaiImportPipelineTests(unittest.TestCase):
    def test_probe_starts_with_first_ready_account_without_waiting_for_wave(self):
        key = f"unit-probe-{time.monotonic_ns()}"
        started = threading.Event()
        release = threading.Event()
        calls = []

        def run_probe(group, session_ids, limit):
            calls.append((group, list(session_ids), limit))
            started.set()
            release.wait(timeout=2)

        try:
            with patch.object(grok_adapter, "_run_probe_wave", side_effect=run_probe):
                with grok_adapter._pipeline_queue_lock:
                    grok_adapter._pipeline_queues[key] = {
                        "group": "batch-unit",
                        "phase": "probe",
                        "limit": 3,
                        "pending": ["ready-first"],
                        "producer_closed": False,
                        "in_wave": False,
                    }
                worker = threading.Thread(
                    target=grok_adapter._pipeline_dispatch_loop,
                    args=(key,),
                    daemon=True,
                )
                worker.start()

                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(calls, [("batch-unit", ["ready-first"], 3)])

                with grok_adapter._pipeline_queue_lock:
                    grok_adapter._pipeline_queues[key]["producer_closed"] = True
                release.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
        finally:
            release.set()
            with grok_adapter._pipeline_queue_lock:
                grok_adapter._pipeline_queues.pop(key, None)

    def test_import_starts_with_first_ready_account_without_waiting_for_wave(self):
        key = f"unit-import-{time.monotonic_ns()}"
        started = threading.Event()
        release = threading.Event()
        calls = []

        def run_import(group, session_ids, limit):
            calls.append((group, list(session_ids), limit))
            started.set()
            release.wait(timeout=2)

        try:
            with patch.object(grok_adapter, "_run_import_wave", side_effect=run_import):
                with grok_adapter._pipeline_queue_lock:
                    grok_adapter._pipeline_queues[key] = {
                        "group": "batch-unit",
                        "phase": "import",
                        "limit": 3,
                        "pending": ["ready-first"],
                        "producer_closed": False,
                        "in_wave": False,
                    }
                worker = threading.Thread(
                    target=grok_adapter._pipeline_dispatch_loop,
                    args=(key,),
                    daemon=True,
                )
                worker.start()

                self.assertTrue(started.wait(timeout=1))
                self.assertEqual(calls, [("batch-unit", ["ready-first"], 3)])

                with grok_adapter._pipeline_queue_lock:
                    grok_adapter._pipeline_queues[key]["producer_closed"] = True
                release.set()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
        finally:
            release.set()
            with grok_adapter._pipeline_queue_lock:
                grok_adapter._pipeline_queues.pop(key, None)

    def test_ready_import_does_not_wait_for_import_stagger(self):
        sid = f"unit-import-no-stagger-{time.monotonic_ns()}"
        grok_adapter._sessions[sid] = {
            "id": sid,
            "status": "import_queued",
            "imported_account_ids": ["account-1"],
            "_post_registration": {
                "target": "sub2api",
                "pre_import_probe_enabled": False,
                "import_stagger_ms": 60_000,
            },
        }
        try:
            with (
                patch("model_health._load_record", return_value={"id": "account-1"}),
                patch("account_pipeline.import_account", return_value={"ok": True, "status_code": 200}) as import_account,
                patch.object(grok_adapter, "wait_pipeline_stagger") as stagger,
                patch("account_rotation.record_imported_session"),
            ):
                result = grok_adapter._retry_import_now(sid, manual_retry=False)

            self.assertTrue(result["ok"])
            stagger.assert_not_called()
            import_account.assert_called_once()
        finally:
            grok_adapter._sessions.pop(sid, None)


if __name__ == "__main__":
    unittest.main()
