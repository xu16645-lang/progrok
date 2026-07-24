from __future__ import annotations

import base64
import json
import sys
import tempfile
import types
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import chatgpt_build_adapter as adapter
import chatgpt_session_to_auth as session_converter


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    def __init__(self, response: _Response):
        self.response = response
        self.request = None

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return self.response


class ChatGPTProbeTests(unittest.TestCase):
    def test_probe_uses_selected_chatgpt_model_and_account_header(self):
        client = _Client(_Response(200, text=(
            'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
            'data: [DONE]\n'
        )))
        result = adapter.probe_chatgpt_session(
            {
                "accessToken": "access-token",
                "account": {"id": "account-1"},
            },
            "gpt-5.6-sol",
            client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gpt-5.6-sol")
        self.assertEqual(client.request["json"]["model"], "gpt-5.6-sol")
        self.assertEqual(client.request["headers"]["chatgpt-account-id"], "account-1")
        self.assertEqual(client.request["url"], adapter.CHATGPT_PROBE_URL)
        self.assertEqual(result["reply"], "OK")

    def test_agent_identity_probe_uses_local_signed_sub2_style_request(self):
        private_key, _public_key = session_converter._generate_ed25519_keypair()
        client = _Client(_Response(200, text=(
            'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
        )))
        result = adapter.probe_chatgpt_agent_identity(
            {
                "agent_runtime_id": "runtime-1",
                "agent_private_key": private_key,
                "task_id": "task-1",
                "account_id": "account-1",
            },
            "gpt-5.5",
            client=client,
        )

        self.assertTrue(result["ok"])
        self.assertEqual("agent_identity", result["auth_mode"])
        self.assertEqual(adapter.CHATGPT_PROBE_URL, client.request["url"])
        self.assertEqual("hi", client.request["json"]["input"][0]["content"][0]["text"])
        self.assertTrue(client.request["json"]["stream"])
        self.assertFalse(client.request["json"]["store"])
        self.assertEqual("account-1", client.request["headers"]["chatgpt-account-id"])
        authorization = client.request["headers"]["Authorization"]
        self.assertTrue(authorization.startswith("AgentAssertion "))
        encoded = authorization.split(" ", 1)[1]
        envelope = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        self.assertEqual("runtime-1", envelope["agent_runtime_id"])
        self.assertEqual("task-1", envelope["task_id"])
        self.assertTrue(envelope["signature"])

    def test_agent_identity_probe_recovers_invalid_task_locally_once(self):
        private_key, _public_key = session_converter._generate_ed25519_keypair()

        class RecoveryClient:
            def __init__(self):
                self.probe_headers = []
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append(url)
                if url == adapter.CHATGPT_PROBE_URL:
                    self.probe_headers.append(kwargs["headers"])
                    if len(self.probe_headers) == 1:
                        return _Response(
                            401,
                            {"error": {"code": "invalid_task_id"}},
                            '{"error":{"code":"invalid_task_id"}}',
                        )
                    return _Response(200, text=(
                        'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                        'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                    ))
                if url != "https://auth.openai.com/api/accounts/v1/agent/runtime-1/task/register":
                    raise AssertionError(f"unexpected URL: {url}")
                if "Authorization" in kwargs["headers"]:
                    raise AssertionError("task registration must not send Authorization")
                return _Response(200, {"task_id": "new-task"})

        client = RecoveryClient()
        identity = {
            "agent_runtime_id": "runtime-1",
            "agent_private_key": private_key,
            "task_id": "old-task",
            "account_id": "account-1",
        }
        with patch(
            "accounts.persist_chatgpt_agent_task",
            return_value={"ok": True, "persisted": True},
        ) as persist:
            result = adapter.probe_chatgpt_agent_identity(
                identity, "gpt-5.5", client=client
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["task_recovered"])
        self.assertEqual(3, len(client.calls))
        first = client.probe_headers[0]["Authorization"].split(" ", 1)[1]
        second = client.probe_headers[1]["Authorization"].split(" ", 1)[1]
        first_payload = json.loads(base64.urlsafe_b64decode(first + "=" * (-len(first) % 4)))
        second_payload = json.loads(base64.urlsafe_b64decode(second + "=" * (-len(second) % 4)))
        self.assertEqual("old-task", first_payload["task_id"])
        self.assertEqual("new-task", second_payload["task_id"])
        persist.assert_called_once()

    def test_probe_rejects_empty_http_200(self):
        result = adapter.probe_chatgpt_session(
            {"accessToken": "access-token"}, client=_Client(_Response(200))
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "no_reply")

    def test_probe_rejects_stream_without_completed_event(self):
        response = _Response(200, text='data: {"type":"response.output_text.delta","delta":"OK"}\n')
        result = adapter.probe_chatgpt_session({"accessToken": "access-token"}, client=_Client(response))
        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "incomplete_stream")

    def test_probe_rejects_completed_event_without_reply(self):
        response = _Response(200, text='data: {"type":"response.completed","response":{"status":"completed"}}\n')
        result = adapter.probe_chatgpt_session({"accessToken": "access-token"}, client=_Client(response))
        self.assertFalse(result["ok"])
        self.assertEqual(result["classification"], "no_reply")

    def test_probe_rejects_upstream_failed_event(self):
        response = _Response(200, text=(
            'data: {"type":"response.failed","response":{"error":{"message":"account unavailable"}}}\n'
        ))
        result = adapter.probe_chatgpt_session({"accessToken": "access-token"}, client=_Client(response))
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["classification"], "upstream_failed")
        self.assertEqual(result["error"], "account unavailable")

    def test_probe_marks_unauthorized_session_invalid(self):
        result = adapter.probe_chatgpt_session(
            {"accessToken": "expired-token"},
            client=_Client(_Response(401, {"error": "token expired"})),
        )

        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertEqual(result["classification"], "invalid_credential")

    def test_failed_probe_does_not_block_sub2api_import(self):
        sid = "cgpt_test_import_after_probe_failure"
        adapter._sessions[sid] = {
            "id": sid,
            "email": "test@example.com",
            "password": "secret",
            "status": "queued",
            "events": [],
            "proxy": None,
            "_headless": True,
            "_post_registration": {
                "auto_import_enabled": True,
                "target": "sub2api",
                "output_format": "sub2api",
                "step_delay_ms": 0,
            },
        }
        browser = types.SimpleNamespace(
            register_chatgpt_account=lambda **kwargs: {
                "ok": True,
                "session": {"accessToken": "token", "account": {"id": "account-1"}},
            }
        )
        converter = types.SimpleNamespace(
            session_to_auth_entry=lambda *args, **kwargs: {
                "chatgpt-agent::runtime-1": {
                    "agent_identity": {
                        "agent_runtime_id": "runtime-1",
                        "task_id": "task-1",
                        "email": "test@example.com",
                    }
                }
            }
        )
        accounts = types.SimpleNamespace(
            import_auth_payload=lambda *args, **kwargs: {
                "ok": True,
                "imported": [{"id": "local-1", "email": "test@example.com"}],
            }
        )

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch.dict(
                        sys.modules,
                        {
                            "chatgpt_browser": browser,
                            "chatgpt_session_to_auth": converter,
                            "accounts": accounts,
                        },
                    ),
                    patch.object(adapter, "CHATGPT_SESSIONS_DIR", Path(temp_dir) / "chatgpt_sessions"),
                    patch.object(
                        adapter,
                        "_probe_registration_session",
                        return_value={
                            "ok": False,
                            "error": "token unavailable",
                            "probe": {"ok": 0, "fail": 1},
                        },
                    ),
                    patch.object(adapter.time, "sleep", return_value=None),
                    patch("account_pipeline.import_account", return_value={"ok": True, "created": 1}) as importer,
                ):
                    adapter._run_registration(sid, "", object())

                importer.assert_called_once()
            session = adapter._sessions[sid]
            self.assertEqual(session["status"], "imported")
            self.assertTrue(session["auto_import"]["ok"])
            self.assertIn("测活失败已独立记录", session["message"])
        finally:
            adapter._sessions.pop(sid, None)

    def test_revoked_session_marks_agent_identity_registration_as_account_error(self):
        sid = "cgpt_test_agent_identity_token_revoked"
        adapter._sessions[sid] = {
            "id": sid,
            "email": "test@example.com",
            "password": "secret",
            "status": "queued",
            "events": [],
            "proxy": None,
            "_headless": True,
            "_post_registration": {"step_delay_ms": 0},
        }
        browser = types.SimpleNamespace(
            register_chatgpt_account=lambda **kwargs: {
                "ok": True,
                "session": {"accessToken": "token", "account": {"id": "account-1"}},
            }
        )
        upstream_error = session_converter.AgentIdentityUpstreamError(
            "Codex Agent 注册",
            status_code=401,
            message="ChatGPT Session 已被上游撤销，无法继续注册 Agent Identity",
            error_code="token_revoked",
        )

        def fail_conversion(*args, **kwargs):
            raise upstream_error

        converter = types.SimpleNamespace(
            AgentIdentityUpstreamError=session_converter.AgentIdentityUpstreamError,
            session_to_auth_entry=fail_conversion,
        )
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                with (
                    patch.dict(
                        sys.modules,
                        {
                            "chatgpt_browser": browser,
                            "chatgpt_session_to_auth": converter,
                        },
                    ),
                    patch.object(adapter, "CHATGPT_SESSIONS_DIR", Path(temp_dir) / "chatgpt_sessions"),
                    patch.object(adapter.time, "sleep", return_value=None),
                ):
                    adapter._run_registration(sid, "", object())

            session = adapter._sessions[sid]
            self.assertEqual(session["status"], "account_error")
            self.assertEqual(session["error_type"], "session_token_revoked")
            self.assertIn("已被上游撤销", session["message"])
        finally:
            adapter._sessions.pop(sid, None)

    def test_verification_code_prefetch_starts_independently_for_each_worker(self):
        barrier = threading.Barrier(2)

        class Receiver:
            def __init__(self):
                self.started = threading.Event()

            def wait_for_code(self, **kwargs):
                self.started.set()
                barrier.wait(timeout=2)
                return "123456"

        receivers = {
            "one@example.com": Receiver(),
            "two@example.com": Receiver(),
        }

        def fake_register(**kwargs):
            kwargs["on_progress"]("[chatgpt] email: submitted")
            self.assertTrue(receivers[kwargs["email"]].started.wait(timeout=1))
            self.assertEqual(kwargs["get_verification_code"](), "123456")
            return {"ok": False, "error": "test finished after verification prefetch"}

        browser = types.SimpleNamespace(register_chatgpt_account=fake_register)
        session_ids = []
        for index, email in enumerate(receivers, start=1):
            sid = f"cgpt_prefetch_{index}"
            session_ids.append(sid)
            adapter._sessions[sid] = {
                "id": sid,
                "email": email,
                "password": "secret",
                "status": "queued",
                "events": [],
                "_headless": True,
                "_post_registration": {"step_delay_ms": 0},
            }

        try:
            with patch.dict(sys.modules, {"chatgpt_browser": browser}):
                threads = [
                    threading.Thread(
                        target=adapter._run_registration,
                        args=(sid, "", receivers[adapter._sessions[sid]["email"]]),
                    )
                    for sid in session_ids
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
        finally:
            for sid in session_ids:
                adapter._sessions.pop(sid, None)

    def test_verification_code_prefetch_surfaces_mailbox_error(self):
        class Receiver:
            def wait_for_code(self, **kwargs):
                raise RuntimeError("Stalwart JMAP read failed")

        sid = "cgpt_prefetch_mailbox_error"
        adapter._sessions[sid] = {
            "id": sid,
            "email": "mailbox-error@example.com",
            "password": "secret",
            "status": "queued",
            "events": [],
            "_headless": True,
            "_post_registration": {"step_delay_ms": 0},
        }

        def fake_register(**kwargs):
            kwargs["on_progress"]("[chatgpt] email: submitted")
            with self.assertRaisesRegex(RuntimeError, "Stalwart JMAP read failed"):
                kwargs["get_verification_code"]()
            return {"ok": False, "error": "test finished after mailbox error"}

        try:
            browser = types.SimpleNamespace(register_chatgpt_account=fake_register)
            with patch.dict(sys.modules, {"chatgpt_browser": browser}):
                adapter._run_registration(sid, "", Receiver())
        finally:
            adapter._sessions.pop(sid, None)

    def test_verification_code_prefetch_returns_none_when_code_is_not_delivered(self):
        class Receiver:
            def wait_for_code(self, **kwargs):
                raise RuntimeError("timeout waiting for ChatGPT email verification code")

        sid = "cgpt_prefetch_code_not_delivered"
        adapter._sessions[sid] = {
            "id": sid,
            "email": "not-delivered@example.com",
            "password": "secret",
            "status": "queued",
            "events": [],
            "_headless": True,
            "_post_registration": {"step_delay_ms": 0},
        }

        def fake_register(**kwargs):
            kwargs["on_progress"]("[chatgpt] email: submitted")
            self.assertIsNone(kwargs["get_verification_code"]())
            return {"ok": False, "error": "邮箱验证码在 180 秒内未送达"}

        try:
            browser = types.SimpleNamespace(register_chatgpt_account=fake_register)
            with patch.dict(sys.modules, {"chatgpt_browser": browser}):
                adapter._run_registration(sid, "", Receiver())
        finally:
            adapter._sessions.pop(sid, None)


if __name__ == "__main__":
    unittest.main()
