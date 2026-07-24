from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_der_private_key
from nacl.bindings import crypto_sign_ed25519_sk_to_curve25519
from nacl.public import PrivateKey as Curve25519PrivateKey, SealedBox


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import chatgpt_build_adapter as adapter
import chatgpt_session_to_auth as converter
from export_formats import build_chatgpt_auth_payload
from chatgpt_build_adapter import _compact_session


class ChatGPTSessionToAuthTests(unittest.TestCase):
    @staticmethod
    def _response(status_code: int, text: str, content_type: str = "application/json"):
        response = unittest.mock.Mock()
        response.status_code = status_code
        response.text = text
        response.headers = {"content-type": content_type}
        return response

    def test_chatgpt_download_builds_agent_identity_auth_json(self):
        payload = build_chatgpt_auth_payload([{
            "type": "chatgpt",
            "email": "user@example.com",
            "agent_runtime_id": "runtime-1",
            "agent_private_key": "private-key",
            "task_id": "task-1",
            "account_id": "account-1",
            "chatgpt_user_id": "user-1",
        }])

        entry = payload["chatgpt-agent::runtime-1"]
        self.assertEqual(entry["auth_mode"], "agent_identity")
        self.assertEqual(entry["agent_identity"]["account_id"], "account-1")
        self.assertEqual(entry["email"], "user@example.com")

    def test_generated_private_key_is_pkcs8_ed25519(self):
        private_key, public_key = converter._generate_ed25519_keypair()
        parsed = load_der_private_key(base64.b64decode(private_key), password=None)
        self.assertIsInstance(parsed, Ed25519PrivateKey)
        self.assertTrue(public_key.startswith("ssh-ed25519 "))

    def test_register_agent_parses_json_without_curl_response_json(self):
        response = self._response(200, '\ufeff{"agent_runtime_id":"agent-runtime-1"}')
        with patch.object(converter.curl_requests, "post", return_value=response):
            runtime_id = converter._register_agent("token", "ssh-ed25519 public-key")

        self.assertEqual(runtime_id, "agent-runtime-1")

    def test_register_agent_reports_token_revoked_without_json_decoder_error(self):
        response = self._response(
            401,
            '{"error":{"message":"Encountered invalidated oauth token for user, failing request","code":"token_revoked"}}',
            "text/plain",
        )
        with patch.object(converter.curl_requests, "post", return_value=response):
            with self.assertRaises(converter.AgentIdentityUpstreamError) as raised:
                converter._register_agent("token", "ssh-ed25519 public-key")

        self.assertEqual(raised.exception.error_code, "token_revoked")
        self.assertIn("Session 已被上游撤销", str(raised.exception))
        self.assertNotIn("unexpected character", str(raised.exception))

    def test_register_task_reports_non_json_success_response_safely(self):
        private_key, _ = converter._generate_ed25519_keypair()
        response = self._response(200, "upstream gateway error", "text/plain")
        with patch.object(converter.curl_requests, "post", return_value=response):
            with self.assertRaises(converter.AgentIdentityUpstreamError) as raised:
                converter._register_task("token", "runtime-1", private_key)

        self.assertIn("响应不是 JSON", str(raised.exception))
        self.assertIn("upstream gateway error", str(raised.exception))

    def test_session_conversion_registers_real_agent_and_keeps_account_identity(self):
        session = {
            "accessToken": "header.payload.signature",
            "user": {"id": "user-1", "email": "user@example.com"},
            "account": {"id": "account-1", "planType": "free"},
        }
        with (
            patch.object(converter, "_register_agent", return_value="agent-runtime-1") as register_agent,
            patch.object(converter, "_register_task", return_value="task-1") as register_task,
        ):
            result = converter.session_to_auth_entry(
                session,
                proxy="http://127.0.0.1:10808",
                verify_task=True,
            )

        entry = next(iter(result.values()))
        identity = entry["agent_identity"]
        self.assertEqual(identity["agent_runtime_id"], "agent-runtime-1")
        self.assertEqual(identity["task_id"], "task-1")
        self.assertEqual(identity["account_id"], "account-1")
        self.assertEqual(identity["chatgpt_user_id"], "user-1")
        parsed = load_der_private_key(base64.b64decode(identity["agent_private_key"]), password=None)
        self.assertIsInstance(parsed, Ed25519PrivateKey)
        self.assertEqual(register_agent.call_args.args[2], "http://127.0.0.1:10808")
        self.assertEqual(register_task.call_args.args[3], "http://127.0.0.1:10808")

    def test_register_task_decrypts_encrypted_task_id(self):
        private_key_b64, _ = converter._generate_ed25519_keypair()
        parsed = load_der_private_key(base64.b64decode(private_key_b64), password=None)
        seed = parsed.private_bytes_raw()
        signing_key = seed + parsed.public_key().public_bytes_raw()
        curve_private = Curve25519PrivateKey(crypto_sign_ed25519_sk_to_curve25519(signing_key))
        encrypted = SealedBox(curve_private.public_key).encrypt(b"task-decrypted-1")
        response = unittest.mock.Mock()
        response.status_code = 200
        response.text = json.dumps({"encrypted_task_id": base64.b64encode(encrypted).decode("ascii")})
        response.headers = {"content-type": "application/json"}

        with patch.object(converter.curl_requests, "post", return_value=response):
            task_id = converter._register_task("token", "runtime-1", private_key_b64)

        self.assertEqual(task_id, "task-decrypted-1")

    def test_default_conversion_registers_task_for_sub2api(self):
        session = {
            "accessToken": "header.payload.signature",
            "user": {"id": "user-1", "email": "user@example.com"},
            "account": {"id": "account-1", "planType": "free"},
        }
        with (
            patch.object(converter, "_register_agent", return_value="agent-runtime-1"),
            patch.object(converter, "_register_task", return_value="task-1") as register_task,
        ):
            result = converter.session_to_auth_entry(session)

        identity = next(iter(result.values()))["agent_identity"]
        self.assertEqual(identity["task_id"], "task-1")
        register_task.assert_called_once()

    def test_reused_agent_identity_also_registers_task(self):
        session = {
            "accessToken": "header.payload.signature",
            "user": {"id": "user-1", "email": "user@example.com"},
            "account": {"id": "account-1", "planType": "free"},
        }
        with (
            patch.object(converter, "_register_agent") as register_agent,
            patch.object(converter, "_register_task", return_value="task-reused-1") as register_task,
        ):
            result = converter.session_to_auth_entry(
                session,
                agent_runtime_id="agent-runtime-1",
                agent_private_key="private-key",
            )

        identity = next(iter(result.values()))["agent_identity"]
        self.assertEqual(identity["task_id"], "task-reused-1")
        register_agent.assert_not_called()
        register_task.assert_called_once_with(
            "header.payload.signature",
            "agent-runtime-1",
            "private-key",
            None,
        )

    def test_compact_session_does_not_return_sensitive_session_data(self):
        compact = _compact_session({
            "id": "session-1",
            "email": "user@example.com",
            "session_data": {"accessToken": "secret"},
            "session_file": "C:/runtime/data/chatgpt_sessions/session-1.json",
            "auth_json": {"agent_identity": {}},
            "password": "secret",
        })

        self.assertNotIn("session_data", compact)
        self.assertNotIn("auth_json", compact)
        self.assertNotIn("password", compact)
        self.assertIn("session_file", compact)

    def test_original_session_store_round_trip_and_rejects_outside_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_dir = root / "chatgpt_sessions"
            outside = root / "outside.json"
            outside.write_text('{"accessToken":"outside-token"}', encoding="utf-8")
            with patch.object(adapter, "CHATGPT_SESSIONS_DIR", session_dir):
                saved = adapter._save_original_chatgpt_session(
                    {"accessToken": "test-token", "account": {"id": "account-1"}},
                    email="user@example.com",
                    session_id="session-1",
                )
                loaded = adapter._load_original_chatgpt_session(saved)
                recovered = adapter._session_data_for({"session_file": saved})
                rejected = adapter._load_original_chatgpt_session(str(outside))

            self.assertTrue(Path(saved).is_file())
            self.assertEqual(loaded.get("account", {}).get("id"), "account-1")
            self.assertTrue(loaded.get("accessToken"))
            self.assertTrue(recovered.get("accessToken"))
            self.assertEqual(rejected, {})


if __name__ == "__main__":
    unittest.main()
