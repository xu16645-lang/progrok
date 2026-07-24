#!/usr/bin/env python3
"""ChatGPT session JSON → auth.json converter.

Converts a ChatGPT session (from https://chatgpt.com/api/auth/session) into
an auth.json entry in agent_identity format (compatible with ZCode agent runtime).

The session endpoint returns:
  {
    "user": {"id": "user-xxx", "name": "...", "email": "..."},
    "accessToken": "eyJ...",
    "expires": "2026-08-21T..."
  }

This module generates the agent_identity auth.json entry by:
1. Extracting user identity from the session
2. Generating (or fetching) agent runtime credentials
3. Writing/merging into the project's auth.json

Usage as CLI:
  python chatgpt_session_to_auth.py --session-file session.json [--out auth.json]
  python chatgpt_session_to_auth.py --session '{"user":...}' [--out auth.json]
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat, load_der_private_key
from curl_cffi import requests as curl_requests
from nacl.bindings import crypto_sign_ed25519_sk_to_curve25519
from nacl.public import PrivateKey as Curve25519PrivateKey, SealedBox

# Project paths
BACKEND_DIR = Path(__file__).resolve().parent
APP_DIR = BACKEND_DIR.parent
DATA_DIR = APP_DIR / "runtime" / "data"
DEFAULT_AUTH_FILE = DATA_DIR / "auth.json"

# OIDC / agent identity constants
CHATGPT_ISSUER = "https://auth.openai.com"
CHATGPT_CLIENT_ID = "chatgpt-web"
AGENT_IDENTITY_KEY_PREFIX = "chatgpt-agent"
AUTHAPI_BASE = "https://auth.openai.com/api/accounts"
AGENT_VERSION = "0.138.0-alpha.6"
AGENT_HARNESS_ID = "codex-cli"
RUNNING_LOCATION = "local"
AGENT_REQUEST_TIMEOUT = 30
_RESPONSE_PREVIEW_LIMIT = 420


class AgentIdentityUpstreamError(RuntimeError):
    """A safe, actionable error returned by the Agent Identity upstream."""

    def __init__(
        self,
        operation: str,
        *,
        status_code: int,
        message: str,
        error_code: str = "",
    ) -> None:
        self.operation = operation
        self.status_code = int(status_code or 0)
        self.error_code = str(error_code or "").strip().lower()
        self.message = str(message or "").strip()
        suffix = f"（{self.error_code}）" if self.error_code else ""
        status = f"HTTP {self.status_code}" if self.status_code else "无 HTTP 状态"
        super().__init__(f"{operation}失败：{status} {self.message}{suffix}".strip())


def _agent_api_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }


def _response_text(response: Any) -> str:
    try:
        value = response.text
    except Exception:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _response_content_type(response: Any) -> str:
    try:
        headers = response.headers
        if headers:
            return str(headers.get("content-type") or headers.get("Content-Type") or "").strip()
    except Exception:
        pass
    return ""


def _response_preview(value: str) -> str:
    # Responses should never echo credentials, but redact defensively before
    # surfacing the upstream text in the registration log.
    compact = " ".join(str(value or "").replace("\x00", "").split())
    compact = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", compact)
    return compact[:_RESPONSE_PREVIEW_LIMIT]


def _response_json(response: Any, operation: str) -> dict[str, Any]:
    raw = _response_text(response)
    try:
        payload = json.loads(raw.lstrip("\ufeff \t\r\n"))
    except (TypeError, ValueError) as exc:
        content_type = _response_content_type(response) or "未提供"
        preview = _response_preview(raw) or "响应为空"
        raise AgentIdentityUpstreamError(
            operation,
            status_code=int(getattr(response, "status_code", 0) or 0),
            message=f"上游响应不是 JSON（Content-Type: {content_type}）：{preview}",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentIdentityUpstreamError(
            operation,
            status_code=int(getattr(response, "status_code", 0) or 0),
            message="上游 JSON 响应不是对象",
        )
    return payload


def _upstream_error(response: Any, operation: str) -> AgentIdentityUpstreamError:
    raw = _response_text(response)
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(raw.lstrip("\ufeff \t\r\n"))
        if isinstance(parsed, dict):
            payload = parsed
    except (TypeError, ValueError):
        pass

    error = payload.get("error")
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("error") or "").strip()
        error_code = str(error.get("code") or "").strip()
    else:
        message = str(payload.get("message") or error or "").strip()
        error_code = str(payload.get("code") or "").strip()

    lowered = f"{error_code} {message}".lower()
    if "token_revoked" in lowered or "invalidated oauth token" in lowered:
        message = "ChatGPT Session 已被上游撤销，无法继续注册 Agent Identity"
        error_code = "token_revoked"
    elif not message:
        message = _response_preview(raw) or "上游未返回错误说明"

    return AgentIdentityUpstreamError(
        operation,
        status_code=int(getattr(response, "status_code", 0) or 0),
        message=message,
        error_code=error_code,
    )


def _generate_ed25519_keypair() -> tuple[str, str]:
    """Generate the PKCS#8 Ed25519 key expected by Codex and Sub2API."""
    private_key = Ed25519PrivateKey.generate()
    pkcs8_der = private_key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ssh_header = b"ssh-ed25519"
    blob = len(ssh_header).to_bytes(4, "big") + ssh_header + len(public_bytes).to_bytes(4, "big") + public_bytes
    return base64.b64encode(pkcs8_der).decode("ascii"), f"ssh-ed25519 {base64.b64encode(blob).decode('ascii')}"


