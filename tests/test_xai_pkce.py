import json
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import xai_pkce  # noqa: E402


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _HTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class XaiPKCETests(unittest.TestCase):
    def test_token_payload_preserves_at_rt_and_local_client(self):
        claims = base64.urlsafe_b64encode(
            json.dumps({"email": "claim@example.com", "exp": 2_000_000_000}).encode()
        ).decode().rstrip("=")
        access_token = f"header.{claims}.signature"
        payload = xai_pkce.token_to_auth_payload(
            {
                "access_token": access_token,
                "refresh_token": "refresh-token",
                "id_token": "id-token",
            },
            email="User@Example.com",
        )

        self.assertEqual(payload["access_token"], access_token)
        self.assertEqual(payload["refresh_token"], "refresh-token")
        self.assertEqual(payload["oidc_client_id"], xai_pkce.CLIENT_ID)
        self.assertEqual(payload["email"], "user@example.com")

        for token in ({"access_token": "at"}, {"refresh_token": "rt"}):
            with self.subTest(token=token):
                with self.assertRaisesRegex(xai_pkce.XaiPKCEError, "access_token"):
                    xai_pkce.token_to_auth_payload(token, email="user@example.com")

    def test_sessions_are_isolated_and_authorization_url_matches_sub2(self):
        first = xai_pkce.create_pkce_session()
        second = xai_pkce.create_pkce_session()

        self.assertNotEqual(first.session_id, second.session_id)
        self.assertNotEqual(first.state, second.state)
        self.assertNotEqual(first.code_verifier, second.code_verifier)
        query = parse_qs(urlparse(xai_pkce.build_authorization_url(first)).query)
        self.assertEqual(query["client_id"], [xai_pkce.CLIENT_ID])
        self.assertEqual(query["state"], [first.state])
        self.assertEqual(query["code_challenge"], [first.code_challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["plan"], ["generic"])
        self.assertEqual(query["referrer"], ["sub2api"])

    def test_callback_requires_matching_state(self):
        with self.assertRaisesRegex(xai_pkce.XaiPKCEError, "state"):
            xai_pkce.parse_callback(
                "http://127.0.0.1:56121/callback?code=abc&state=wrong",
                "expected",
            )

    def test_exchange_posts_pkce_form_and_requires_at_rt(self):
        session = xai_pkce.create_pkce_session()
        http = _HTTP(
            _Response(payload={"access_token": "at", "refresh_token": "rt"})
        )
        token = xai_pkce.exchange_code(
            "auth-code",
            session,
            http_session=http,
            proxy_kwargs={"proxies": {"https": "http://proxy"}},
        )

        self.assertEqual(token["refresh_token"], "rt")
        url, kwargs = http.calls[0]
        self.assertEqual(url, xai_pkce.TOKEN_URL)
        self.assertEqual(kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(kwargs["data"]["code_verifier"], session.code_verifier)
        self.assertEqual(kwargs["data"]["redirect_uri"], xai_pkce.REDIRECT_URI)
        self.assertIn("proxies", kwargs)

        missing_rt = _HTTP(_Response(payload={"access_token": "at"}))
        with self.assertRaisesRegex(xai_pkce.XaiPKCEError, "refresh_token"):
            xai_pkce.exchange_code("code", session, http_session=missing_rt)

    def test_authorize_exchange_persists_then_clears_verifier(self):
        session_seen = {}
        stages = []
        http = _HTTP(
            _Response(payload={"access_token": "at", "refresh_token": "rt"})
        )

        def authorize(url, state):
            session_seen.update(parse_qs(urlparse(url).query))
            return f"{xai_pkce.REDIRECT_URI}?code=ok&state={state}"

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            token = xai_pkce.authorize_and_exchange(
                authorize,
                session_path=path,
                on_progress=stages.append,
                http_session=http,
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(token["access_token"], "at")
        self.assertEqual(persisted["code_verifier"], "")
        self.assertTrue(persisted["session_id"])
        self.assertTrue(persisted["state"])
        self.assertEqual(
            stages, ["session_created", "callback_captured", "token_exchanged"]
        )
        self.assertEqual(session_seen["scope"], [xai_pkce.SCOPE])

    def test_http_error_is_sanitized(self):
        session = xai_pkce.create_pkce_session()
        http = _HTTP(_Response(400, {"error": "invalid_grant"}))
        with self.assertRaisesRegex(xai_pkce.XaiPKCEError, "invalid_grant"):
            xai_pkce.exchange_code("secret-code", session, http_session=http)


if __name__ == "__main__":
    unittest.main()
