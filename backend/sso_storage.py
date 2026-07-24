"""Extracted SSO auth storage operations."""

from __future__ import annotations

from typing import Any

def token_to_auth_entry(ctx, token, email=""):
    """
    返回 (top_level_key, entry)
    top_level_key 固定为 issuer::client_id（与 ~/.grok/auth.json 一致）
    """
    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    payload = ctx.decode_jwt_payload(access)
    user_id = payload.get("sub") or payload.get("principal_id") or ""
    principal_id = payload.get("principal_id") or user_id
    principal_type = payload.get("principal_type") or "User"
    expires_in = int(token.get("expires_in") or 21600)
    if "exp" in payload:
        expires_at = ctx.rfc3339_ns(float(payload["exp"]))
    else:
        expires_at = ctx.rfc3339_ns(ctx.time.time() + expires_in)
    iat = payload.get("iat")
    create_time = ctx.rfc3339_ns(float(iat) if iat else ctx.time.time())
    entry = {
        "key": access,
        "auth_mode": "oidc",
        "create_time": create_time,
        "user_id": user_id,
        "email": email or payload.get("email") or "",
        "principal_type": principal_type,
        "principal_id": principal_id,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "oidc_issuer": ctx.OIDC_ISSUER,
        "oidc_client_id": ctx.GROK_CLI_CLIENT_ID,
    }
    return (ctx.AUTH_KEY, entry)


def write_auth_json(ctx, path, auth_key, entry):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {auth_key: entry}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        ctx.json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ctx.os.replace(tmp, path)


def merge_auth_json(ctx, path, auth_key, entry, unique=True):
    """
    合并写入。unique=True 时 key 变成 issuer::client_id::user_id，避免多账号互相覆盖。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = ctx.json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    key = auth_key
    if unique and entry.get("user_id"):
        key = f"{auth_key}::{entry['user_id']}"
    existing[key] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        ctx.json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    ctx.os.replace(tmp, path)


def import_into_project_auth(ctx, entry):
    """Use project's account manager to merge entry into AUTH_FILE."""
    import accounts as _accounts

    payload = {
        "key": entry["key"],
        "auth_mode": entry.get("auth_mode", "oidc"),
        "email": entry.get("email", ""),
        "refresh_token": entry.get("refresh_token", ""),
        "expires_at": entry.get("expires_at"),
        "oidc_issuer": entry.get("oidc_issuer", ctx.OIDC_ISSUER),
        "oidc_client_id": entry.get("oidc_client_id", ctx.GROK_CLI_CLIENT_ID),
    }
    result = _accounts.import_auth_payload(payload, merge=True)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "import failed")
    imported = result.get("imported", [])
    return imported[0] if imported else ""
