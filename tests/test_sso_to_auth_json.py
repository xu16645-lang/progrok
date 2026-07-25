import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import sso_to_auth_json as converter  # noqa: E402


class _Cookies:
    def set(self, *_args, **_kwargs):
        return None


class _Response:
    def __init__(self, *, status=200, url="https://accounts.x.ai/", text="", payload=None):
        self.status_code = status
        self.url = url
        self.text = text
        self.content = text.encode()
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class _TimeoutThenCodeSession:
    def __init__(self):
        self.calls = 0

    def post(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("curl: (28) Operation timed out")
        return _Response(payload={"device_code": "device", "user_code": "ABCD-1234"})


class _FlowSession:
    def __init__(self):
        self.cookies = _Cookies()
        self.verify_get_calls = 0

    def get(self, url, **_kwargs):
        if url == "https://accounts.x.ai/":
            return _Response(url=url)
        self.verify_get_calls += 1
        if self.verify_get_calls == 1:
            raise RuntimeError("curl: (28) Operation timed out")
        return _Response(url=url)

    def post(self, url, **_kwargs):
        if url.endswith("/oauth2/device/code"):
            return _Response(
                payload={
                    "device_code": "device-code",
                    "user_code": "ABCD-1234",
                    "verification_uri_complete": "https://accounts.x.ai/device?code=test",
                    "interval": 1,
                    "expires_in": 60,
                }
            )
        if url.endswith("/oauth2/device/verify"):
            return _Response(url="https://accounts.x.ai/oauth2/device/consent")
        if url.endswith("/oauth2/device/approve"):
            return _Response(url="https://accounts.x.ai/oauth2/device/done")
        if url.endswith("/oauth2/token"):
            return _Response(payload={"access_token": "access", "refresh_token": "refresh"})
        raise AssertionError(f"Unexpected URL: {url}")


class _RateLimitedSession:
    def __init__(self):
        self.cookies = _Cookies()

    def get(self, url, **_kwargs):
        return _Response(url=url)

    def post(self, url, **_kwargs):
        self.assert_url = url
        return _Response(
            status=429,
            text='{"error":"slow_down"}',
            payload={"error": "slow_down"},
        )


class _TokenSession:
    def __init__(self, payload):
        self.payload = payload

    def post(self, *_args, **_kwargs):
        return _Response(payload=self.payload)


class _UrlopenResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return converter.json.dumps(self.payload).encode()


class SsoToAuthJsonTests(unittest.TestCase):
    def test_device_code_retries_curl_timeout(self):
        session = _TimeoutThenCodeSession()
        failure = {}
        with (
            patch.object(converter, "_wait_device_flow_slot"),
            patch.object(converter, "_device_flow_retries", return_value=2),
            patch.object(converter.time, "sleep"),
            patch("builtins.print"),
        ):
            result = converter.request_device_code(session, failure=failure)

        self.assertEqual(result["device_code"], "device")
        self.assertEqual(session.calls, 2)
        self.assertEqual(failure, {})

    def test_rate_limit_has_specific_failure_details(self):
        session = _RateLimitedSession()
        failure = {}
        with (
            patch.object(converter.requests, "Session", return_value=session),
            patch.object(converter, "_wait_device_flow_slot"),
            patch.object(converter, "_device_flow_retries", return_value=1),
            patch("builtins.print"),
        ):
            result = converter.sso_to_token("sso", quiet=True, failure=failure)

        self.assertIsNone(result)
        self.assertEqual(failure["stage"], "device_code")
        self.assertEqual(failure["reason"], "rate_limited")
        self.assertTrue(failure["retryable"])

    def test_token_poll_rejects_session_response_without_refresh_token(self):
        failure = {}

        result = converter.poll_token(
            "device-code",
            session=_TokenSession({"access_token": "access"}),
            failure=failure,
        )

        self.assertIsNone(result)
        self.assertEqual(failure["stage"], "token_poll")
        self.assertIn("refresh_token", failure["detail"])
        self.assertNotIn("access", failure["detail"])

    def test_token_poll_rejects_urllib_response_without_access_token(self):
        failure = {}
        response = _UrlopenResponse({"refresh_token": "refresh"})

        with patch.object(converter.urllib.request, "urlopen", return_value=response):
            result = converter.poll_token("device-code", failure=failure)

        self.assertIsNone(result)
        self.assertEqual(failure["stage"], "token_poll")
        self.assertIn("access_token", failure["detail"])
        self.assertNotIn("refresh", failure["detail"])

    def test_token_poll_accepts_both_required_tokens(self):
        token = {"access_token": "access", "refresh_token": "refresh"}
        failure = {"stage": "old"}

        result = converter.poll_token(
            "device-code", session=_TokenSession(token), failure=failure
        )

        self.assertEqual(result, token)
        self.assertEqual(failure, {})

    def test_verify_timeout_retries_the_full_flow(self):
        session = _FlowSession()
        failure = {}
        with (
            patch.object(converter.requests, "Session", return_value=session),
            patch.object(converter, "_wait_device_flow_slot"),
            patch.object(converter, "_device_flow_retries", return_value=2),
            patch.object(converter.time, "sleep"),
            patch("builtins.print"),
        ):
            token = converter.sso_to_token("sso", quiet=True, failure=failure)

        self.assertEqual(token["access_token"], "access")
        self.assertEqual(session.verify_get_calls, 2)
        self.assertEqual(failure, {})

    def test_browser_authorization_failure_does_not_open_a_second_device_flow(self):
        session = _FlowSession()
        failure = {}
        authorize_calls = []

        def authorize(url, code):
            authorize_calls.append((url, code))
            raise RuntimeError("browser authorization timed out")

        with (
            patch.object(converter.requests, "Session", return_value=session),
            patch.object(converter, "_wait_device_flow_slot"),
            patch.object(converter, "_device_flow_retries", return_value=3),
            patch("builtins.print"),
        ):
            token = converter.sso_to_token_with_browser(
                "sso", authorize, quiet=True, failure=failure
            )

        self.assertIsNone(token)
        self.assertEqual(len(authorize_calls), 1)
        self.assertEqual(failure["stage"], "approve")
        self.assertFalse(failure["retryable"])


if __name__ == "__main__":
    unittest.main()
