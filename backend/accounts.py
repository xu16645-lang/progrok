"""Filesystem-backed account sink used by the standalone registration adapter."""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from export_formats import (
    build_cpa_chatgpt_agent_identity_record,
    build_cpa_record,
    build_sub2api_payload,
    cpa_chatgpt_agent_identity_filename,
    cpa_filename,
)

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "runtime" / "data"
ACCOUNTS_DIR = DATA_DIR / "accounts"
MERGED_AUTH = DATA_DIR / "auth.json"
_lock = threading.RLock()


def _jwt_payload(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part).decode("utf-8"))
    except Exception:
        return {}


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value[:100] or "account"


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _xai_identity(record: dict[str, Any]) -> tuple[set[str], str]:
    identifiers = {
        str(record.get(name) or "").strip()
        for name in ("sub", "user_id", "principal_id", "account_id")
    }
    identifiers.discard("")
    return identifiers, str(record.get("email") or "").strip().lower()


def _is_xai_oidc_entry(entry: dict[str, Any]) -> bool:
    if str(entry.get("type") or "").strip().lower() == "xai":
        return True
    issuer = str(entry.get("oidc_issuer") or "").strip().lower()
    auth_mode = str(entry.get("auth_mode") or "").strip().lower()
    return auth_mode in {"oidc", "oauth"} and issuer.endswith("x.ai")


def _matches_xai_record(entry: dict[str, Any], identifiers: set[str], email: str) -> bool:
    if not _is_xai_oidc_entry(entry):
        return False
    entry_ids = {
        str(entry.get(name) or "").strip()
        for name in ("sub", "user_id", "principal_id", "account_id")
    }
    entry_ids.discard("")
    if identifiers and identifiers.intersection(entry_ids):
        return True
    return bool(email and str(entry.get("email") or "").strip().lower() == email)


def _apply_xai_oauth_refresh(entry: dict[str, Any], refreshed: dict[str, Any]) -> None:
    access_token = str(refreshed.get("access_token") or "").strip()
    refresh_token = str(refreshed.get("refresh_token") or "").strip()
    expires_at = str(refreshed.get("expires_at") or "").strip()
    refreshed_at = str(refreshed.get("refreshed_at") or "").strip()

    if not access_token:
        raise ValueError("刷新结果缺少 access_token")
    if "key" in entry and "access_token" not in entry:
        entry["key"] = access_token
    else:
        entry["access_token"] = access_token
    if refresh_token:
        entry["refresh_token"] = refresh_token
    if expires_at:
        entry["expires_at"] = expires_at
        if "expired" in entry or str(entry.get("type") or "").lower() == "xai":
            entry["expired"] = expires_at
    if refreshed_at:
        entry["last_refresh"] = refreshed_at
    for field in ("id_token", "token_type", "scope", "client_id"):
        value = str(refreshed.get(field) or "").strip()
        if value:
            entry[field] = value


def _safe_account_source_path(raw_path: Any) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    try:
        path = Path(text).resolve()
        path.relative_to(ACCOUNTS_DIR.resolve())
        return path
    except (OSError, ValueError):
        return None


