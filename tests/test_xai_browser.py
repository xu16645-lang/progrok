import sys
import threading
import time
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
    PASSWORD_SUBMIT_DELAY_MS,
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

    def wait_for_timeout(self, _milliseconds):
        return None

    def set_viewport_size(self, value):
        self.viewport = value

    def evaluate(self, _script):
        return None

    def close(self):
        self.closed = True


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
    def test_generated_profile_is_not_the_old_fixed_placeholder(self):
        with patch("xai_browser.secrets.choice", side_effect=["Aiden", "Carter"]):
            generated = XaiVisibleRegistration._generate_profile()

        self.assertEqual(generated, ("Aiden", "Carter"))
        self.assertNotEqual(generated, ("User", "Grok"))

    def test_profile_is_filled_before_password_and_submit_waits_five_seconds(self):
        events = []

        class _Input:
            def __init__(self, name):
                self.name = name

            def fill(self, value):
                events.append(("fill", self.name, value))

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
            patch.object(visual, "_wait", side_effect=lambda ms: events.append(("wait", ms))),
            patch.object(visual, "_settle_page", side_effect=lambda: events.append(("settle",))),
            patch.object(visual, "_submit", side_effect=lambda *_args: events.append(("submit",))),
        ):
            result = visual.register_account(
                email="person@example.test",
                password="random-password",
                get_verification_code=lambda *_args: None,
            )

        fill_names = [event[1] for event in events if event[0] == "fill"]
        self.assertEqual(fill_names, ["first", "last", "password"])
        password_fill = events.index(("fill", "password", "random-password"))
        wait = events.index(("wait", PASSWORD_SUBMIT_DELAY_MS))
        submit = events.index(("submit",))
        self.assertLess(password_fill, wait)
        self.assertLess(wait, submit)
        self.assertTrue(result["ok"])

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
    def test_grok_browser_registration_preserves_visible_mode(self):
        captured = {}

        class _Adapter:
            def start_registration(self, **kwargs):
                captured.update(kwargs)
                return {"ok": True, "id": "gba_test"}

        settings = app.Settings(
            registration_target="grok",
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
            captured["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )

    def test_xai_browser_toggle_is_rendered(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="grok-browser-visible"', html)

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
            patch.object(sso_to_auth_json, "poll_token", return_value=token),
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
        self.assertEqual(captured["count"], 2)
        self.assertEqual(captured["mail_provider"], "hotmail_local")
        self.assertEqual(
            captured["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )


class XaiImportPipelineTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
