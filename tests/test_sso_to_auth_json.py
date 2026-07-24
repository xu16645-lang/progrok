import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import sso_to_auth_json as converter


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


if __name__ == "__main__":
    unittest.main()
