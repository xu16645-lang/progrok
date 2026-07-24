"""Sub2API authentication, probe, group, and import protocols."""
from __future__ import annotations

import json
from typing import Any

import httpx

from export_formats import build_sub2api_payload
from .common import (
    CHATGPT_SUB2API_MODELS,
    error_text as _error_text,
)


def _sub2api_auth_headers(
    http: httpx.Client,
    *,
    base: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    base = str(base or "").strip().rstrip("/")
    key = str(api_key or "").strip()
    mode = str(auth_mode or "password").strip().lower()
    email = str(admin_email or "").strip()
    password = str(admin_password or "")
    if not base:
        return None, {"ok": False, "target": "sub2api", "error": "Sub2API 地址未填写"}
    if mode == "api_key" and not key:
        return None, {"ok": False, "target": "sub2api", "error": "Sub2API 管理员 API Key 未填写"}
    if mode == "password" and (not email or not password):
        return None, {"ok": False, "target": "sub2api", "error": "Sub2API 管理员邮箱或密码未填写"}
    if mode not in {"password", "api_key"}:
        return None, {"ok": False, "target": "sub2api", "error": "不支持的 Sub2API 认证方式"}
    if mode == "api_key":
        return {"x-api-key": key}, None
    login_response = http.post(
        f"{base}/api/v1/auth/login",
        headers={"Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    if login_response.status_code >= 300:
        return None, {
            "ok": False,
            "target": "sub2api",
            "status_code": login_response.status_code,
            "error": f"Sub2API 登录失败：{_error_text(login_response)}",
        }
    try:
        login_payload = login_response.json()
    except Exception:
        login_payload = {}
    login_data = (
        login_payload.get("data")
        if isinstance(login_payload, dict) and isinstance(login_payload.get("data"), dict)
        else login_payload
    )
    if isinstance(login_data, dict) and login_data.get("requires_2fa"):
        return None, {
            "ok": False,
            "target": "sub2api",
            "error": "Sub2API 管理员账号启用了二次验证，请改用管理员 API Key",
        }
    access_token = (
        str(login_data.get("access_token") or "").strip()
        if isinstance(login_data, dict)
        else ""
    )
    if not access_token:
        return None, {
            "ok": False,
            "target": "sub2api",
            "error": "Sub2API 登录响应中没有 access_token",
        }
    return {"Authorization": f"Bearer {access_token}"}, None


def list_sub2api_groups(
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = str(base_url or "").strip().rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        auth_headers, error = _sub2api_auth_headers(
            http,
            base=base,
            api_key=api_key,
            auth_mode=auth_mode,
            admin_email=admin_email,
            admin_password=admin_password,
        )
        if error:
            return error
        response = http.get(
            f"{base}/api/v1/admin/groups",
            headers=auth_headers,
            params={
                "page": 1,
                "page_size": 1000,
                "sort_by": "sort_order",
                "sort_order": "asc",
            },
        )
        if response.status_code >= 300:
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": _error_text(response)}
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            groups = data.get("items") or data.get("groups") or []
        else:
            groups = data
        normalized = []
        for item in groups if isinstance(groups, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                group_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            item_platform = str(item.get("platform") or "").lower()
            normalized.append({"id": group_id, "name": str(item.get("name") or f"分组 {group_id}"), "platform": item_platform})
        return {"ok": True, "target": "sub2api", "groups": normalized}
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "target": "sub2api", "error": f"Sub2API 分组获取失败：{str(exc)[:220]}"}
    finally:
        if owned:
            http.close()


def _chatgpt_agent_identity(record: dict[str, Any]) -> dict[str, Any] | None:
    identity = record.get("agent_identity")
    if isinstance(identity, dict):
        return identity
    if (
        record.get("type") == "chatgpt"
        or record.get("auth_kind") == "agent_identity"
        or record.get("auth_mode") == "agent_identity"
    ):
        return record
    return None


def _find_sub2api_account(
    http: httpx.Client,
    *,
    base: str,
    headers: dict[str, str],
    email: str,
    platform: str = "openai",
) -> dict[str, Any] | None:
    response = http.get(
        f"{base}/api/v1/admin/accounts",
        headers=headers,
        params={"page": 1, "page_size": 20, "platform": platform, "search": email},
        timeout=30.0,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"导入后账号查询失败：{_error_text(response)}")
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else payload
    items = data.get("items") if isinstance(data, dict) else data
    target = email.strip().lower()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        credentials = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
        item_email = str(credentials.get("email") or item.get("name") or "").strip().lower()
        if item_email == target:
            return item
    return None


def import_chatgpt_to_sub2api(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    group_id: int = 0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    identity = _chatgpt_agent_identity(record) or {}
    required = ("agent_runtime_id", "agent_private_key", "task_id", "account_id", "chatgpt_user_id")
    missing = [key for key in required if not str(identity.get(key) or "").strip()]
    if missing:
        return {"ok": False, "target": "sub2api", "error": f"Agent Identity 缺少字段：{', '.join(missing)}"}
    base = str(base_url or "").strip().rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        auth_headers, error = _sub2api_auth_headers(
            http,
            base=base,
            api_key=api_key,
            auth_mode=auth_mode,
            admin_email=admin_email,
            admin_password=admin_password,
        )
        if error:
            return error
        agent_identity = {
            "agent_runtime_id": str(identity.get("agent_runtime_id") or ""),
            "agent_private_key": str(identity.get("agent_private_key") or ""),
            "task_id": str(identity.get("task_id") or "").strip(),
            "account_id": str(identity.get("account_id") or ""),
            "chatgpt_user_id": str(identity.get("chatgpt_user_id") or ""),
            "email": str(identity.get("email") or record.get("email") or "").strip().lower(),
            "plan_type": str(identity.get("plan_type") or "free"),
            "chatgpt_account_is_fedramp": bool(identity.get("chatgpt_account_is_fedramp", False)),
        }
        selected_group_id = int(group_id or 0)
        response = http.post(
            f"{base}/api/v1/admin/accounts/import/codex-session",
            headers={**(auth_headers or {}), "Content-Type": "application/json"},
            json={
                "content": json.dumps({"auth_mode": "agentIdentity", "agent_identity": agent_identity}, ensure_ascii=False),
                "name": agent_identity["email"] or f"ChatGPT {agent_identity['agent_runtime_id'][:8]}",
                "group_ids": [selected_group_id] if selected_group_id else [],
                "concurrency": 1,
                "priority": 50,
                "rate_multiplier": 1.0,
                "auto_pause_on_expired": False,
                "update_existing": True,
                "skip_default_group_bind": bool(selected_group_id),
                "credential_extras": {
                    "model_mapping": {model: model for model in CHATGPT_SUB2API_MODELS}
                },
            },
        )
        if response.status_code >= 300:
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": _error_text(response)}
        payload = response.json()
        result = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        failed = int(result.get("failed") or 0) if isinstance(result, dict) else 0
        if failed:
            errors = result.get("errors") if isinstance(result, dict) else None
            detail = str(errors[0].get("message") or "") if isinstance(errors, list) and errors and isinstance(errors[0], dict) else ""
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": detail or f"Sub2API 导入失败账号数：{failed}"}
        imported = _find_sub2api_account(
            http,
            base=base,
            headers=auth_headers or {},
            email=agent_identity["email"],
        )
        if imported is None:
            return {"ok": False, "target": "sub2api", "error": "Sub2API 导入返回成功，但回查不到该账号"}
        imported_credentials = imported.get("credentials") if isinstance(imported.get("credentials"), dict) else {}
        persisted_task_id = str(imported_credentials.get("task_id") or "").strip()
        if not persisted_task_id:
            return {"ok": False, "target": "sub2api", "error": "Sub2API 导入后 task_id 未保存，Agent Identity 无法构建认证"}
        return {
            "ok": True,
            "target": "sub2api",
            "status_code": response.status_code,
            "created": int(result.get("created") or 0) if isinstance(result, dict) else 0,
            "updated": int(result.get("updated") or 0) if isinstance(result, dict) else 0,
            "group_id": selected_group_id or None,
            "account_id": imported.get("id"),
            "task_ready": True,
        }
    except (httpx.HTTPError, RuntimeError) as exc:
        return {"ok": False, "target": "sub2api", "error": f"网络错误：{str(exc)[:220]}"}
    finally:
        if owned:
            http.close()


def import_to_sub2api(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    group_id: int = 0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = str(base_url or "").strip().rstrip("/")
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        auth_headers, error = _sub2api_auth_headers(
            http,
            base=base,
            api_key=api_key,
            auth_mode=auth_mode,
            admin_email=admin_email,
            admin_password=admin_password,
        )
        if error:
            return error
        selected_group_id = int(group_id or 0)
        response = http.post(
            f"{base}/api/v1/admin/accounts/data",
            headers={**(auth_headers or {}), "Content-Type": "application/json"},
            json={
                "data": build_sub2api_payload([record]),
                "skip_default_group_bind": bool(selected_group_id),
            },
        )
        if response.status_code >= 300:
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": _error_text(response)}
        try:
            payload = response.json()
        except Exception:
            payload = {}
        result = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        failed = int(result.get("account_failed") or 0) if isinstance(result, dict) else 0
        if failed:
            return {"ok": False, "target": "sub2api", "status_code": response.status_code, "error": f"Sub2API 导入失败账号数：{failed}"}
        created_value = result.get("account_created") if isinstance(result, dict) else None
        created = int(created_value) if created_value is not None else 1
        account_id = None
        if selected_group_id:
            email = str(record.get("email") or "").strip().lower()
            if not email:
                return {
                    "ok": False,
                    "target": "sub2api",
                    "status_code": response.status_code,
                    "created": created,
                    "group_id": selected_group_id,
                    "error": "Sub2API 账号已导入，但缺少邮箱，无法绑定所选分组",
                }
            imported = _find_sub2api_account(
                http,
                base=base,
                headers=auth_headers or {},
                email=email,
                platform="grok",
            )
            if imported is None or not imported.get("id"):
                return {
                    "ok": False,
                    "target": "sub2api",
                    "status_code": response.status_code,
                    "created": created,
                    "group_id": selected_group_id,
                    "error": "Sub2API 账号已导入，但回查不到账号，无法绑定所选分组",
                }
            account_id = imported.get("id")
            bind_response = http.put(
                f"{base}/api/v1/admin/accounts/{account_id}",
                headers={**(auth_headers or {}), "Content-Type": "application/json"},
                json={"group_ids": [selected_group_id]},
            )
            if bind_response.status_code >= 300:
                return {
                    "ok": False,
                    "target": "sub2api",
                    "status_code": bind_response.status_code,
                    "created": created,
                    "account_id": account_id,
                    "group_id": selected_group_id,
                    "error": f"Sub2API 账号已导入，但分组绑定失败：{_error_text(bind_response)}",
                }
        return {
            "ok": True,
            "target": "sub2api",
            "status_code": response.status_code,
            "created": created,
            "group_id": selected_group_id or None,
            "account_id": account_id,
        }
    except (httpx.HTTPError, RuntimeError) as exc:
        return {"ok": False, "target": "sub2api", "error": f"网络错误：{str(exc)[:220]}"}
    finally:
        if owned:
            http.close()