def _proxy_map(proxy: str | None) -> dict[str, str] | None:
    value = str(proxy or "").strip()
    return {"http": value, "https": value} if value else None


def _register_agent(access_token: str, public_key_ssh: str, proxy: str | None = None) -> str:
    response = curl_requests.post(
        f"{AUTHAPI_BASE}/v1/agent/register",
        headers=_agent_api_headers(access_token),
        json={
            "abom": {
                "agent_version": AGENT_VERSION,
                "agent_harness_id": AGENT_HARNESS_ID,
                "running_location": RUNNING_LOCATION,
            },
            "agent_public_key": public_key_ssh,
        },
        proxies=_proxy_map(proxy),
        impersonate="chrome",
        timeout=AGENT_REQUEST_TIMEOUT,
    )
    if not 200 <= response.status_code < 300:
        raise _upstream_error(response, "Codex Agent 注册")
    payload = _response_json(response, "Codex Agent 注册")
    runtime_id = str(payload.get("agent_runtime_id") or "").strip()
    if not runtime_id:
        raise AgentIdentityUpstreamError(
            "Codex Agent 注册",
            status_code=response.status_code,
            message="上游响应缺少 agent_runtime_id",
        )
    return runtime_id


def _register_task(access_token: str, runtime_id: str, private_key_b64: str, proxy: str | None = None) -> str:
    private_key = load_der_private_key(base64.b64decode(private_key_b64), password=None)
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    signature = private_key.sign(f"{runtime_id}:{timestamp}".encode())
    response = curl_requests.post(
        f"{AUTHAPI_BASE}/v1/agent/{runtime_id}/task/register",
        headers=_agent_api_headers(access_token),
        json={"timestamp": timestamp, "signature": base64.b64encode(signature).decode("ascii")},
        proxies=_proxy_map(proxy),
        impersonate="chrome",
        timeout=AGENT_REQUEST_TIMEOUT,
    )
    if not 200 <= response.status_code < 300:
        raise _upstream_error(response, "Codex Agent task 注册")
    payload = _response_json(response, "Codex Agent task 注册")
    task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
    if task_id:
        return task_id
    encrypted = str(payload.get("encrypted_task_id") or payload.get("encryptedTaskId") or "").strip()
    if not encrypted:
        raise RuntimeError("Task 注册响应缺少 task_id")
    try:
        ed25519_key = load_der_private_key(base64.b64decode(private_key_b64), password=None)
        if not isinstance(ed25519_key, Ed25519PrivateKey):
            raise TypeError("private key is not Ed25519")
        seed = ed25519_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        signing_key = seed + ed25519_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        curve_private = Curve25519PrivateKey(crypto_sign_ed25519_sk_to_curve25519(signing_key))
        decrypted = SealedBox(curve_private).decrypt(base64.b64decode(encrypted)).decode("utf-8").strip()
    except Exception as exc:
        raise RuntimeError(f"Task ID 解密失败：{exc}") from exc
    if not decrypted:
        raise RuntimeError("Task ID 解密后为空")
    return decrypted


