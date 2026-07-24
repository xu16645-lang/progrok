from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app import _manual_import_records, _normalize_import_record_for_registration_target


class ManualImportRecordTests(unittest.TestCase):
    def test_sub2api_openai_agent_identity_keeps_chatgpt_type(self):
        raw_records = _manual_import_records(
            {
                "type": "sub2api-data",
                "accounts": [
                    {
                        "platform": "openai",
                        "type": "agent_identity",
                        "credentials": {
                            "agent_runtime_id": "runtime-1",
                            "agent_private_key": "private-key",
                            "account_id": "account-1",
                            "chatgpt_user_id": "user-1",
                            "email": "user@example.com",
                        },
                    }
                ],
            }
        )

        records = [
            _normalize_import_record_for_registration_target(record, "chatgpt")
            for record in raw_records
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["type"], "chatgpt")
        self.assertEqual(records[0]["auth_kind"], "agent_identity")
        self.assertEqual(records[0]["agent_identity"]["agent_runtime_id"], "runtime-1")

    def test_agent_identity_is_rejected_when_registration_target_is_grok(self):
        with self.assertRaisesRegex(ValueError, "当前注册目标为 Grok"):
            _normalize_import_record_for_registration_target(
                {
                    "agent_runtime_id": "runtime-1",
                    "agent_private_key": "private-key",
                },
                "grok",
            )


if __name__ == "__main__":
    unittest.main()
