from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import moemail


def _mock_client(handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)
    return patch(
        "moemail.httpx.Client",
        side_effect=lambda **kwargs: real_client(transport=transport, **kwargs),
    )


def test_stalwart_virtual_mailbox_and_jmap_recipient_filter() -> None:
    target = "random123@gptfree.jo3.org"
    expected_auth = "Basic " + base64.b64encode(
        b"admin@gptfree.jo3.org:secret"
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == expected_auth
        if request.method == "GET" and request.url.path == "/jmap/session":
            return httpx.Response(
                200,
                json={
                    "primaryAccounts": {
                        "urn:ietf:params:jmap:mail": "account-1",
                    }
                },
            )
        assert request.method == "POST" and request.url.path == "/jmap/"
        payload = json.loads(request.content)
        assert payload["methodCalls"][0][1]["filter"] == {"to": target}
        return httpx.Response(
            200,
            json={
                "methodResponses": [
                    [
                        "Email/get",
                        {
                            "list": [
                                {
                                    "id": "message-1",
                                    "subject": "OpenAI verification code",
                                    "from": [{"email": "noreply@openai.com"}],
                                    "to": [{"email": target}],
                                    "bodyValues": {
                                        "part-1": {"value": "Your code is 845219"}
                                    },
                                },
                                {
                                    "id": "message-2",
                                    "subject": "Wrong recipient",
                                    "to": [{"email": "other@gptfree.jo3.org"}],
                                    "bodyValues": {
                                        "part-1": {"value": "Your code is 111111"}
                                    },
                                },
                            ]
                        },
                        "get",
                    ]
                ]
            },
        )

    with _mock_client(handler):
        mailbox = moemail.create_mailbox(
            provider="stalwart",
            name="random123",
            domain="gptfree.jo3.org",
            api_key="secret",
            base_url="http://mail.example.test/admin",
        )
        messages = moemail.fetch_messages(
            mailbox["id"],
            provider="stalwart",
            address=mailbox["email"],
            api_key="secret",
            base_url="http://mail.example.test/admin",
        )

    assert mailbox["email"] == target
    assert len(messages) == 1
    assert messages[0]["extracted"]["codes"] == ["845219"]


def test_stalwart_accepts_explicit_admin_username() -> None:
    username, password = moemail._stalwart_credentials(
        "service@gptfree.jo3.org:password-value",
        "gptfree.jo3.org",
    )
    assert username == "service@gptfree.jo3.org"
    assert password == "password-value"