def _parse_session(session_data: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized user identity from a ChatGPT session."""
    user = session_data.get("user") if isinstance(session_data.get("user"), dict) else session_data
    access_token = str(session_data.get("accessToken") or "")
    user_id = str(user.get("id") or "")
    email = str(user.get("email") or session_data.get("email") or "").strip().lower()
    name = str(user.get("name") or "")
    session_account = session_data.get("account") if isinstance(session_data.get("account"), dict) else {}
    plan_type = str(session_data.get("plan_type") or session_account.get("planType") or "free")

    # Parse JWT claims from access token if available
    claims: dict[str, Any] = {}
    if access_token and "." in access_token:
        try:
            parts = access_token.split(".")
            if len(parts) >= 2:
                padded = parts[1] + "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except Exception:
            pass

    # Extract from claims if session user is incomplete
    if not user_id:
        user_id = str(
            claims.get("sub")
            or claims.get("user_id")
            or claims.get("https://api.openai.com/profile/user_id")
            or ""
        )
    if not email:
        email = str(
            claims.get("email")
            or claims.get("https://api.openai.com/profile/email")
            or ""
        ).strip().lower()
    auth_claims = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    account_id = str(
        session_data.get("accountId")
        or session_account.get("id")
        or auth_claims.get("chatgpt_account_id")
        or ""
    )
    plan_type = str(auth_claims.get("chatgpt_plan_type") or plan_type or "free")

    expires_str = str(
        session_data.get("expires")
        or claims.get("exp")
        or ""
    )
    expires_at = None
    if expires_str:
        try:
            if expires_str.isdigit() and len(expires_str) >= 10:
                expires_at = datetime.fromtimestamp(
                    int(expires_str) if len(expires_str) <= 10 else int(expires_str) / 1000,
                    tz=timezone.utc,
                ).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
            else:
                expires_at = expires_str
        except Exception:
            pass

    return {
        "access_token": access_token,
        "user_id": user_id,
        "account_id": account_id,
        "email": email,
        "name": name,
        "plan_type": plan_type,
        "expires_at": expires_at,
        "claims": claims,
    }


def _create_agent_identity(
    parsed: dict[str, Any],
    *,
    account_id: str | None = None,
    agent_runtime_id: str | None = None,
    agent_private_key: str | None = None,
    proxy: str | None = None,
    verify_task: bool = True,
) -> dict[str, Any]:
    """Build a complete agent_identity auth.json entry.

    A task is required for Agent Identity requests forwarded by Sub2API.  The
    task registration must happen while the ChatGPT session token is still
    available; an exported identity alone cannot perform that registration.
    """
    email = parsed["email"]
    user_id = parsed["user_id"]

    account_id = str(account_id or parsed.get("account_id") or "").strip()
    if not account_id or not user_id:
        raise RuntimeError("Session JWT 缺少 account_id 或 chatgpt_user_id")

    supplied_runtime_id = str(agent_runtime_id or "").strip()
    supplied_private_key = str(agent_private_key or "").strip()
    if bool(supplied_runtime_id) != bool(supplied_private_key):
        raise RuntimeError("复用 Agent Identity 时必须同时提供 agent_runtime_id 和 agent_private_key")

    access_token = str(parsed.get("access_token") or "").strip()
    if supplied_runtime_id:
        priv_key = supplied_private_key
        runtime_id = supplied_runtime_id
    else:
        if not access_token:
            raise RuntimeError("Session 缺少 accessToken，无法注册 Codex Agent")
        priv_key, public_key_ssh = _generate_ed25519_keypair()
        runtime_id = _register_agent(access_token, public_key_ssh, proxy)

    task_id = ""
    if verify_task:
        if not access_token:
            raise RuntimeError("Session 缺少 accessToken，无法注册 Agent task")
        task_id = _register_task(access_token, runtime_id, priv_key, proxy)
        if not task_id:
            raise RuntimeError("Agent task 注册未返回 task_id")

    return {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": runtime_id,
            "agent_private_key": priv_key,
            "task_id": task_id,
            "account_id": account_id,
            "chatgpt_user_id": user_id or "",
            "email": email,
            "plan_type": parsed.get("plan_type", "free"),
            "chatgpt_account_is_fedramp": False,
        },
    }


def session_to_auth_entry(
    session_data: dict[str, Any],
    *,
    account_id: str | None = None,
    agent_runtime_id: str | None = None,
    agent_private_key: str | None = None,
    proxy: str | None = None,
    verify_task: bool = True,
) -> dict[str, Any]:
    """Convert ChatGPT session JSON to a complete auth.json entry dict.

    By default task registration is mandatory, so the result can be imported
    by Sub2API without relying on a discarded ChatGPT session token.
    """
    parsed = _parse_session(session_data)
    entry = _create_agent_identity(
        parsed,
        account_id=account_id,
        agent_runtime_id=agent_runtime_id,
        agent_private_key=agent_private_key,
        proxy=proxy,
        verify_task=verify_task,
    )

    agent_id = entry["agent_identity"]["agent_runtime_id"]
    # Build a unique key that plays nicely with multi-account merge
    auth_key = f"{AGENT_IDENTITY_KEY_PREFIX}::{agent_id}"

    return {auth_key: entry}


def write_auth_json(
    path: Path,
    session_data: dict[str, Any],
    *,
    merge: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Write/merge auth.json from session data.

    Returns a dict with ok, file, entry summary.
    """
    entry_dict = session_to_auth_entry(session_data, **kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)

    if merge and path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
        existing.update(entry_dict)
        merged = existing
    else:
        merged = entry_dict

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)

    # Build summary
    first_entry = next(iter(entry_dict.values()), {})
    agent_id = first_entry.get("agent_identity", {})
    return {
        "ok": True,
        "file": str(path),
        "email": agent_id.get("email", ""),
        "agent_runtime_id": agent_id.get("agent_runtime_id", ""),
        "account_id": agent_id.get("account_id", ""),
        "chatgpt_user_id": agent_id.get("chatgpt_user_id", ""),
    }


