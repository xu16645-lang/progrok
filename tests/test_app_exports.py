from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import app


class _Adapter:
    def __init__(self, sessions: list[dict], batch_id: str = "batch-1"):
        self._sessions = {
            str(item.get("id") or index): dict(item)
            for index, item in enumerate(sessions, start=1)
        }
        self.sessions = [dict(item) for item in sessions]
        self.batch_id = batch_id

    def list_registration_sessions(self):
        return {"sessions": [dict(item) for item in self.sessions], "batches": []}

    def get_registration_batch(self, batch_id):
        if batch_id != self.batch_id:
            return None
        return {"id": batch_id, "sessions": [dict(item) for item in self.sessions]}


class DownloadAccountsTests(unittest.TestCase):
    def _write_json(self, root: Path, relative: str, payload: dict) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_cpa_download_uses_accounts_without_legacy_sso_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root,
                "accounts/xai-user@example.test.json",
                {
                    "type": "xai",
                    "email": "user@example.test",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                },
            )
            adapter = _Adapter(
                [
                    {
                        "id": "session-1",
                        "batch_id": "batch-1",
                        "email": "user@example.test",
                        "status": "imported",
                        "imported_account_ids": ["account-1"],
                    }
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
            ):
                response = app.download_accounts(
                    export_format="cpa_json", batch_id="batch-1"
                )

            payload = json.loads(response.body)
            self.assertEqual(payload["access_token"], "access-token")
            self.assertEqual(payload["refresh_token"], "refresh-token")
            self.assertEqual(response.headers["x-progrok-account-count"], "1")

    def test_sso_download_uses_browser_session_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root,
                "register_sso/session-1.browser-session.json",
                {
                    "email": "user@example.test",
                    "sso": "browser-sso",
                    "source": "xai-browser",
                },
            )
            adapter = _Adapter(
                [
                    {
                        "id": "session-1",
                        "batch_id": "batch-1",
                        "email": "user@example.test",
                        "status": "imported",
                        "imported_account_ids": ["account-1"],
                    }
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
            ):
                response = app.download_accounts(
                    export_format="pure_sso", batch_id="batch-1"
                )

            self.assertEqual(response.body.decode(), "browser-sso\n")

    def test_batch_download_excludes_failed_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root,
                "accounts/xai-failed@example.test.json",
                {
                    "type": "xai",
                    "email": "failed@example.test",
                    "access_token": "access-token",
                    "refresh_token": "refresh-token",
                },
            )
            adapter = _Adapter(
                [
                    {
                        "id": "session-1",
                        "batch_id": "batch-1",
                        "email": "failed@example.test",
                        "status": "error",
                    }
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
            ):
                with self.assertRaises(app.HTTPException) as caught:
                    app.download_accounts(
                        export_format="cpa_json", batch_id="batch-1"
                    )

            self.assertEqual(caught.exception.status_code, 404)

    def test_cpa_download_rejects_credential_without_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root,
                "accounts/xai-user@example.test.json",
                {
                    "type": "xai",
                    "email": "user@example.test",
                    "access_token": "access-only",
                },
            )
            adapter = _Adapter(
                [
                    {
                        "id": "session-1",
                        "email": "user@example.test",
                        "status": "imported",
                        "imported_account_ids": ["account-1"],
                    }
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
            ):
                with self.assertRaises(app.HTTPException) as caught:
                    app.download_accounts(export_format="cpa_json")

            self.assertEqual(caught.exception.status_code, 422)
            self.assertIn("refresh_token", str(caught.exception.detail))

    def test_email_password_download_uses_successful_live_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = _Adapter(
                [
                    {
                        "id": "session-1",
                        "email": "user@example.test",
                        "password": "generated-password",
                        "status": "imported",
                        "imported_account_ids": ["account-1"],
                    }
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
            ):
                response = app.download_accounts(export_format="json")

            self.assertEqual(
                json.loads(response.body),
                [
                    {
                        "email": "user@example.test",
                        "password": "generated-password",
                    }
                ],
            )

    def test_email_password_sso_download_survives_browser_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_json(
                root,
                "register_sso/session-1.browser-session.json",
                {
                    "email": "user@example.test",
                    "password": "persisted-password",
                    "sso": "persisted-sso",
                    "source": "xai-browser",
                },
            )
            adapter = _Adapter(
                [
                    {
                        "id": "session-1",
                        "batch_id": "batch-1",
                        "email": "user@example.test",
                        "status": "imported",
                        "imported_account_ids": ["account-1"],
                    }
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
            ):
                response = app.download_accounts(
                    export_format="email_password_sso", batch_id="batch-1"
                )

            self.assertEqual(
                response.body.decode(),
                "user@example.test:persisted-password:persisted-sso\n",
            )

    def test_history_date_download_only_exports_that_dates_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for email in ("today@example.test", "older@example.test"):
                self._write_json(
                    root,
                    f"accounts/xai-{email}.json",
                    {
                        "type": "xai",
                        "email": email,
                        "access_token": f"access-{email}",
                        "refresh_token": f"refresh-{email}",
                    },
                )
            adapter = _Adapter(
                [
                    {"id": "session-1", "email": "today@example.test", "status": "imported"},
                    {"id": "session-2", "email": "older@example.test", "status": "imported"},
                ]
            )
            with (
                patch.object(app, "DATA_DIR", root),
                patch.object(app, "_get_registration_adapter", return_value=adapter),
                patch(
                    "account_rotation.registration_history_emails",
                    return_value={"today@example.test"},
                ),
            ):
                response = app.download_accounts(
                    export_format="cpa_json", history_date="2026-07-25"
                )

            payload = json.loads(response.body)
            self.assertEqual(payload["email"], "today@example.test")
            self.assertEqual(response.headers["x-progrok-account-count"], "1")


if __name__ == "__main__":
    unittest.main()
