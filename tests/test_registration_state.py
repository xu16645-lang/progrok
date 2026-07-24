from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import registration_state


class RegistrationStateTests(unittest.TestCase):
    def test_state_survives_reload_and_strips_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            registration_state, "STATE_DIR", Path(tmp)
        ):
            registration_state.save_state(
                "chatgpt",
                {
                    "cgpt_1": {
                        "id": "cgpt_1",
                        "email": "one@example.com",
                        "status": "running",
                        "password": "secret",
                        "session_data": {"accessToken": "token"},
                        "events": [],
                    }
                },
                {"batch_cgpt_1": {"id": "batch_cgpt_1", "status": "running"}},
            )

            sessions, batches = registration_state.load_state("chatgpt")

            self.assertEqual(sessions["cgpt_1"]["email"], "one@example.com")
            self.assertNotIn("password", sessions["cgpt_1"])
            self.assertNotIn("session_data", sessions["cgpt_1"])
            self.assertEqual(sessions["cgpt_1"]["status"], "error")
            self.assertEqual(sessions["cgpt_1"]["error"], "service_restarted")
            self.assertEqual(batches["batch_cgpt_1"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
