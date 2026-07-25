from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
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
        hotmail_local._verification_states.clear()

    def tearDown(self):
        hotmail_local._reservations.clear()
        hotmail_local._verification_states.clear()
        hotmail_local.ACCOUNTS_FILE = self.old_file
        self.temp_dir.cleanup()

    def test_parse_and_import(self):
        result = hotmail_local.import_accounts(
            "user@outlook.com----password----client-id----refresh-token\ninvalid"
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["invalid"], 1)
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["available"], 10)
        self.assertEqual(pool["available_accounts"], 1)
        self.assertNotIn("refresh_token", pool["accounts"][0])

    def test_reservation_release_and_mark_used(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        # Prefer different physical mailboxes under concurrency.
        self.assertNotEqual(first["id"], second["id"])
        hotmail_local.release_account(first["id"], alias_index=first["alias_index"])
        again = hotmail_local.reserve_account()
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["alias_index"], first["alias_index"])
        hotmail_local.mark_used(first["id"], alias_index=again["alias_index"])
        hotmail_local.mark_used(second["id"], alias_index=second["alias_index"])
        pool = hotmail_local.list_accounts()
        # One slot each on a and b.
        self.assertEqual(pool["used"], 0)
        self.assertEqual(pool["available"], 18)

    def test_plus_alias_reuses_mailbox_ten_times(self):
        hotmail_local.import_accounts(
            "ostaraaugustine7332@hotmail.com----p----client-a----token-a"
        )
        emails = []
        for expected_index in range(10):
            reserved = hotmail_local.reserve_account()
            emails.append(reserved["registration_email"])
            self.assertEqual(reserved["alias_index"], expected_index)
            if expected_index == 0:
                self.assertEqual(
                    reserved["registration_email"],
                    "ostaraaugustine7332@hotmail.com",
                )
            else:
                self.assertEqual(
                    reserved["registration_email"],
                    f"ostaraaugustine7332+{expected_index}@hotmail.com",
                )
            self.assertTrue(
                hotmail_local.mark_used(
                    reserved["id"], alias_index=reserved["alias_index"]
                )
            )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["available"], 0)
        self.assertEqual(pool["used"], 1)
        self.assertEqual(
            emails,
            [
                "ostaraaugustine7332@hotmail.com",
                "ostaraaugustine7332+1@hotmail.com",
                "ostaraaugustine7332+2@hotmail.com",
                "ostaraaugustine7332+3@hotmail.com",
                "ostaraaugustine7332+4@hotmail.com",
                "ostaraaugustine7332+5@hotmail.com",
                "ostaraaugustine7332+6@hotmail.com",
                "ostaraaugustine7332+7@hotmail.com",
                "ostaraaugustine7332+8@hotmail.com",
                "ostaraaugustine7332+9@hotmail.com",
            ],
        )
        with self.assertRaises(RuntimeError):
            hotmail_local.reserve_account()

    def test_concurrent_reservations_prefer_different_mailboxes(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        # Spread across physical mailboxes before reusing plus-aliases.
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["alias_index"], 0)
        self.assertEqual(second["alias_index"], 0)
        third = hotmail_local.reserve_account()
        self.assertIn(third["id"], {first["id"], second["id"]})
        self.assertEqual(third["alias_index"], 1)
        hotmail_local.release_account(first["id"], alias_index=first["alias_index"])
        hotmail_local.release_account(second["id"], alias_index=second["alias_index"])
        hotmail_local.release_account(third["id"], alias_index=third["alias_index"])

    def test_same_mailbox_aliases_only_after_all_idle_mailboxes_are_busy(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        self.assertEqual(first["id"], second["id"])
        self.assertNotEqual(first["registration_email"], second["registration_email"])
        self.assertEqual(first["alias_index"], 0)
        self.assertEqual(second["alias_index"], 1)
        hotmail_local.mark_used(first["id"], alias_index=0)
        hotmail_local.mark_used(second["id"], alias_index=1)
        third = hotmail_local.reserve_account()
        self.assertEqual(third["registration_email"], "a+2@outlook.com")
        hotmail_local.release_account(third["id"], alias_index=2)

    def test_alias_failure_does_not_freeze_whole_mailbox(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        self.assertEqual(first["registration_email"], "a@outlook.com")
        self.assertTrue(
            hotmail_local.mark_failed(first["id"], "页面超时", alias_index=0)
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 0)
        self.assertEqual(pool["available"], 9)
        again = hotmail_local.reserve_account()
        # Index 0 failed, so next free alias is +1.
        self.assertEqual(again["registration_email"], "a+1@outlook.com")
        hotmail_local.release_account(again["id"], alias_index=1)

    def test_account_level_failure_freezes_mailbox(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        first = hotmail_local.reserve_account()
        self.assertTrue(
            hotmail_local.mark_failed(
                first["id"], "refresh token invalid_grant", alias_index=0
            )
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 1)
        second = hotmail_local.reserve_account()
        self.assertNotEqual(second["id"], first["id"])
        hotmail_local.release_account(second["id"], alias_index=second["alias_index"])

    def test_account_level_failure_after_alias_failure_still_freezes(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        self.assertTrue(
            hotmail_local.mark_failed(first["id"], "页面超时", alias_index=0)
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 0)
        self.assertEqual(pool["available"], 9)
        second = hotmail_local.reserve_account()
        self.assertTrue(
            hotmail_local.mark_failed(
                second["id"], "invalid_grant", alias_index=second["alias_index"]
            )
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 1)
        self.assertEqual(pool["available"], 0)
        with self.assertRaises(RuntimeError):
            hotmail_local.reserve_account()

    def test_page_login_errors_do_not_freeze_mailbox(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        self.assertTrue(
            hotmail_local.mark_failed(
                first["id"], "登录按钮点击后页面未跳转", alias_index=0
            )
        )
        self.assertTrue(
            hotmail_local.mark_failed(
                first["id"], "OAuth 授权页面超时", alias_index=1
            )
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 0)
        self.assertEqual(pool["available"], 8)
        again = hotmail_local.reserve_account()
        self.assertEqual(again["registration_email"], "a+2@outlook.com")
        hotmail_local.release_account(again["id"], alias_index=2)

    def test_import_dedupes_by_base_mailbox(self):
        result = hotmail_local.import_accounts(
            "base@hotmail.com----p----client-a----token-a\n"
            "base+1@hotmail.com----p----client-b----token-b"
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["updated"], 1)
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["total"], 1)
        self.assertEqual(pool["accounts"][0]["email"], "base@hotmail.com")
        self.assertEqual(pool["available"], 10)

    def test_load_merges_historical_duplicate_base_rows(self):
        hotmail_local.ACCOUNTS_FILE.write_text(
            json.dumps(
                [
                    {
                        "id": "hm_base",
                        "email": "base@hotmail.com",
                        "password": "p",
                        "client_id": "client-a",
                        "refresh_token": "token-a",
                        "used_aliases": [0],
                        "failed_aliases": [],
                        "failed": False,
                        "use_count": 1,
                    },
                    {
                        "id": "hm_alias",
                        "email": "base+1@hotmail.com",
                        "password": "p2",
                        "client_id": "client-b",
                        "refresh_token": "token-b",
                        # Row-local 0/1 both map to base+1 (slot 1); local 2 maps to slot 2.
                        "used_aliases": [0],
                        "failed_aliases": [2],
                        "failed": False,
                        "use_count": 1,
                    },
                ],
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["total"], 1)
        self.assertEqual(pool["accounts"][0]["email"], "base@hotmail.com")
        # base used 0, base+1 local 0->1 used, local 2->2 failed => 7 free slots
        self.assertEqual(pool["available"], 7)
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["used_aliases"], [0, 1])
        self.assertEqual(stored[0]["failed_aliases"], [2])
        self.assertEqual(stored[0]["refresh_token"], "token-b")

    def test_merge_shifts_plus_identity_local_aliases(self):
        hotmail_local.ACCOUNTS_FILE.write_text(
            json.dumps(
                [
                    {
                        "id": "hm_base",
                        "email": "base@hotmail.com",
                        "password": "p",
                        "client_id": "client-a",
                        "refresh_token": "token-a",
                        "used_aliases": [0],
                        "failed_aliases": [],
                        "failed": False,
                    },
                    {
                        "id": "hm_alias",
                        "email": "base+1@hotmail.com",
                        "password": "p2",
                        "client_id": "client-b",
                        "refresh_token": "token-b",
                        "used_aliases": [0],
                        "failed_aliases": [],
                        "failed": False,
                    },
                ],
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["available"], 8)
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["used_aliases"], [0, 1])
        next_reserved = hotmail_local.reserve_account()
        self.assertEqual(next_reserved["registration_email"], "base+2@hotmail.com")
        hotmail_local.release_account(
            next_reserved["id"], alias_index=next_reserved["alias_index"]
        )

    def test_merge_maps_base_plus_local_indices_like_build_plus_alias(self):
        # build_plus_alias("base+1@", 0) -> base+1  (slot 1)
        # build_plus_alias("base+1@", 1) -> base+1  (slot 1, same address)
        # build_plus_alias("base+1@", 2) -> base+2  (slot 2)
        hotmail_local.ACCOUNTS_FILE.write_text(
            json.dumps(
                [
                    {
                        "id": "hm_base",
                        "email": "base@hotmail.com",
                        "password": "p",
                        "client_id": "client-a",
                        "refresh_token": "token-a",
                        "used_aliases": [],
                        "failed_aliases": [],
                        "failed": False,
                    },
                    {
                        "id": "hm_alias",
                        "email": "base+1@hotmail.com",
                        "password": "p2",
                        "client_id": "client-b",
                        "refresh_token": "token-b",
                        "used_aliases": [0, 1, 2],
                        "failed_aliases": [],
                        "failed": False,
                    },
                ],
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        pool = hotmail_local.list_accounts()
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["used_aliases"], [1, 2])
        # slots 0,3..9 free => 8 available
        self.assertEqual(pool["available"], 8)
        first = hotmail_local.reserve_account()
        self.assertEqual(first["registration_email"], "base@hotmail.com")
        hotmail_local.release_account(first["id"], alias_index=0)
        hotmail_local.mark_used(first["id"], alias_index=0)
        second = hotmail_local.reserve_account()
        self.assertEqual(second["registration_email"], "base+3@hotmail.com")
        hotmail_local.release_account(second["id"], alias_index=3)

    def test_reimport_clears_account_failed_quarantine(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-old"
        )
        first = hotmail_local.reserve_account()
        self.assertTrue(
            hotmail_local.mark_failed(
                first["id"], "invalid_grant", alias_index=first["alias_index"]
            )
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 1)
        self.assertEqual(pool["available"], 0)

        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-new"
        )
        restored = hotmail_local.list_accounts()
        self.assertEqual(restored["failed"], 0)
        self.assertEqual(restored["available"], 10)
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["refresh_token"], "token-new")
        self.assertFalse(stored[0].get("account_failed"))

    def test_successful_probe_clears_account_failed(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        account_id = result["account_ids"][0]
        hotmail_local.mark_failed(account_id, "invalid_grant")
        self.assertEqual(hotmail_local.list_accounts()["failed"], 1)
        with patch.object(
            hotmail_local,
            "_request_json",
            return_value={"ok": True, "transport": "graph", "nextRefreshToken": "token-b"},
        ):
            probe = hotmail_local.probe_account(account_id)
        self.assertTrue(probe["ok"])
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 0)
        self.assertEqual(pool["available"], 10)
        self.assertEqual(pool["healthy"], 1)

    def test_concurrent_token_rotation_uses_latest_token(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-0"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        receiver_a = hotmail_local.HotmailLocalReceiver(first)
        receiver_b = hotmail_local.HotmailLocalReceiver(second)
        seen_tokens: list[str] = []
        counter = {"n": 0}
        counter_lock = threading.Lock()
        errors: list[BaseException] = []

        def fake_request(_base, _path, payload, **_kwargs):
            token = str(payload.get("refreshToken") or "")
            with counter_lock:
                seen_tokens.append(token)
                counter["n"] += 1
                next_token = f"token-{counter['n']}"
            # Simulate network latency so a missing lock would interleave refreshes.
            time.sleep(0.05)
            return {"ok": True, "code": "123456", "nextRefreshToken": next_token}

        def run_wait(receiver):
            try:
                receiver.wait_for_code(timeout=5)
            except BaseException as exc:  # noqa: BLE001 - collect for assertion
                errors.append(exc)

        with patch.object(hotmail_local, "_request_json", side_effect=fake_request):
            threads = [
                threading.Thread(target=run_wait, args=(receiver_a,)),
                threading.Thread(target=run_wait, args=(receiver_b,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        # Serialized: second request must observe the first rotation, not the stale copy.
        self.assertEqual(seen_tokens, ["token-0", "token-1"])
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["refresh_token"], "token-2")
        self.assertEqual(
            {receiver_a.refresh_token, receiver_b.refresh_token},
            {"token-1", "token-2"},
        )

    def test_mark_used_ignores_out_of_range_alias_index(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        hotmail_local.release_account(first["id"], alias_index=first["alias_index"])
        self.assertTrue(hotmail_local.mark_used(first["id"], alias_index=99))
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["available"], 10)
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["used_aliases"], [])

    def test_receiver_uses_base_mailbox_and_recipient_filter(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        reserved = hotmail_local.reserve_account()
        # Force the second slot.
        hotmail_local.release_account(reserved["id"], alias_index=0)
        hotmail_local.mark_used(reserved["id"], alias_index=0)
        reserved = hotmail_local.reserve_account()
        receiver = hotmail_local.HotmailLocalReceiver(reserved)
        self.assertEqual(receiver.email, "a+1@outlook.com")
        self.assertEqual(receiver.base_email, "a@outlook.com")
        payloads = []

        def fake_request(_base, _path, payload, **_kwargs):
            payloads.append(payload)
            return {"ok": True, "code": "123456"}

        with patch.object(hotmail_local, "_request_json", side_effect=fake_request):
            self.assertEqual(receiver.wait_for_code(timeout=1), "123456")
        self.assertEqual(payloads[0]["email"], "a@outlook.com")
        self.assertEqual(payloads[0]["recipientFilters"], ["a+1@outlook.com"])
        self.assertEqual(payloads[0]["top"], 30)
        hotmail_local.mark_used(reserved["id"], alias_index=1)

    def test_receiver_exposes_live_verification_code_state(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        reserved = hotmail_local.reserve_account()
        receiver = hotmail_local.HotmailLocalReceiver(
            reserved, verification_target="xai"
        )

        receiver.mark_code_request_started()
        waiting = hotmail_local.list_accounts()["accounts"][0][
            "verification_entries"
        ][0]
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["email"], "a@outlook.com")
        self.assertEqual(waiting["code"], "")

        with patch.object(
            hotmail_local,
            "_request_json",
            return_value={"ok": True, "code": "ABC-123"},
        ):
            self.assertEqual(receiver.wait_for_code(timeout=1), "ABC123")

        received = hotmail_local.list_accounts()["accounts"][0][
            "verification_entries"
        ][0]
        self.assertEqual(received["status"], "received")
        self.assertEqual(received["code"], "ABC123")

    def test_wait_for_code_can_cancel_while_blocked_on_token_lock(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        holder = hotmail_local.HotmailLocalReceiver(first)
        waiter = hotmail_local.HotmailLocalReceiver(second)
        token_lock = hotmail_local._account_token_lock(first["id"])
        self.assertTrue(token_lock.acquire(blocking=False))
        cancelled = {"value": False}
        errors: list[BaseException] = []

        def run_wait():
            try:
                waiter.wait_for_code(
                    timeout=3,
                    should_cancel=lambda: cancelled["value"],
                    poll_interval=1.0,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run_wait)
        thread.start()
        time.sleep(0.35)
        cancelled["value"] = True
        thread.join(timeout=2)
        token_lock.release()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", str(errors[0]).lower())

    def test_wait_for_code_respects_deadline_while_blocked_on_token_lock(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        first = hotmail_local.reserve_account()
        second = hotmail_local.reserve_account()
        waiter = hotmail_local.HotmailLocalReceiver(second)
        token_lock = hotmail_local._account_token_lock(first["id"])
        self.assertTrue(token_lock.acquire(blocking=False))
        errors: list[BaseException] = []
        started = time.time()

        def run_wait():
            try:
                waiter.wait_for_code(timeout=0.6, poll_interval=1.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run_wait)
        thread.start()
        thread.join(timeout=3)
        token_lock.release()
        elapsed = time.time() - started
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn("超时", str(errors[0]))
        self.assertLess(elapsed, 2.0)

    def test_delete_account_drops_token_lock(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        account_id = result["account_ids"][0]
        hotmail_local._account_token_lock(account_id)
        self.assertIn(account_id, hotmail_local._token_locks)
        self.assertTrue(hotmail_local.delete_account(account_id))
        self.assertNotIn(account_id, hotmail_local._token_locks)

    def test_select_latest_code_filters_by_recipient_alias(self):
        messages = [
            {
                "receivedTimestamp": 3000,
                "subject": "xAI verification code AAA-111",
                "bodyPreview": "Your code is AAA-111",
                "from": {"emailAddress": {"address": "noreply@x.ai"}},
                "recipients": ["a+2@outlook.com"],
            },
            {
                "receivedTimestamp": 2000,
                "subject": "xAI verification code BBB-222",
                "bodyPreview": "Your code is BBB-222",
                "from": {"emailAddress": {"address": "noreply@x.ai"}},
                "recipients": ["a+1@outlook.com"],
            },
        ]
        selected = hotmail_helper.select_latest_code(
            messages,
            ["x.ai"],
            ["verification"],
            [],
            0,
            ["x.ai", "code"],
            [
                {"source": r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b", "flags": "i"},
            ],
            False,
            ["a+1@outlook.com"],
        )
        self.assertEqual(selected["code"], "BBB222")

    def test_select_latest_code_rejects_missing_recipients_when_filtered(self):
        messages = [
            {
                "receivedTimestamp": 3000,
                "subject": "OpenAI verification code 123456",
                "bodyPreview": "Your ChatGPT code is 123456",
                "from": {"emailAddress": {"address": "noreply@openai.com"}},
            },
            {
                "receivedTimestamp": 2000,
                "subject": "OpenAI verification code 654321",
                "bodyPreview": "Your ChatGPT code is 654321",
                "from": {"emailAddress": {"address": "noreply@openai.com"}},
                "recipients": ["a+1@outlook.com"],
            },
        ]
        selected = hotmail_helper.select_latest_code(
            messages,
            [],
            [],
            [],
            0,
            ["openai", "chatgpt", "verification", "code"],
            [],
            False,
            ["a+1@outlook.com"],
        )
        self.assertEqual(selected["code"], "654321")

        missing_only = hotmail_helper.select_latest_code(
            [messages[0]],
            [],
            [],
            [],
            0,
            ["openai", "chatgpt", "verification", "code"],
            [],
            False,
            ["a+1@outlook.com"],
        )
        self.assertEqual(missing_only["code"], "")

    def test_delete_used_accounts_keeps_unused_accounts(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        # Fully exhaust the first mailbox before deleting "used" accounts.
        self.assertTrue(hotmail_local.set_used(result["account_ids"][0], True))

        deleted = hotmail_local.delete_used_accounts()

        self.assertEqual(deleted["deleted"], 1)
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["total"], 1)
        self.assertEqual(pool["used"], 0)
        self.assertEqual(pool["accounts"][0]["email"], "b@outlook.com")

    def test_all_alias_failed_does_not_become_all_success_on_reload(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        account_id = result["account_ids"][0]
        for index in range(10):
            self.assertTrue(
                hotmail_local.mark_failed(account_id, f"页面超时 {index}", alias_index=index)
            )
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(stored[0]["used_aliases"], [])
        self.assertEqual(stored[0]["failed_aliases"], list(range(10)))
        self.assertTrue(stored[0]["used"])

        # Reload path must not reinterpret used=true + empty used_aliases as all success.
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["used"], 1)
        self.assertEqual(pool["available"], 0)
        reloaded = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(reloaded[0]["used_aliases"], [])
        self.assertEqual(reloaded[0]["failed_aliases"], list(range(10)))
        self.assertEqual(
            hotmail_local._normalize_used_aliases(reloaded[0]),
            set(),
        )
        self.assertEqual(
            hotmail_local._normalize_failed_aliases(reloaded[0]),
            set(range(10)),
        )

    def test_delete_used_accounts_removes_all_alias_failed_mailboxes(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        target_id = result["account_ids"][0]
        for index in range(10):
            self.assertTrue(
                hotmail_local.mark_failed(target_id, f"页面超时 {index}", alias_index=index)
            )
        pool = hotmail_local.list_accounts()
        a_row = next(item for item in pool["accounts"] if item["email"] == "a@outlook.com")
        self.assertTrue(a_row["used"])
        self.assertEqual(pool["used"], 1)
        stored = json.loads(hotmail_local.ACCOUNTS_FILE.read_text(encoding="utf-8"))
        a_stored = next(item for item in stored if item["email"] == "a@outlook.com")
        self.assertTrue(a_stored["used"])

        deleted = hotmail_local.delete_used_accounts()
        self.assertEqual(deleted["deleted"], 1)
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["total"], 1)
        self.assertEqual(pool["accounts"][0]["email"], "b@outlook.com")

    def test_delete_account_rejects_reserved_mailbox(self):
        result = hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a"
        )
        account_id = result["account_ids"][0]
        reserved = hotmail_local.reserve_account()
        self.assertEqual(reserved["id"], account_id)
        with self.assertRaises(RuntimeError) as caught:
            hotmail_local.delete_account(account_id)
        self.assertIn("注册任务", str(caught.exception))
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["total"], 1)
        hotmail_local.release_account(account_id, alias_index=reserved["alias_index"])
        self.assertTrue(hotmail_local.delete_account(account_id))
        self.assertEqual(hotmail_local.list_accounts()["total"], 0)

    def test_failed_account_is_quarantined_until_user_restores_it(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )
        first = hotmail_local.reserve_account()
        # Account-level auth failure freezes the whole mailbox.
        self.assertTrue(
            hotmail_local.mark_failed(
                first["id"], "refresh token invalid_grant", alias_index=first["alias_index"]
            )
        )
        pool = hotmail_local.list_accounts()
        self.assertEqual(pool["failed"], 1)
        self.assertEqual(pool["available"], 10)
        second = hotmail_local.reserve_account()
        self.assertNotEqual(second["id"], first["id"])
        hotmail_local.release_account(second["id"], alias_index=second["alias_index"])
        self.assertTrue(hotmail_local.set_used(first["id"], False))
        restored = hotmail_local.list_accounts()
        self.assertEqual(restored["failed"], 0)
        self.assertEqual(restored["available"], 20)

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
        self.assertEqual(pool["available"], 10)
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
        self.assertEqual(pool["failed"], 0)
        self.assertEqual(pool["used"], 0)
        self.assertFalse(pool["accounts"][0]["failed"])
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
        self.assertEqual(result["pool"]["available"], 10)
        self.assertEqual(result["pool"]["available_accounts"], 1)
        self.assertEqual(result["pool"]["healthy"], 1)
        self.assertEqual(result["pool"]["unhealthy"], 1)

    def test_registration_concurrency_does_not_exceed_count(self):
        hotmail_local.import_accounts(
            "a@outlook.com----p----client-a----token-a\n"
            "b@outlook.com----p----client-b----token-b"
        )

        class Adapter:
            def start_registration(self, **kwargs):
                return {"ok": True, "kwargs": kwargs}

        settings = app_module.Settings(
            registration_target="grok",
            mail_provider="hotmail_local",
            count=2,
            concurrency=8,
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
        self.assertEqual(result["kwargs"]["stagger_ms"], 3000)
        self.assertEqual(
            result["kwargs"]["hotmail_local_base_url"],
            settings.hotmail_local_base_url,
        )
        self.assertEqual(result["kwargs"]["post_registration"]["step_delay_ms"], 3000)

    def test_registration_rejects_empty_hotmail_pool(self):
        settings = app_module.Settings(
            registration_target="grok",
            mail_provider="hotmail_local",
            count=0,
            concurrency=1,
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

    def test_grokfree_mail_profile_inherits_existing_cloudflare_key(self):
        profiles = app_module._normalize_mail_provider_configs({
            "custom": {
                "mail_base_url": "https://custom.example",
                "mail_api_key": "shared-worker-key",
                "mail_domain": "example.test",
            }
        })

        grokfree = profiles["cloudflare_grokfree"]
        self.assertEqual(grokfree["mail_base_url"], "")
        self.assertEqual(grokfree["mail_api_key"], "shared-worker-key")
        self.assertEqual(grokfree["mail_domain"], "")

    def test_grokfree_mail_profile_is_persisted_independently(self):
        old_config_file = app_module.CONFIG_FILE
        app_module.CONFIG_FILE = Path(self.temp_dir.name) / "config.json"
        try:
            with (
                patch.object(app_module, "apply_environment"),
                patch.object(app_module, "_sync_solver_proxy_file"),
            ):
                app_module.save_config({
                    "mail_provider": "cloudflare_grokfree",
                    "mail_base_url": "https://mail.example.test",
                    "mail_api_key": "grokfree-key",
                    "mail_domain": "mail.example.test",
                    "mail_provider_configs": {
                        "custom": {
                            "mail_base_url": "https://custom.example",
                            "mail_api_key": "custom-key",
                            "mail_domain": "example.test",
                        }
                    },
                })
            loaded = app_module.load_config()
            profiles = loaded["mail_provider_configs"]
            self.assertEqual(profiles["cloudflare_grokfree"]["mail_api_key"], "grokfree-key")
            self.assertEqual(profiles["custom"]["mail_api_key"], "custom-key")
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
