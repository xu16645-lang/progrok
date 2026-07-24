from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import hotmail_local
from tools import hotmail_helper
import app as app_module


class _HelperHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = {"ok": True, "service": "hotmail-helper", "version": 2}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.last_payload = json.loads(self.rfile.read(length) or b"{}")
        body = {"ok": True, "code": "123456", "nextRefreshToken": "rotated-token"}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class HotmailLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_file = hotmail_local.ACCOUNTS_FILE
        hotmail_local.ACCOUNTS_FILE = Path(self.temp_dir.name) / "accounts.json"
        hotmail_local._reservations.clear()

    def tearDown(self):
        hotmail_local._reservations.clear()
        hotmail_local.ACCOUNTS_FILE = self.old_file
        self.temp_dir.cleanup()

    def test_parse_and_import(self):
        result = hotmail_local.import_accounts(
            "user@outlook.com----password----client-id----refresh-token\ninvalid"
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["invalid"], 1)
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["available"], 1)
        self.assertNotIn("refresh_token", pool["accounts"][0])

    def test_reservation_release_and_mark_used(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        self.assertNotEqual(first["id"], second["id"])
        hotmail_local.release_account(first["id"])
        again = hotmail_local.reserve_account()
        self.assertEqual(again["id"], first["id"])
        hotmail_local.mark_used(first["id"])
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["used"], 1)

    def test_delete_used_accounts_keeps_unused_accounts(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        self.assertTrue(hotmail_local.mark_used(result["account_ids"][0]))

        deleted = hotmail_local.delete_used_accounts()

        self.assertEqual(deleted["deleted"], 1)
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["total"], 1)
        self.assertEqual(pool["used"], 0)
        self.assertEqual(pool["accounts"][0]["email"], "b@outlook.com")

    def test_failed_account_is_quarantined_until_user_restores_it(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        first = hotmail_local.reserve_account()
        self.assertTrue(hotmail_local.mark_failed(first["id"], "验证码超时"))
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 1)
        self.assertEqual(pool["available"], 1)
        second = hotmail_local.reserve_account()
        self.assertNotEqual(second["id"], first["id"])
        hotmail_local.release_account(second["id"])
        self.assertTrue(hotmail_local.set_used(first["id"], False))
        restored = hotmail_local.list_accounts()
        self.assertEqual(restored["failed"], 0)
        self.assertEqual(restored["available"], 2)

    def test_helper_protocol_and_token_rotation(self):
        hotmail_local.import_accounts("a@outlook.com----p----client-a----token-a")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _HelperHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            self.assertTrue(hotmail_local.helper_health(base)["ok"])
            email, receiver = hotmail_local.create_receiver(base)
            self.assertEqual(email, "a@outlook.com")
            self.assertEqual(receiver.wait_for_code(timeout=3), "123456")
            self.assertEqual(server.last_payload["clientId"], "client-a")
            self.assertEqual(server.last_payload["refreshToken"], "token-a")
            self.assertFalse(server.last_payload["allowTimeFallback"])
            self.assertIn("openai", server.last_payload["requiredKeywords"])
            stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
            self.assertEqual(stored[0]["refresh_token"], "rotated-token")
        finally:
            server.shutdown()
            server.server_close()

    def test_xai_receiver_accepts_alphanumeric_code_and_uses_xai_filters(self):
        imported = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        account = hotmail_local.reserve_account()
        receiver = hotmail_local.HotmailLocalReceiver(
            account,
            verification_target="xai",
        )
        payloads = []

        def fake_request(_base, _path, payload, **_kwargs):
            payloads.append(payload)
            return {"ok": True, "code": "A1B-2C3"}

        with patch.object(hotmail_local, "_request_json", side_effect=fake_request):
            self.assertEqual(receiver.wait_for_code(timeout=1), "A1B2C3")

        self.assertTrue(payloads)
        self.assertIn("x.ai", payloads[0]["requiredKeywords"])
        self.assertTrue(payloads[0]["codePatterns"])
        self.assertEqual(account["id"], imported["account_ids"][0])
        hotmail_local.release_account(account["id"])

    def test_registration_code_lookup_never_falls_back_to_old_mail(self):
        messages = [{
            "receivedTimestamp": 1000,
            "subject": "OpenAI verification code 938841",
            "bodyPreview": "Your ChatGPT code is 938841",
            "from": {"emailAddress": {"address": "noreply@openai.com"}},
        }]
        strict = hotmail_helper.select_latest_code(
            messages, [], [], [], 2000, ["openai", "chatgpt", "verification", "code"], [], False
        )
        compatible = hotmail_helper.select_latest_code(
            messages, [], [], [], 2000, ["openai"], [], True
        )
        self.assertEqual(strict["code"], "")
        self.assertEqual(compatible["code"], "938841")
        self.assertTrue(compatible["usedTimeFallback"])

    def test_helper_joins_multi_group_xai_verification_code(self):
        code = hotmail_helper.extract_code(
            "SpaceXAI confirmation code: RBX-7KI",
            code_patterns=[
                {
                    "source": r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b",
                    "flags": "i",
                }
            ],
        )

        self.assertEqual(code, "RBX7KI")

    def test_helper_refresh_token_has_runtime_clock_dependency(self):
        with patch.object(
            hotmail_helper,
            "post_form",
            return_value={"access_token": "access-token"},
        ):
            result = hotmail_helper.refresh_access_token(
                "client-id",
                "refresh-token",
                strategy_names=["live"],
            )

        self.assertEqual(result["access_token"], "access-token")
        self.assertEqual(result["token_endpoint"], "live")

    def test_helper_refresh_token_preserves_http_error_details(self):
        error = HTTPError(
            "https://login.live.com/oauth20_token.srf",
            400,
            "Bad Request",
            hdrs=None,
            fp=BytesIO(b'{"error":"invalid_grant"}'),
        )
        with patch.object(hotmail_helper, "post_form", side_effect=error):
            result = hotmail_helper.try_refresh_access_token(
                hotmail_helper.TOKEN_ENDPOINTS["live"],
                "client-id",
                "refresh-token",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 400)
        self.assertIn("invalid_grant", result["error"])

    def test_helper_refresh_token_preserves_network_error_details(self):
        with patch.object(
            hotmail_helper,
            "post_form",
            side_effect=URLError("connection failed"),
        ):
            result = hotmail_helper.try_refresh_access_token(
                hotmail_helper.TOKEN_ENDPOINTS["live"],
                "client-id",
                "refresh-token",
            )

        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertIn("connection failed", result["error"])

    def test_probe_account_updates_health_and_rotates_token(self):
        result = hotmail_local.import_accounts("a@outlook.com----p----client-a----token-a")
        account_id = result["account_ids"][0]
        with patch.object(
            hotmail_local,
            "_request_json",
            return_value={"ok": True, "transport": "graph", "nextRefreshToken": "token-b"},
        ) as request_json:
            probe = hotmail_local.probe_account(account_id, "http://helper.local")

        self.assertTrue(probe["ok"])
        self.assertEqual(probe["transport"], "graph")
        request_json.assert_called_once()
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["healthy"], 1)
        self.assertEqual(pool["available"], 1)
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["refresh_token"], "token-b")

    def test_failed_probe_quarantines_mailbox_from_available_pool(self):
        result = hotmail_local.import_accounts("a@outlook.com----p----client-a----bad-token")
        account_id = result["account_ids"][0]
        with patch.object(hotmail_local, "_request_json", side_effect=RuntimeError("刷新令牌失效")):
            probe = hotmail_local.probe_account(account_id)

        self.assertFalse(probe["ok"])
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["unhealthy"], 1)
        self.assertEqual(pool["available"], 0)
        self.assertIn("刷新令牌失效", pool["accounts"][0]["mail_health_error"])
        with self.assertRaises(RuntimeError):
            hotmail_local.reserve_account()

    def test_import_endpoint_automatically_probes_imported_accounts(self):
        request = app_module.HotmailImportRequest(
            text=(
                "a@outlook.com----p----client-a----token-a\n"
                "b@outlook.com----p----client-b----token-b"
            ),
            base_url="http://helper.local",
        )
        responses = [
            {"ok": True, "transport": "graph"},
            {"ok": False, "error": "令牌已撤销"},
        ]
        response_lock = threading.Lock()

        def fake_request(*_args, **_kwargs):
            with response_lock:
                return responses.pop(0)

        with patch.object(hotmail_local, "_request_json", side_effect=fake_request):
            result = app_module.hotmail_import(request)

        self.assertEqual(result["probe"]["total"], 2)
        self.assertEqual(result["probe"]["passed"], 1)
        self.assertEqual(result["probe"]["failed"], 1)
        self.assertEqual(result["pool"]["available"], 1)
        self.assertEqual(result["pool"]["healthy"], 1)
        self.assertEqual(result["pool"]["unhealthy"], 1)

    def test_registration_count_uses_available_pool_and_clamps_concurrency(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )

        class Adapter:
            def start_registration(self, **kwargs):
                return {"ok": True, "kwargs": kwargs}

        settings = app_module.Settings(
            registration_target="chatgpt",
            mail_provider="hotmail_local",
            count=99,
            concurrency=6,
            stagger_ms=3000,
            chatgpt_step_delay_ms=3000,
        )
        with (
            patch.object(app_module, "_get_registration_adapter", return_value=Adapter()),
            patch.object(app_module, "load_config", return_value=app_module.DEFAULT_CONFIG.copy()),
            patch.object(app_module, "save_config", side_effect=lambda value: value),
            patch.object(app_module, "apply_environment"),
            patch.object(app_module, "_sync_solver_proxy_file"),
        ):
            result = app_module.start_register(settings)

        self.assertEqual(result["kwargs"]["count"], 2)
        self.assertEqual(result["kwargs"]["concurrency"], 2)
        self.assertEqual(result["kwargs"]["stagger_ms"], 0)
        self.assertEqual(
            result["kwargs"]["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )
        self.assertEqual(result["kwargs"]["post_registration"]["step_delay_ms"], 3000)

    def test_registration_rejects_empty_hotmail_pool(self):
        settings = app_module.Settings(
            registration_target="chatgpt",
            mail_provider="hotmail_local",
            count=0,
            concurrency=0,
        )
        with self.assertRaises(app_module.HTTPException) as caught:
            app_module.start_register(settings)
        self.assertIn("没有可用邮箱", str(caught.exception.detail))

    def test_mail_provider_configs_are_persisted_independently(self):
        old_config_file = app_module.CONFIG_FILE
        app_module.CONFIG_FILE = Path(self.temp_dir.name) / "config.json"
        try:
            with (
                patch.object(app_module, "apply_environment"),
                patch.object(app_module, "_sync_solver_proxy_file"),
            ):
                app_module.save_config({
                    "mail_provider": "custom",
                    "mail_base_url": "https://custom.example/api",
                    "mail_api_key": "custom-key",
                    "mail_domain": "mail.example",
                    "mail_provider_configs": {
                        "yyds": {
                            "mail_base_url": "https://maliapi.215.im",
                            "mail_api_key": "yyds-key",
                            "mail_domain": "",
                        }
                    },
                })
            loaded = app_module.load_config()
            self.assertEqual(loaded["mail_provider_configs"]["yyds"]["mail_api_key"], "yyds-key")
            self.assertEqual(loaded["mail_provider_configs"]["custom"]["mail_api_key"], "custom-key")
            self.assertEqual(loaded["mail_provider_configs"]["custom"]["mail_domain"], "mail.example")
        finally:
            app_module.CONFIG_FILE = old_config_file

    def test_post_registration_probe_model_follows_registration_target(self):
        grok = app_module._post_registration_config({
            "registration_target": "grok",
            "probe_model": "grok-test",
            "chatgpt_probe_model": "gpt-test",
        })
        chatgpt = app_module._post_registration_config({
            "registration_target": "chatgpt",
            "probe_model": "grok-test",
            "chatgpt_probe_model": "gpt-test",
        })

        self.assertEqual(grok["model"], "grok-test")
        self.assertEqual(chatgpt["model"], "gpt-test")
        self.assertEqual(chatgpt["registration_target"], "chatgpt")


if __name__ == "__main__":
    unittest.main()