def persist_xai_oauth_refresh(record: dict[str, Any], refreshed: dict[str, Any]) -> dict[str, Any]:
    """Durably store a refreshed xAI OAuth credential without exposing tokens."""
    identifiers, email = _xai_identity(record)
    source_path = _safe_account_source_path(record.get("_local_account_path"))
    source_requested = bool(record.get("_local_account_path"))
    source_auth_key = str(record.get("_local_auth_key") or "").strip()
    updated_files = 0
    updated_auth_entries = 0

    with _lock:
        if source_requested and source_path is None:
            return {"ok": False, "error": "本地 xAI 凭证文件路径无效"}

        candidates = [source_path] if source_path else list(ACCOUNTS_DIR.glob("*.json"))
        for path in candidates:
            if path is None or not path.exists():
                if source_path:
                    return {"ok": False, "error": "本地 xAI 凭证文件不存在"}
                continue
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                if source_path:
                    return {"ok": False, "error": f"读取本地 xAI 凭证文件失败：{exc}"}
                continue
            if not isinstance(entry, dict) or not _matches_xai_record(entry, identifiers, email):
                if source_path:
                    return {"ok": False, "error": "本地 xAI 凭证文件与待刷新账号不匹配"}
                continue
            try:
                _apply_xai_oauth_refresh(entry, refreshed)
                _atomic_json(path, entry)
                updated_files += 1
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"保存本地 xAI 凭证失败：{exc}"}

        merged: dict[str, Any] = {}
        if MERGED_AUTH.exists():
            try:
                loaded = json.loads(MERGED_AUTH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged = loaded
            except (OSError, ValueError) as exc:
                return {"ok": False, "error": f"读取合并 auth.json 失败：{exc}"}
        for auth_key, entry in merged.items():
            if not isinstance(entry, dict):
                continue
            if source_auth_key and auth_key != source_auth_key and not _matches_xai_record(entry, identifiers, email):
                continue
            if not source_auth_key and not _matches_xai_record(entry, identifiers, email):
                continue
            try:
                _apply_xai_oauth_refresh(entry, refreshed)
                updated_auth_entries += 1
            except ValueError as exc:
                return {"ok": False, "error": f"更新合并 auth.json 失败：{exc}"}
        if updated_auth_entries:
            try:
                _atomic_json(MERGED_AUTH, merged)
            except OSError as exc:
                return {"ok": False, "error": f"保存合并 auth.json 失败：{exc}"}

    return {
        "ok": True,
        "persisted": bool(updated_files or updated_auth_entries),
        "updated_files": updated_files,
        "updated_auth_entries": updated_auth_entries,
    }


def _chatgpt_agent_identity(entry: dict[str, Any]) -> dict[str, Any] | None:
    identity = entry.get("agent_identity") if isinstance(entry, dict) else None
    if not isinstance(identity, dict):
        return None
    runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    private_key = str(identity.get("agent_private_key") or "").strip()
    if not runtime_id or not private_key:
        return None
    return identity


def load_chatgpt_agent_identity(
    *, runtime_id: str = "", email: str = ""
) -> dict[str, Any]:
    """Load one locally persisted Agent Identity without exposing it through APIs."""
    target_runtime_id = str(runtime_id or "").strip()
    target_email = str(email or "").strip().lower()
    with _lock:
        try:
            merged = json.loads(MERGED_AUTH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("未找到本地 ChatGPT Agent Identity 认证文件") from exc
        if not isinstance(merged, dict):
            raise RuntimeError("本地 ChatGPT Agent Identity 认证文件格式无效")
        for auth_key, entry in merged.items():
            if not isinstance(entry, dict):
                continue
            identity = _chatgpt_agent_identity(entry)
            if identity is None:
                continue
            identity_runtime_id = str(identity.get("agent_runtime_id") or "").strip()
            identity_email = str(
                identity.get("email") or entry.get("email") or ""
            ).strip().lower()
            if target_runtime_id and identity_runtime_id != target_runtime_id:
                continue
            if not target_runtime_id and target_email and identity_email != target_email:
                continue
            if not target_runtime_id and not target_email:
                continue
            result = dict(identity)
            result["_local_auth_key"] = str(auth_key)
            return result
    raise RuntimeError("未找到匹配的本地 ChatGPT Agent Identity 认证记录")


def _update_chatgpt_agent_task_in_payload(
    payload: dict[str, Any], runtime_id: str, task_id: str
) -> bool:
    changed = False
    identity = payload.get("agent_identity")
    if isinstance(identity, dict) and str(identity.get("agent_runtime_id") or "").strip() == runtime_id:
        identity["task_id"] = task_id
        changed = True
    if str(payload.get("agent_runtime_id") or "").strip() == runtime_id:
        payload["task_id"] = task_id
        changed = True
    accounts = payload.get("accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if not isinstance(account, dict):
                continue
            credentials = account.get("credentials")
            if (
                isinstance(credentials, dict)
                and str(credentials.get("agent_runtime_id") or "").strip() == runtime_id
            ):
                credentials["task_id"] = task_id
                changed = True
    return changed


def persist_chatgpt_agent_task(identity: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Persist a recovered Agent Identity task ID in every local representation."""
    runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    new_task_id = str(task_id or "").strip()
    if not runtime_id or not new_task_id:
        return {"ok": False, "error": "Agent Identity runtime_id 或 task_id 为空"}

    updated_auth_entries = 0
    updated_files = 0
    with _lock:
        try:
            merged = json.loads(MERGED_AUTH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": f"读取本地 Agent Identity 认证文件失败：{exc}"}
        if not isinstance(merged, dict):
            return {"ok": False, "error": "本地 Agent Identity 认证文件格式无效"}

        for entry in merged.values():
            if not isinstance(entry, dict):
                continue
            if _update_chatgpt_agent_task_in_payload(entry, runtime_id, new_task_id):
                updated_auth_entries += 1
        if not updated_auth_entries:
            return {"ok": False, "error": "未找到需要更新的本地 Agent Identity 认证记录"}
        try:
            _atomic_json(MERGED_AUTH, merged)
        except OSError as exc:
            return {"ok": False, "error": f"保存本地 Agent Identity 认证文件失败：{exc}"}

        for path in ACCOUNTS_DIR.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if not _update_chatgpt_agent_task_in_payload(payload, runtime_id, new_task_id):
                continue
            try:
                _atomic_json(path, payload)
                updated_files += 1
            except OSError as exc:
                return {"ok": False, "error": f"保存 Agent Identity 账号文件失败：{exc}"}

    return {
        "ok": True,
        "persisted": True,
        "updated_auth_entries": updated_auth_entries,
        "updated_files": updated_files,
    }


def import_auth_payload(
    payload: dict[str, Any],
    merge: bool = True,
    output_format: str = "cpa",
) -> dict[str, Any]:
    auth_mode = str(payload.get("auth_mode") or "oidc").strip().lower()

    # ---- ChatGPT agent_identity format ----
    if auth_mode == "agent_identity":
        return _import_chatgpt_agent_identity(payload, merge=merge, output_format=output_format)

    # ---- Standard OIDC format (Grok/xAI) ----
    access_token = str(payload.get("key") or "").strip()
    if not access_token:
        return {"ok": False, "error": "auth payload missing access token"}

    claims = _jwt_payload(access_token)
    account_id = str(claims.get("sub") or claims.get("principal_id") or uuid.uuid4())
    email = str(payload.get("email") or claims.get("email") or "").strip().lower()
    issuer = str(payload.get("oidc_issuer") or "https://auth.x.ai")
    client_id = str(payload.get("oidc_client_id") or "b1a00492-073a-47ea-816f-4c329264a828")
    entry = {
        "key": access_token,
        "auth_mode": str(payload.get("auth_mode") or "oidc"),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": payload.get("expires_at"),
        "user_id": account_id,
        "principal_id": str(claims.get("principal_id") or account_id),
        "principal_type": str(claims.get("principal_type") or "User"),
        "email": email,
        "oidc_issuer": issuer,
        "oidc_client_id": client_id,
    }
    auth_key = f"{issuer}::{client_id}"
    unique_key = f"{auth_key}::{account_id}"
    cpa_record = build_cpa_record(payload)
    normalized_format = str(output_format or "cpa").strip().lower()
    if normalized_format == "sub2api":
        output_record = build_sub2api_payload([cpa_record])
        filename = cpa_filename(cpa_record).replace("xai-", "sub2api-", 1)
    else:
        normalized_format = "cpa"
        output_record = cpa_record
        filename = cpa_filename(cpa_record)
    target = ACCOUNTS_DIR / filename

    with _lock:
        _atomic_json(target, output_record)
        merged: dict[str, Any] = {}
        if merge and MERGED_AUTH.exists():
            try:
                loaded = json.loads(MERGED_AUTH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged = loaded
            except Exception:
                merged = {}
        merged[unique_key] = entry
        _atomic_json(MERGED_AUTH, merged)

    row = {
        "id": account_id,
        "email": email,
        "path": str(target),
        "format": normalized_format,
    }
    return {
        "ok": True,
        "storage": "filesystem",
        "imported": [row],
        "auth_file": str(MERGED_AUTH),
    }


def _import_chatgpt_agent_identity(
    payload: dict[str, Any],
    merge: bool = True,
    output_format: str = "cpa",
) -> dict[str, Any]:
    """Import a ChatGPT agent_identity auth entry."""
    agent_id = payload.get("agent_identity") if isinstance(payload.get("agent_identity"), dict) else payload
    agent_runtime_id = str(agent_id.get("agent_runtime_id") or payload.get("key") or "").strip()
    agent_private_key = str(agent_id.get("agent_private_key") or "").strip()
    task_id = str(agent_id.get("task_id") or "").strip()
    email = str(agent_id.get("email") or payload.get("email") or "").strip().lower()
    chatgpt_user_id = str(agent_id.get("chatgpt_user_id") or "").strip()
    account_id = str(agent_id.get("account_id") or "").strip()

    required = {
        "agent_runtime_id": agent_runtime_id,
        "agent_private_key": agent_private_key,
        "task_id": task_id,
        "account_id": account_id,
        "chatgpt_user_id": chatgpt_user_id,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        return {
            "ok": False,
            "error": f"Agent Identity 缺少必要字段：{', '.join(missing)}；未保存或导入站点",
        }

    # Build auth.json entry
    entry = {
        "auth_mode": "agent_identity",
        "agent_identity": {
            "agent_runtime_id": agent_runtime_id,
            "agent_private_key": agent_private_key,
            "task_id": task_id,
            "account_id": account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "email": email,
            "plan_type": str(agent_id.get("plan_type") or "free"),
            "chatgpt_account_is_fedramp": bool(agent_id.get("chatgpt_account_is_fedramp") or False),
        },
        "email": email,
    }

    unique_key = f"chatgpt-agent::{agent_runtime_id}"

    # Build export record
    normalized_format = str(output_format or "cpa").strip().lower()
    safe_email = _safe_name(email or f"chatgpt-{agent_runtime_id[:8]}")

    if normalized_format == "sub2api":
        output_record = {
            "type": "sub2api-data",
            "version": 1,
            "accounts": [{
                "name": email or f"ChatGPT {agent_runtime_id[:8]}",
                "platform": "chatgpt",
                "type": "agent_identity",
                "credentials": {
                    "agent_runtime_id": agent_runtime_id,
                    "agent_private_key": agent_private_key,
                    "task_id": task_id,
                    "account_id": account_id,
                    "chatgpt_user_id": chatgpt_user_id,
                    "email": email,
                    "plan_type": str(agent_id.get("plan_type") or "free"),
                },
                "concurrency": 3,
                "priority": 50,
                "rate_multiplier": 1.0,
                "auto_pause_on_expired": False,
            }],
        }
        filename = f"chatgpt-{safe_email}.json"
    else:
        output_record = build_cpa_chatgpt_agent_identity_record(entry)
        filename = cpa_chatgpt_agent_identity_filename(output_record)

    target = ACCOUNTS_DIR / filename

    with _lock:
        _atomic_json(target, output_record)
        merged: dict[str, Any] = {}
        if merge and MERGED_AUTH.exists():
            try:
                loaded = json.loads(MERGED_AUTH.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    merged = loaded
            except Exception:
                merged = {}
        merged[unique_key] = entry
        _atomic_json(MERGED_AUTH, merged)

    row = {
        "id": agent_runtime_id,
        "email": email,
        "path": str(target),
        "format": normalized_format,
    }
    return {
        "ok": True,
        "storage": "filesystem",
        "imported": [row],
        "auth_file": str(MERGED_AUTH),
    }
