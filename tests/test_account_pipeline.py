import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from account_pipeline import (
    CHATGPT_SUB2API_MODELS,
    create_grok_oauth_in_sub2api,
    import_grok_sso_to_sub2api,
    import_account,
    import_chatgpt_to_sub2api,
    import_to_sub2api,
    import_to_cpa,
    list_sub2api_groups,
    probe_account,
    probe_grok_account_in_sub2api,
)
from export_formats import cpa_chatgpt_agent_identity_filename
import accounts
from accounts import import_auth_payload


class ProbeFallbackTests(unittest.TestCase):
    def test_timeout_is_retryable_without_models_fallback(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            raise httpx.ReadTimeout("model busy", request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = probe_account(
                {"access_token": "test", "base_url": "https://example.test/v1"},
                client=client,
            )

        self.assertIsNone(result["available"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["classification"], "uncertain")
        self.assertEqual(["/v1/responses"], calls)

    def test_double_timeout_is_retryable_not_dead(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("busy", request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = probe_account(
                {"access_token": "test", "base_url": "https://example.test/v1"},
                client=client,
            )

        self.assertIsNone(result["available"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["classification"], "uncertain")

    def test_unauthorized_is_hard_failure(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(401, json={"error": "invalid token"}, request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = probe_account(
                {"access_token": "test", "base_url": "https://example.test/v1"},
                client=client,
            )

        self.assertFalse(result["available"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["classification"], "invalid_credential")
        self.assertEqual(calls, ["/v1/responses"])

    def test_expired_xai_token_refreshes_before_probe(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.url.host, request.url.path, request.headers.get("Authorization")))
            if request.url.host == "auth.x.ai":
                self.assertIn(b"grant_type=refresh_token", request.content)
                self.assertIn(b"client_id=b1a00492", request.content)
                return httpx.Response(
                    200,
                    json={
                        "access_token": "fresh-token",
                        "refresh_token": "rotated-refresh-token",
                        "expires_in": 21600,
                    },
                    request=request,
                )
            self.assertEqual("Bearer fresh-token", request.headers.get("Authorization"))
            self.assertIsNone(request.headers.get("X-XAI-Token-Auth"))
            self.assertEqual("interactive", request.headers.get("X-Grok-Client-Mode"))
            self.assertEqual("sub2api-grok/1.0", request.headers.get("User-Agent"))
            self.assertEqual(
                {"model": "grok-4.5", "input": "hi", "stream": True},
                json.loads(request.content),
            )
            return httpx.Response(
                200,
                text=(
                    'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                    'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                ),
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = probe_account(
                {
                    "access_token": "expired-token",
                    "refresh_token": "refresh-token",
                    "expires_at": time.time() - 10,
                    "base_url": "https://example.test/v1",
                },
                client=client,
            )

        self.assertTrue(result["available"])
        self.assertTrue(result["token_refreshed"])
        self.assertEqual(
            [("auth.x.ai", "/oauth2/token"), ("example.test", "/v1/responses")],
            [(host, path) for host, path, _auth in calls],
        )

    def test_401_forces_one_refresh_then_retries_probe(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            authorization = request.headers.get("Authorization")
            calls.append((request.url.host, request.url.path, authorization))
            if request.url.host == "auth.x.ai":
                return httpx.Response(
                    200,
                    json={"access_token": "new-token", "expires_in": 21600},
                    request=request,
                )
            if authorization == "Bearer old-token":
                return httpx.Response(401, json={"error": "expired"}, request=request)
            self.assertEqual("Bearer new-token", authorization)
            return httpx.Response(
                200,
                text=(
                    'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                    'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                ),
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = probe_account(
                {
                    "access_token": "old-token",
                    "refresh_token": "refresh-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "base_url": "https://example.test/v1",
                },
                client=client,
            )

        self.assertTrue(result["available"])
        self.assertTrue(result["token_refreshed"])
        self.assertEqual(
            [
                ("example.test", "/v1/responses", "Bearer old-token"),
                ("auth.x.ai", "/oauth2/token", None),
                ("example.test", "/v1/responses", "Bearer new-token"),
            ],
            calls,
        )


class XaiCredentialPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name)
        self.accounts_dir = self.data_dir / "accounts"
        self.accounts_dir.mkdir(parents=True)
        self.auth_path = self.data_dir / "auth.json"
        self.patches = [
            patch.object(accounts, "ACCOUNTS_DIR", self.accounts_dir),
            patch.object(accounts, "MERGED_AUTH", self.auth_path),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_refresh_updates_cpa_and_merged_auth_files(self):
        account_path = self.accounts_dir / "xai-user@example.com.json"
        account_path.write_text(
            json.dumps(
                {
                    "type": "xai",
                    "auth_kind": "oauth",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "expired": "2026-07-01T00:00:00Z",
                    "email": "user@example.com",
                    "sub": "user-1",
                }
            ),
            encoding="utf-8",
        )
        auth_key = "https://auth.x.ai::client::user-1"
        self.auth_path.write_text(
            json.dumps(
                {
                    auth_key: {
                        "key": "old-access",
                        "auth_mode": "oidc",
                        "refresh_token": "old-refresh",
                        "expires_at": "2026-07-01T00:00:00Z",
                        "email": "user@example.com",
                        "user_id": "user-1",
                        "oidc_issuer": "https://auth.x.ai",
                    }
                }
            ),
            encoding="utf-8",
        )

        result = accounts.persist_xai_oauth_refresh(
            {
                "sub": "user-1",
                "email": "user@example.com",
                "_local_account_path": str(account_path),
                "_local_auth_key": auth_key,
            },
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_at": "2026-07-24T12:00:00Z",
                "refreshed_at": "2026-07-24T06:00:00Z",
            },
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        saved_account = json.loads(account_path.read_text(encoding="utf-8"))
        saved_auth = json.loads(self.auth_path.read_text(encoding="utf-8"))
        self.assertEqual("new-access", saved_account["access_token"])
        self.assertEqual("new-refresh", saved_account["refresh_token"])
        self.assertEqual("2026-07-24T12:00:00Z", saved_account["expired"])
        self.assertEqual("new-access", saved_auth[auth_key]["key"])
        self.assertEqual("new-refresh", saved_auth[auth_key]["refresh_token"])
        self.assertEqual("2026-07-24T12:00:00Z", saved_auth[auth_key]["expires_at"])

    def test_agent_identity_task_recovery_updates_merged_and_account_files(self):
        runtime_id = "runtime-1"
        auth_key = f"chatgpt-agent::{runtime_id}"
        self.auth_path.write_text(
            json.dumps(
                {
                    auth_key: {
                        "auth_mode": "agent_identity",
                        "email": "user@example.com",
                        "agent_identity": {
                            "agent_runtime_id": runtime_id,
                            "agent_private_key": "private-key",
                            "task_id": "old-task",
                            "account_id": "account-1",
                            "chatgpt_user_id": "user-1",
                            "email": "user@example.com",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        account_path = self.accounts_dir / "codex-agent-test.json"
        account_path.write_text(
            json.dumps(
                {
                    "type": "codex",
                    "agent_runtime_id": runtime_id,
                    "task_id": "old-task",
                    "agent_identity": {"agent_runtime_id": runtime_id, "task_id": "old-task"},
                }
            ),
            encoding="utf-8",
        )

        loaded = accounts.load_chatgpt_agent_identity(runtime_id=runtime_id)
        self.assertEqual("old-task", loaded["task_id"])
        result = accounts.persist_chatgpt_agent_task(loaded, "new-task")

        self.assertTrue(result["ok"])
        saved_auth = json.loads(self.auth_path.read_text(encoding="utf-8"))
        saved_account = json.loads(account_path.read_text(encoding="utf-8"))
        self.assertEqual("new-task", saved_auth[auth_key]["agent_identity"]["task_id"])
        self.assertEqual("new-task", saved_account["task_id"])
        self.assertEqual("new-task", saved_account["agent_identity"]["task_id"])


class Sub2APIImportTests(unittest.TestCase):
    def setUp(self):
        self.agent_record = {
            "auth_mode": "agent_identity",
            "email": "user@example.com",
            "access_token": "must-not-upload",
            "refresh_token": "must-not-upload",
            "agent_identity": {
                "agent_runtime_id": "runtime-1",
                "agent_private_key": "private-key",
                "task_id": "task-1",
                "account_id": "account-1",
                "chatgpt_user_id": "user-1",
                "email": "user@example.com",
                "plan_type": "free",
                "chatgpt_account_is_fedramp": False,
            },
        }

    def test_grok_oauth_callback_creates_account_directly_in_sub2api(self):
        requests = []
        authorized = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/oauth/auth-url"):
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "auth_url": "https://auth.x.ai/oauth2/authorize?state=state-1",
                            "session_id": "sub2-session-1",
                            "state": "state-1",
                        }
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                json={"data": {"id": 88, "name": "grok@example.com"}},
                request=request,
            )

        def authorize(url, state):
            authorized.append((url, state))
            return "http://127.0.0.1:56121/callback?code=code-1&state=state-1"

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = create_grok_oauth_in_sub2api(
                authorize=authorize,
                email="grok@example.com",
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                group_id=20,
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], 88)
        self.assertEqual(
            [request.url.path for request in requests],
            [
                "/api/v1/admin/grok/oauth/auth-url",
                "/api/v1/admin/grok/oauth/create-from-oauth",
            ],
        )
        self.assertEqual(
            authorized,
            [("https://auth.x.ai/oauth2/authorize?state=state-1", "state-1")],
        )
        create_body = json.loads(requests[1].content)
        self.assertEqual(create_body["session_id"], "sub2-session-1")
        self.assertEqual(create_body["group_ids"], [20])
        self.assertIn("callback?code=code-1", create_body["code"])
        self.assertNotIn("access_token", create_body)
        self.assertNotIn("refresh_token", create_body)

    def test_grok_sso_import_uses_native_sub2api_endpoint_and_group(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "created": [
                            {
                                "email": "grok@example.com",
                                "account": {"id": 91, "name": "grok@example.com"},
                            }
                        ],
                        "failed": [],
                    }
                },
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_grok_sso_to_sub2api(
                sso_token="signed-sso",
                email="grok@example.com",
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                group_id=20,
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], 91)
        self.assertEqual(result["path"], "grok_sso_to_oauth")
        self.assertEqual(requests[0].url.path, "/api/v1/admin/grok/sso-to-oauth")
        body = json.loads(requests[0].content)
        self.assertEqual(body["sso_token"], "signed-sso")
        self.assertEqual(body["group_ids"], [20])

    def test_callback_imported_grok_probe_uses_sub2api_account_test(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                text=(
                    'data: {"type":"content","text":"ok"}\n\n'
                    'data: {"type":"test_complete","success":true}\n\n'
                ),
                headers={"content-type": "text/event-stream"},
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = probe_grok_account_in_sub2api(
                88,
                model="grok-4.5",
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["available"])
        self.assertEqual(requests[0].url.path, "/api/v1/admin/accounts/88/test")
        self.assertEqual(json.loads(requests[0].content)["model_id"], "grok-4.5")

    def test_agent_identity_uses_codex_session_group_and_fixed_models(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/v1/admin/accounts":
                return httpx.Response(
                    200,
                    json={"data": {"items": [{"id": 41, "name": "user@example.com", "credentials": {"email": "user@example.com", "task_id": "task-1"}}]}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"data": {"total": 1, "created": 1, "updated": 0, "failed": 0}},
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_chatgpt_to_sub2api(
                self.agent_record,
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                group_id=23,
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 2)
        request = requests[0]
        self.assertEqual(request.url.path, "/api/v1/admin/accounts/import/codex-session")
        self.assertEqual(request.headers["x-api-key"], "admin-key")
        body = json.loads(request.content)
        auth_json = json.loads(body["content"])
        self.assertEqual(auth_json["auth_mode"], "agentIdentity")
        self.assertEqual(auth_json["agent_identity"]["task_id"], "task-1")
        self.assertNotIn("access_token", body["content"])
        self.assertNotIn("refresh_token", body["content"])
        self.assertEqual(body["group_ids"], [23])
        self.assertTrue(body["skip_default_group_bind"])
        self.assertEqual(
            body["credential_extras"]["model_mapping"],
            {model: model for model in CHATGPT_SUB2API_MODELS},
        )
        self.assertTrue(result["task_ready"])
        self.assertEqual(result["account_id"], 41)

    def test_agent_identity_without_task_id_is_rejected_before_upload(self):
        self.agent_record["agent_identity"]["task_id"] = ""
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500, request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_chatgpt_to_sub2api(
                self.agent_record,
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                client=client,
            )

        self.assertFalse(result["ok"])
        self.assertIn("task_id", result["error"])
        self.assertEqual(requests, [])

    def test_agent_identity_uses_cpa_codex_auth_json(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok"}, request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_to_cpa(
                self.agent_record,
                base_url="https://cpa.example.test/management.html#",
                api_key="management-key",
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.url.path, "/v0/management/auth-files")
        self.assertEqual(
            request.url.params["name"],
            cpa_chatgpt_agent_identity_filename({"agent_runtime_id": "runtime-1"}),
        )
        self.assertEqual(request.headers["authorization"], "Bearer management-key")
        body = json.loads(request.content)
        self.assertEqual(body["type"], "codex")
        self.assertEqual(body["auth_kind"], "agent_identity")
        self.assertEqual(body["auth_mode"], "agent_identity")
        self.assertEqual(body["agent_identity"]["task_id"], "task-1")
        self.assertNotIn("access_token", body)
        self.assertNotIn("refresh_token", body)

    def test_cpa_rejects_empty_xai_payload_before_upload(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok"}, request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_to_cpa(
                {"type": "xai", "email": "missing-token@example.com"},
                base_url="https://cpa.example.test",
                api_key="management-key",
                client=client,
            )

        self.assertFalse(result["ok"])
        self.assertIn("access_token", result["error"])
        self.assertEqual(requests, [])

    def test_cpa_does_not_downgrade_malformed_agent_identity_to_xai(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok"}, request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_to_cpa(
                {
                    "auth_mode": "agent_identity",
                    "email": "agent@example.com",
                    "access_token": "must-not-be-treated-as-xai",
                },
                base_url="https://cpa.example.test",
                api_key="management-key",
                client=client,
            )

        self.assertFalse(result["ok"])
        self.assertIn("Agent Identity", result["error"])
        self.assertEqual(requests, [])

    def test_local_cpa_export_uses_agent_identity_format(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch("accounts.ACCOUNTS_DIR", root / "accounts"),
                patch("accounts.MERGED_AUTH", root / "auth.json"),
            ):
                result = import_auth_payload(self.agent_record, output_format="cpa")

            self.assertTrue(result["ok"])
            output_path = Path(result["imported"][0]["path"])
            self.assertEqual(output_path.name, cpa_chatgpt_agent_identity_filename({"agent_runtime_id": "runtime-1"}))
            saved = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["type"], "codex")
            self.assertEqual(saved["auth_kind"], "agent_identity")
            self.assertEqual(saved["agent_identity"]["account_id"], "account-1")

    def test_local_identity_storage_rejects_missing_task_id(self):
        payload = {
            "auth_mode": "agent_identity",
            "agent_identity": {
                "agent_runtime_id": "runtime-1",
                "agent_private_key": "private-key",
                "task_id": "",
                "account_id": "account-1",
                "chatgpt_user_id": "user-1",
            },
        }

        result = import_auth_payload(payload)

        self.assertFalse(result["ok"])
        self.assertIn("task_id", result["error"])

    def test_import_account_keeps_grok_on_legacy_data_endpoint(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"data": {"account_created": 1, "account_failed": 0}},
                request=request,
            )

        config = {
            "target": "sub2api",
            "sub2api_base_url": "https://sub2.example.test",
            "sub2api_auth_mode": "api_key",
            "sub2api_api_key": "admin-key",
        }
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_to_sub2api(
                {"type": "xai", "email": "grok@example.com", "access_token": "token"},
                base_url=config["sub2api_base_url"],
                auth_mode=config["sub2api_auth_mode"],
                api_key=config["sub2api_api_key"],
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual([request.url.path for request in requests], ["/api/v1/admin/accounts/data"])
        body = json.loads(requests[0].content)
        self.assertNotIn("group_ids", body)
        self.assertFalse(body["skip_default_group_bind"])

    def test_xai_import_binds_selected_grok_group(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/v1/admin/accounts":
                return httpx.Response(
                    200,
                    json={"data": {"items": [{"id": 61, "name": "grok@example.com", "credentials": {"email": "grok@example.com"}}]}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"data": {"account_created": 1, "account_failed": 0}},
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = import_to_sub2api(
                {"type": "xai", "email": "grok@example.com", "access_token": "token"},
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                group_id=27,
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["group_id"], 27)
        self.assertEqual(result["account_id"], 61)
        self.assertEqual(
            [request.url.path for request in requests],
            ["/api/v1/admin/accounts/data", "/api/v1/admin/accounts", "/api/v1/admin/accounts/61"],
        )
        import_body = json.loads(requests[0].content)
        self.assertNotIn("group_ids", import_body)
        self.assertTrue(import_body["skip_default_group_bind"])
        self.assertEqual(json.loads(requests[2].content), {"group_ids": [27]})

    def test_password_login_and_group_response_shapes(self):
        shapes = [
            {"data": {"items": [{"id": 1, "name": "OpenAI A", "platform": "openai"}]}},
            {"data": {"groups": [{"id": 2, "name": "OpenAI B", "platform": "openai"}]}},
            [{"id": 3, "name": "OpenAI C", "platform": "openai"}],
        ]
        for index, shape in enumerate(shapes, start=1):
            with self.subTest(shape=index):
                calls = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request)
                    if request.url.path == "/api/v1/auth/login":
                        return httpx.Response(200, json={"data": {"access_token": "session-token"}}, request=request)
                    return httpx.Response(200, json=shape, request=request)

                with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                    result = list_sub2api_groups(
                        base_url="https://sub2.example.test",
                        auth_mode="password",
                        admin_email="admin@example.com",
                        admin_password="secret",
                        client=client,
                    )

                self.assertTrue(result["ok"])
                self.assertEqual(result["groups"][0]["id"], index)
                self.assertEqual(calls[1].headers["authorization"], "Bearer session-token")
                self.assertIsNone(calls[1].url.params.get("platform"))

    def test_group_listing_includes_all_platforms(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "items": [
                            {"id": 7, "name": "Grok A", "platform": "grok"},
                            {"id": 8, "name": "OpenAI A", "platform": "openai"},
                        ]
                    }
                },
                request=request,
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = list_sub2api_groups(
                base_url="https://sub2.example.test",
                auth_mode="api_key",
                api_key="admin-key",
                client=client,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["groups"],
            [
                {"id": 7, "name": "Grok A", "platform": "grok"},
                {"id": 8, "name": "OpenAI A", "platform": "openai"},
            ],
        )
        self.assertIsNone(calls[0].url.params.get("platform"))

    def test_import_account_uses_separate_xai_group(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/api/v1/admin/accounts":
                return httpx.Response(
                    200,
                    json={"data": {"items": [{"id": 62, "name": "grok@example.com", "credentials": {"email": "grok@example.com"}}]}},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"data": {"account_created": 1, "account_failed": 0}},
                request=request,
            )

        config = {
            "target": "sub2api",
            "sub2api_base_url": "https://sub2.example.test",
            "sub2api_auth_mode": "api_key",
            "sub2api_api_key": "admin-key",
            "sub2api_group_id": 9,
            "sub2api_xai_group_id": 31,
        }
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with patch("account_pipeline.httpx.Client", return_value=client):
                result = import_account(
                    {"type": "xai", "email": "grok@example.com", "access_token": "token"},
                    config,
                )

        self.assertTrue(result["ok"])
        self.assertEqual([request.url.path for request in requests], ["/api/v1/admin/accounts/data", "/api/v1/admin/accounts", "/api/v1/admin/accounts/62"])
        self.assertTrue(json.loads(requests[0].content)["skip_default_group_bind"])
        self.assertEqual(json.loads(requests[2].content), {"group_ids": [31]})

    def test_import_account_routes_agent_identity_to_new_endpoint(self):
        paths = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/api/v1/admin/accounts":
                return httpx.Response(
                    200,
                    json={"data": {"items": [{"id": 42, "name": "user@example.com", "credentials": {"email": "user@example.com", "task_id": "task-1"}}]}},
                    request=request,
                )
            return httpx.Response(200, json={"created": 1, "failed": 0}, request=request)

        config = {
            "target": "sub2api",
            "sub2api_base_url": "https://sub2.example.test",
            "sub2api_auth_mode": "api_key",
            "sub2api_api_key": "admin-key",
            "sub2api_group_id": 9,
        }
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            from unittest.mock import patch

            with patch("account_pipeline.httpx.Client", return_value=client):
                result = import_account(self.agent_record, config)

        self.assertTrue(result["ok"])
        self.assertEqual(paths, ["/api/v1/admin/accounts/import/codex-session", "/api/v1/admin/accounts"])


if __name__ == "__main__":
    unittest.main()
