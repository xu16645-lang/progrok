from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
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

    def test_registration_history_groups_by_beijing_registration_date(self) -> None:
        first = self.imported_session("first@example.com")
        first_time = datetime(2026, 7, 24, 16, 30, tzinfo=timezone.utc).timestamp()
        first["created_at"] = first_time - 30
        first["updated_at"] = first_time + 30
        first["events"] = [
            {"at": first_time, "status": "completion", "message": "registration complete"},
            {"at": first_time + 20, "status": "imported", "message": "导入成功"},
        ]
        second = self.imported_session("second@example.com")
        second_time = datetime(2026, 7, 23, 18, 0, tzinfo=timezone.utc).timestamp()
        second["created_at"] = second_time - 30
        second["updated_at"] = second_time + 30
        second["events"] = [
            {"at": second_time, "status": "completion", "message": "registration complete"},
            {"at": second_time + 20, "status": "imported", "message": "导入成功"},
        ]

        account_rotation.record_imported_session("xai", first)
        account_rotation.record_imported_session("chatgpt", second)

        history = account_rotation.list_registration_history()
        self.assertEqual(
            history["items"],
            [
                {"date": "2026-07-25", "count": 1, "xai": 1, "chatgpt": 0},
                {"date": "2026-07-24", "count": 1, "xai": 0, "chatgpt": 1},
            ],
        )
        self.assertEqual(
            account_rotation.registration_history_emails("2026-07-25"),
            {"first@example.com"},
        )
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            account_rotation.registration_history_emails("25/07/2026")

    def test_rotation_state_retries_transient_windows_replace_lock(self) -> None:
        real_replace = account_rotation.os.replace
        attempts = []

        def replace(source, target):
            attempts.append((source, target))
            if len(attempts) < 3:
                raise PermissionError("target temporarily locked")
            return real_replace(source, target)

        with (
            patch.object(account_rotation, "_REPLACE_RETRY_DELAYS", (0.0, 0.0)),
            patch.object(account_rotation.os, "replace", side_effect=replace),
        ):
            account_rotation.record_imported_session(
                "chatgpt", self.imported_session("retry@example.com")
            )

        self.assertEqual(len(attempts), 3)
        self.assertTrue(self.state_path.is_file())
        self.assertFalse(list(self.state_path.parent.glob("account_rotation.tmp.*")))

    def test_imported_session_inherits_pre_import_probe_result(self) -> None:
        session = self.imported_session("probed@example.com")
        session["probe"] = {
            "state": "complete",
            "count": 1,
            "ok": 1,
            "fail": 0,
            "results": [
                {
                    "account_id": "agent-1",
                    "ok": True,
                    "available": True,
                    "status_code": 200,
                    "reply": "upstream pong",
                }
            ],
        }

        record = account_rotation.record_imported_session("chatgpt", session)

        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "normal")
        self.assertEqual(record["request_status"], "正常")
        self.assertEqual(record["last_probe_result"], "upstream pong")
        self.assertEqual(record["probe_state"], "complete")
        self.assertIsNotNone(record["last_probe_at"])

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

        item = account_rotation.list_records(status="normal")["items"][0]
        self.assertEqual(item["status"], "normal")
        self.assertIn("429", item["request_status"])
        self.assertEqual(item["probe_state"], "uncertain")
        self.assertEqual(account_rotation.list_records(status="error")["total"], 0)
        self.assertIsNotNone(item["last_probe_at"])

    def test_imported_xai_uses_local_upstream_probe_even_when_imported_to_sub2api(self) -> None:
        record = {
            "account_type": "xai",
            "site_target": "sub2api",
            "credential_source": "sub2api_login_callback",
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

    def test_registration_probe_reuses_rotation_probe_path(self) -> None:
        with patch.object(
            account_rotation,
            "_probe_record",
            return_value={"ok": True, "available": True},
        ) as probe:
            result = account_rotation.probe_registration_account(
                "xai",
                email="xai@example.com",
                account_id="account-1",
                session_id="session-1",
                session_file="runtime/data/accounts/account-1.json",
                config={"model": "grok-custom"},
            )

        self.assertTrue(result["ok"])
        record, config = probe.call_args.args
        self.assertEqual(record["account_id"], "account-1")
        self.assertEqual(config["probe_model"], "grok-custom")

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
