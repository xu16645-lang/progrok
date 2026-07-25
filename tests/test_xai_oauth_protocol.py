from pathlib import Path
import sys
from urllib.parse import urlencode


VENDOR_ROOT = Path(__file__).resolve().parents[1] / "vendor" / "grok-build-auth"
if str(VENDOR_ROOT) not in sys.path:
    sys.path.insert(0, str(VENDOR_ROOT))

from xconsole_client.oauth_protocol import (  # noqa: E402
    ProtocolOAuthClient,
    SUBMIT_OAUTH2_CONSENT_ACTION,
)


class _Response:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.url = "https://accounts.x.ai/oauth2/consent"


class _Session:
    def __init__(self, dynamic_action):
        self.dynamic_action = dynamic_action
        self.actions = []

    def post(self, _url, *, headers, **_kwargs):
        action = headers.get("next-action")
        self.actions.append(action)
        if action == SUBMIT_OAUTH2_CONSENT_ACTION:
            return _Response(200, '1:{"success":false,"error":"Access denied"}')
        if action == self.dynamic_action:
            return _Response(200, '{"code":"fresh-code"}')
        return _Response(400, "unexpected action")


def test_external_oauth_discovers_changed_server_action():
    dynamic_action = "a" * 48
    client = ProtocolOAuthClient.__new__(ProtocolOAuthClient)
    client._s = _Session(dynamic_action)
    client.load_cookies = lambda _cookies: None
    client._set_sso_cookie = lambda _sso: None
    client.create_cookie_setter_link = lambda *_args, **_kwargs: {
        "ok": True,
        "cookie_setter_url": "https://accounts.x.ai/set-cookie/test",
    }

    query = {
        "response_type": "code",
        "client_id": "client",
        "redirect_uri": "http://127.0.0.1/callback",
        "scope": "openid",
        "state": "state",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "nonce": "nonce",
    }
    auth_url = "https://auth.x.ai/oauth2/authorize?" + urlencode(query)
    consent_url = "https://accounts.x.ai/oauth2/consent"

    def fake_get(url, **_kwargs):
        if url == auth_url:
            return _Response()
        if "set-cookie" in url:
            return _Response(headers={"location": consent_url})
        if url == consent_url:
            return _Response(text='<script src="/_next/static/chunks/consent.js"></script>')
        if "consent.js" in url:
            return _Response(
                text=(
                    f'(0,i.createServerReference)("{dynamic_action}",i.callServer,'
                    'void 0,i.findSourceMapURL,"submitOAuth2Consent")'
                )
            )
        raise AssertionError(url)

    client._get = fake_get
    code = client.authorize_existing_request(
        auth_url,
        expected_state="state",
        session_cookies={"sso": "session-jwt"},
    )

    assert code == "fresh-code"
    assert client._s.actions == [dynamic_action]


def test_external_oauth_does_not_fall_back_to_stale_bundled_action():
    client = ProtocolOAuthClient.__new__(ProtocolOAuthClient)
    client._s = _Session("b" * 48)
    client.load_cookies = lambda _cookies: None
    client._set_sso_cookie = lambda _sso: None
    client.create_cookie_setter_link = lambda *_args, **_kwargs: {
        "ok": True,
        "cookie_setter_url": "https://accounts.x.ai/set-cookie/test",
    }

    query = {
        "response_type": "code",
        "client_id": "client",
        "redirect_uri": "http://127.0.0.1/callback",
        "scope": "openid",
        "state": "state",
        "code_challenge": "challenge",
        "code_challenge_method": "S256",
        "nonce": "nonce",
    }
    auth_url = "https://auth.x.ai/oauth2/authorize?" + urlencode(query)
    consent_url = "https://accounts.x.ai/oauth2/consent"

    def fake_get(url, **_kwargs):
        if url == auth_url:
            return _Response()
        if "set-cookie" in url:
            return _Response(headers={"location": consent_url})
        if url == consent_url:
            return _Response(text="<html><body>Authorize</body></html>")
        raise AssertionError(url)

    client._get = fake_get
    try:
        client.authorize_existing_request(
            auth_url,
            expected_state="state",
            session_cookies={"sso": "session-jwt"},
        )
    except RuntimeError as exc:
        assert "live page bundles" in str(exc)
    else:
        raise AssertionError("missing live action must fail closed")

    assert client._s.actions == []
