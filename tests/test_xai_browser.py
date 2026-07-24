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
from grok_build_adapter import _make_email_receiver, _snapshot_reg_config
from xai_browser import XAI_SIGNUP_URL, XaiBrowserRuntime, XaiVisibleRegistration


class _Page:
    def __init__(self):
        self.gotos = []
        self.viewport = None
        self.closed = False

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
    def test_grok_protocol_registration_forces_headless(self):
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
        self.assertTrue(captured["headless"])
        self.assertEqual(
            captured["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )

    def test_xai_protocol_browser_toggle_is_not_rendered(self):
        html = (BACKEND_DIR.parent / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('id="grok-browser-visible"', html)

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
