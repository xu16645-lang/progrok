from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import account_rotation


class AccountRotationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "account_rotation.json"
        self.patches = [
            patch.object(account_rotation, "STATE_PATH", self.state_path),
            patch.object(account_rotation, "_loaded", False),
            patch.object(account_rotation, "_records", {}),
            patch.object(account_rotation, "_meta", {}),
            patch.object(account_rotation, "_poll_running", False),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    @staticmethod
    def imported_session(email: str = "one@example.com") -> dict[str, object]:
        now = time.time()
        return {
            "id": "cgpt_demo",
            "email": email,
            "created_at": now - 30,
            "updated_at": now,
            "session_file": "runtime/data/chatgpt_sessions/demo.json",
            "imported_accounts": [{"id": "agent-1", "email": email}],
            "auto_import": {"enabled": True, "ok": True, "target": "sub2api"},
            "events": [
                {"at": now - 20, "status": "completion", "message": "registration complete"},
                {"at": now - 5, "status": "imported", "message": "导入成功"},
            ],
            "password": "must-not-be-persisted",
            "session_data": {"accessToken": "must-not-be-persisted"},
        }

    def test_imported_session_is_persistent_and_public_fields_are_safe(self) -> None:
        record = account_rotation.record_imported_session(
            "chatgpt", self.imported_session()
        )

        self.assertIsNotNone(record)
        page = account_rotation.list_records(page=1, page_size=20)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["email"], "one@example.com")
        self.assertEqual(page["items"][0]["account_type"], "chatgpt")
        self.assertTrue(page["items"][0]["imported_to_site"])
        self.assertNotIn("password", page["items"][0])
        self.assertNotIn("session_data", page["items"][0])
        self.assertNotIn("source_session_file", page["items"][0])
        self.assertTrue(self.state_path.exists())

    def test_filter_and_probe_result_update(self) -> None:
        record = account_rotation.record_imported_session(
            "chatgpt", self.imported_session("match@example.com")
        )
        self.assertIsNotNone(record)
        filtered = account_rotation.list_records(keyword="match", page=1, page_size=20)
        self.assertEqual(filtered["total"], 1)

        with patch.object(account_rotation, "_probe_record", return_value={"ok": False, "status_code": 429, "error": "rate limited"}):
            result = account_rotation.schedule_probe([record["id"]], {"probe_concurrency": 1})
            self.assertEqual(result["scheduled"], 1)
            deadline = time.time() + 2
            while account_rotation.list_records()["poll"]["running"] and time.time() < deadline:
                time.sleep(0.02)

        item = account_rotation.list_records(status="error")["items"][0]
        self.assertEqual(item["status"], "error")
        self.assertIn("429", item["request_status"])
        self.assertIsNotNone(item["last_probe_at"])

    def test_imported_xai_uses_local_upstream_probe_even_when_imported_to_sub2api(self) -> None:
        record = {
            "account_type": "xai",
            "site_target": "sub2api",
            "email": "xai@example.com",
        }
        config = {
            "probe_model": "grok-4.5",
            "sub2api_base_url": "https://sub2.example.test",
            "sub2api_api_key": "admin-key",
            "sub2api_auth_mode": "api_key",
        }
        local_record = {"access_token": "local-token"}
        with (
            patch.object(account_rotation, "_load_xai_record", return_value=local_record) as loader,
            patch(
                "account_pipeline.probe_account",
                return_value={"ok": True, "available": True, "probe_via": "local_upstream"},
            ) as probe,
        ):
            result = account_rotation._probe_record(record, config)

        self.assertTrue(result["ok"])
        self.assertEqual("local_upstream", result["probe_via"])
        loader.assert_called_once_with(record)
        probe.assert_called_once_with(local_record, "grok-4.5")

    def test_imported_chatgpt_uses_local_agent_identity_probe(self) -> None:
        record = {
            "account_type": "chatgpt",
            "site_target": "sub2api",
            "account_id": "runtime-1",
            "email": "chatgpt@example.com",
        }
        config = {
            "chatgpt_probe_model": "gpt-5.5",
            "sub2api_base_url": "https://sub2.example.test",
            "sub2api_api_key": "admin-key",
        }
        identity = {"agent_runtime_id": "runtime-1", "task_id": "task-1"}
        with (
            patch.object(account_rotation, "_load_chatgpt_identity", return_value=identity) as loader,
            patch(
                "chatgpt_build_adapter.probe_chatgpt_agent_identity",
                return_value={"ok": True, "available": True, "probe_via": "local_agent_identity"},
            ) as probe,
        ):
            result = account_rotation._probe_record(record, config)

        self.assertTrue(result["ok"])
        self.assertEqual("local_agent_identity", result["probe_via"])
        loader.assert_called_once_with(record)
        probe.assert_called_once_with(identity, "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