def load_session_file(path: str | Path) -> dict[str, Any]:
    """Load session JSON from a file path."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"session file {path} does not contain a JSON object")
    return data


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="ChatGPT session JSON → auth.json (agent_identity format)"
    )
    ap.add_argument(
        "--session-file", metavar="FILE",
        help="Path to a session.json file (output of GET chatgpt.com/api/auth/session)",
    )
    ap.add_argument(
        "--session", metavar="JSON",
        help="Inline session JSON string",
    )
    ap.add_argument(
        "--out", default=None,
        help=f"Output auth.json path (default: {DEFAULT_AUTH_FILE})",
    )
    ap.add_argument(
        "--account-id", default=None,
        help="Custom account_id (UUID format; auto-generated if omitted)",
    )
    ap.add_argument(
        "--agent-runtime-id", default=None,
        help="Custom agent_runtime_id (auto-generated if omitted)",
    )
    ap.add_argument(
        "--agent-private-key", default=None,
        help="Custom agent_private_key (auto-generated if omitted)",
    )
    ap.add_argument(
        "--no-merge", dest="merge", action="store_false", default=True,
        help="Do not merge into existing auth.json; overwrite",
    )
    args = ap.parse_args()

    # Load session data
    if args.session:
        try:
            session_data = json.loads(args.session)
        except json.JSONDecodeError as e:
            print(f"❌ Invalid session JSON: {e}", file=sys.stderr)
            return 1
    elif args.session_file:
        try:
            session_data = load_session_file(args.session_file)
        except Exception as e:
            print(f"❌ Failed to load session file: {e}", file=sys.stderr)
            return 1
    else:
        ap.error("需要 --session 或 --session-file 参数")

    if not isinstance(session_data, dict):
        print("❌ Session data must be a JSON object", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else DEFAULT_AUTH_FILE

    result = write_auth_json(
        out_path,
        session_data,
        merge=args.merge,
        account_id=args.account_id,
        agent_runtime_id=args.agent_runtime_id,
        agent_private_key=args.agent_private_key,
    )

    if result.get("ok"):
        print(f"✅ auth.json written: {result['file']}")
        print(f"   email:          {result.get('email')}")
        print(f"   agent_runtime:  {result.get('agent_runtime_id')}")
        print(f"   account_id:     {result.get('account_id')}")
        print(f"   chatgpt_user:   {result.get('chatgpt_user_id')}")
        return 0
    else:
        print(f"❌ Failed: {result.get('error', 'unknown error')}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
