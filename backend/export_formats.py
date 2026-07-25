"""Account export builders for CPA and Sub2API."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CPA_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
GROK_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_SCOPE = "openid profile email offline_access grok-cli:access api:access"
GROK_REDIRECT_URI = "http://127.0.0.1:56121/callback"
CPA_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-pager",
    "User-Agent": "grok-pager/0.2.93 grok-shell/0.2.93 (linux; x86_64)",
}


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _unix_timestamp(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError):
        return None


def _expires_at(record: dict[str, Any]) -> int:
    expires_at = _unix_timestamp(record.get("expires_at") or record.get("expired"))
    if expires_at:
        return expires_at
    claims = _jwt_payload(str(record.get("access_token") or record.get("key") or ""))
    try:
        if claims.get("exp"):
            return int(claims["exp"])
    except (TypeError, ValueError):
        pass
    try:
        return int(time.time()) + int(record.get("expires_in") or 21600)
    except (TypeError, ValueError):
        return int(time.time()) + 21600


def _iso_utc(timestamp: int | float | None) -> str:
    if not timestamp:
        return ""
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def safe_email_filename(email: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in email)
    return safe or "unknown"


def build_cpa_record(
    record: dict[str, Any], *, source_file: str = "progrok-registration"
) -> dict[str, Any]:
    access_token = str(record.get("access_token") or record.get("key") or "")
    id_token = str(record.get("id_token") or "")
    claims = _jwt_payload(id_token) or _jwt_payload(access_token)
    email = str(record.get("email") or claims.get("email") or "").strip().lower()
    sub = str(record.get("sub") or claims.get("sub") or claims.get("principal_id") or "")
    return {
        "type": "xai",
        "auth_kind": "oauth",
        "access_token": access_token,
        "refresh_token": str(record.get("refresh_token") or ""),
        "id_token": id_token,
        "token_type": str(record.get("token_type") or "Bearer"),
        "expired": _iso_utc(_expires_at(record)),
        "last_refresh": str(record.get("last_refresh") or _iso_utc(int(time.time()))),
        "email": email,
        "sub": sub,
        "base_url": str(record.get("base_url") or CPA_BASE_URL),
        "token_endpoint": str(record.get("token_endpoint") or CPA_TOKEN_ENDPOINT),
        "redirect_uri": str(record.get("redirect_uri") or GROK_REDIRECT_URI),
        "disabled": bool(record.get("disabled", False)),
        "headers": dict(record.get("headers") or CPA_HEADERS),
        "sso": str(record.get("sso") or ""),
        "password": str(record.get("password") or ""),
        "_source": "grok-register-auto-cpa",
        "_source_file": str(record.get("_source_file") or source_file),
    }


def cpa_filename(record: dict[str, Any]) -> str:
    return f"xai-{safe_email_filename(str(record.get('email') or ''))}.json"


def build_sub2api_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    for source in records:
        cpa = build_cpa_record(source)
        credentials: dict[str, Any] = {
            "access_token": cpa["access_token"],
            "refresh_token": cpa["refresh_token"],
            "token_type": cpa["token_type"],
            "client_id": str(source.get("client_id") or source.get("oidc_client_id") or GROK_CLIENT_ID),
            "scope": str(source.get("scope") or GROK_SCOPE),
            "email": cpa["email"],
            "sub": cpa["sub"],
            "expires_at": cpa["expired"],
            "base_url": cpa["base_url"],
        }
        if cpa["id_token"]:
            credentials["id_token"] = cpa["id_token"]
        accounts.append(
            {
                "name": cpa["email"] or "Grok OAuth Account",
                "platform": "grok",
                "type": "oauth",
                "credentials": credentials,
                "extra": {
                    "sso": cpa["sso"],
                    "password": cpa["password"],
                },
                "concurrency": 3,
                "priority": 50,
                "rate_multiplier": 1.0,
                "auto_pause_on_expired": True,
            }
        )
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _iso_utc(int(time.time())),
        "proxies": [],
        "accounts": accounts,
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# ChatGPT / agent_identity export
# --------------------------------------------------------------------------- #
def build_chatgpt_export_record(record: dict[str, Any]) -> dict[str, Any]:
    """Build a ChatGPT agent_identity export record."""
    agent_id = record.get("agent_identity") if isinstance(record.get("agent_identity"), dict) else record
    return {
        "type": "chatgpt",
        "auth_kind": "agent_identity",
        "agent_runtime_id": str(agent_id.get("agent_runtime_id") or ""),
        "agent_private_key": str(agent_id.get("agent_private_key") or ""),
        "task_id": str(agent_id.get("task_id") or ""),
        "account_id": str(agent_id.get("account_id") or ""),
        "chatgpt_user_id": str(agent_id.get("chatgpt_user_id") or ""),
        "email": str(agent_id.get("email") or record.get("email") or "").strip().lower(),
        "plan_type": str(agent_id.get("plan_type") or "free"),
        "chatgpt_account_is_fedramp": bool(agent_id.get("chatgpt_account_is_fedramp") or False),
        "access_token": str(record.get("access_token") or ""),
        "_source": "chatgpt-register-auto",
    }


def _validate_chatgpt_agent_identity(export: dict[str, Any]) -> None:
    required = (
        "agent_runtime_id",
        "agent_private_key",
        "task_id",
        "account_id",
        "chatgpt_user_id",
    )
    missing = [field for field in required if not str(export.get(field) or "").strip()]
    if missing:
        email = str(export.get("email") or "未知账号")
        raise ValueError(f"ChatGPT 账号 {email} 缺少 {', '.join(missing)}，无法导出 Agent Identity")


def build_cpa_chatgpt_agent_identity_record(record: dict[str, Any]) -> dict[str, Any]:
    """Build the Codex Agent Identity JSON accepted by CPA."""
    export = build_chatgpt_export_record(record)
    _validate_chatgpt_agent_identity(export)
    identity = {
        "agent_runtime_id": export["agent_runtime_id"],
        "agent_private_key": export["agent_private_key"],
        "task_id": export["task_id"],
        "account_id": export["account_id"],
        "chatgpt_user_id": export["chatgpt_user_id"],
        "email": export["email"],
        "plan_type": export["plan_type"],
        "chatgpt_account_is_fedramp": export["chatgpt_account_is_fedramp"],
    }
    return {
        "type": "codex",
        "auth_kind": "agent_identity",
        "auth_mode": "agent_identity",
        "agent_identity": identity,
        **identity,
        "_source": "chatgpt-register-auto-cpa",
    }


def cpa_chatgpt_agent_identity_filename(record: dict[str, Any]) -> str:
    runtime_id = str(record.get("agent_runtime_id") or "").strip()
    if not runtime_id:
        raise ValueError("ChatGPT Agent Identity 缺少 agent_runtime_id，无法生成 CPA 文件名")
    digest = hashlib.sha256(runtime_id.encode("utf-8")).hexdigest()[:32]
    return f"codex-agent-{digest}.json"


def build_chatgpt_auth_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a merged Agent Identity auth.json payload."""
    payload: dict[str, Any] = {}
    for source in records:
        export = build_chatgpt_export_record(source)
        _validate_chatgpt_agent_identity(export)
        runtime_id = export["agent_runtime_id"]
        payload[f"chatgpt-agent::{runtime_id}"] = {
            "auth_mode": "agent_identity",
            "agent_identity": {
                "agent_runtime_id": runtime_id,
                "agent_private_key": export["agent_private_key"],
                "task_id": export["task_id"],
                "account_id": export["account_id"],
                "chatgpt_user_id": export["chatgpt_user_id"],
                "email": export["email"],
                "plan_type": export["plan_type"],
                "chatgpt_account_is_fedramp": export["chatgpt_account_is_fedramp"],
            },
            "email": export["email"],
        }
    return payload


def build_chatgpt_sub2api_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Sub2API-compatible payload from ChatGPT agent_identity records."""
    accounts: list[dict[str, Any]] = []
    for source in records:
        export = build_chatgpt_export_record(source)
        _validate_chatgpt_agent_identity(export)
        accounts.append({
            "name": export["email"] or f"ChatGPT {export['agent_runtime_id'][:8]}",
            "platform": "chatgpt",
            "type": "agent_identity",
            "credentials": {
                "agent_runtime_id": export["agent_runtime_id"],
                "agent_private_key": export["agent_private_key"],
                "task_id": export["task_id"],
                "account_id": export["account_id"],
                "chatgpt_user_id": export["chatgpt_user_id"],
                "email": export["email"],
                "plan_type": export["plan_type"],
            },
            "concurrency": 3,
            "priority": 50,
            "rate_multiplier": 1.0,
            "auto_pause_on_expired": False,
        })
    return {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": _iso_utc(int(time.time())),
        "proxies": [],
        "accounts": accounts,
    }
