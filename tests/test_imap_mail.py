from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import moemail


RAW_MESSAGE = b"""From: verify@example.com
To: random123@example.net
Subject: Verification code
Content-Type: text/plain; charset=utf-8

Your verification code is 845219.
"""


class FakeImap:
    def __init__(self, host, port, **kwargs):
        assert host == "mail.example.test"
        assert port == 993

    def login(self, username, password):
        assert username == "collector@example.net"
        assert password == "secret:value"
        return "OK", []

    def select(self, folder, readonly=False):
        assert folder == "INBOX"
        assert readonly is True
        return "OK", [b"1"]

    def noop(self):
        return "OK", []

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"1"]
        assert command == "fetch"
        return "OK", [(b"1 (BODY[] {1})", RAW_MESSAGE), b")"]

    def logout(self):
        return "BYE", []


@patch("mail_protocols.imap_mail.imaplib.IMAP4_SSL", FakeImap)
def test_custom_imaps_creates_virtual_address_and_reads_code() -> None:
    kwargs = {
        "provider": "custom",
        "base_url": "imaps://mail.example.test:993/INBOX",
        "api_key": "collector@example.net:secret:value",
    }
    mailbox = moemail.create_mailbox(
        name="random123",
        domain="example.net",
        **kwargs,
    )
    messages = moemail.fetch_messages(
        mailbox["id"],
        address=mailbox["email"],
        **kwargs,
    )

    assert mailbox == {
        "id": "random123@example.net",
        "email": "random123@example.net",
        "token": "",
        "provider": "imap",
    }
    assert messages[0]["extracted"]["codes"] == ["845219"]


def test_custom_https_keeps_cloudflare_compatibility() -> None:
    assert (
        moemail.normalize_mail_provider("custom", base_url="https://mail.example.test")
        == "cfmail"
    )


@patch("mail_protocols.imap_mail.random.choice", side_effect=lambda domains: domains[1])
@patch("mail_protocols.imap_mail.imaplib.IMAP4_SSL", FakeImap)
def test_custom_imaps_randomly_selects_from_domain_pool(choose) -> None:
    mailbox = moemail.create_mailbox(
        provider="custom",
        name="random123",
        domain="first.example.net, second.example.net; first.example.net",
        base_url="imaps://mail.example.test:993/INBOX",
        api_key="collector@example.net:secret:value",
    )

    assert mailbox["email"] == "random123@second.example.net"
    choose.assert_called_once_with(["first.example.net", "second.example.net"])
