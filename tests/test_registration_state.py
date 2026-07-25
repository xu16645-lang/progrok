from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import registration_state


class RegistrationStateTests(unittest.TestCase):
    def test_concurrent_saves_use_distinct_temporary_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            registration_state, "STATE_DIR", Path(tmp)
        ):
            def save(index: int) -> None:
                registration_state.save_state(
                    "grok",
                    {f"session-{index}": {"id": f"session-{index}", "status": "done"}},
                    {},
                )

            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(save, range(100)))

            state_file = Path(tmp) / "grok.json"
            self.assertTrue(state_file.is_file())
            self.assertFalse(list(Path(tmp).glob("grok.tmp.*")))
            sessions, batches = registration_state.load_state("grok")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(batches, {})

    def test_save_retries_transient_windows_replace_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            registration_state, "STATE_DIR", Path(tmp)
        ), patch.object(
            registration_state, "_REPLACE_RETRY_DELAYS", (0.0, 0.0)
        ):
            real_replace = registration_state.os.replace
            attempts = []

            def replace(source, target):
                attempts.append((source, target))
                if len(attempts) < 3:
                    raise PermissionError("target temporarily locked")
                return real_replace(source, target)

            with patch.object(registration_state.os, "replace", side_effect=replace):
                registration_state.save_state("grok", {}, {})

            self.assertEqual(len(attempts), 3)
            self.assertTrue((Path(tmp) / "grok.json").is_file())

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
