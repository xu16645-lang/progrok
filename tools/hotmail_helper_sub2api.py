"""Extracted mailbox-helper normalize_sub2api_base_url to import_agent_identity_to_sub2api operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class Sub2APIContext:
    """Resolve helper services from the live compatibility facade."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def normalize_sub2api_base_url(ctx: Sub2APIContext, raw_url):
    value = str(raw_url or "").strip().rstrip("/")
    if not value:
        raise RuntimeError("Missing SUB2API site URL")
    if not ctx.re.match("^https?://", value, ctx.re.IGNORECASE):
        value = "https://" + value
    parsed = ctx.urlparse(value)
    if not parsed.netloc:
        raise RuntimeError("Invalid SUB2API site URL")
    path = ctx.re.sub(
        "/(?:api/v1|admin/accounts.*)$", "", parsed.path or "", flags=ctx.re.IGNORECASE
    ).rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def unwrap_sub2api_data(ctx: Sub2APIContext, payload):
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    return data if data is not None else payload


def sub2api_json_request(
    ctx: Sub2APIContext, url, method="GET", payload=None, token=""
):
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = ctx.json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = ctx.Request(url, data=body, headers=headers, method=method)
    try:
        with ctx.urlopen(request, timeout=ctx.REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except ctx.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"SUB2API HTTP {exc.code}: {ctx.compact_text(detail, 1200)}"
        ) from exc
    except ctx.URLError as exc:
        raise RuntimeError(f"Cannot connect to SUB2API: {exc.reason}") from exc
    try:
        return ctx.json.loads(raw) if raw.strip() else {}
    except ctx.json.JSONDecodeError as exc:
        raise RuntimeError(
            f"SUB2API returned invalid JSON: {ctx.compact_text(raw, 600)}"
        ) from exc


def login_sub2api(ctx: Sub2APIContext, base_url, email_addr, password):
    login_response = ctx.sub2api_json_request(
        f"{base_url}/api/v1/auth/login",
        method="POST",
        payload={"email": email_addr, "password": password},
    )
    login_data = ctx.unwrap_sub2api_data(login_response)
    if not isinstance(login_data, dict):
        raise RuntimeError("SUB2API login returned an invalid response")
    if login_data.get("requires_2fa"):
        raise RuntimeError(
            "SUB2API administrator account has 2FA enabled; password auto-login is unavailable"
        )
    token = str(
        login_data.get("access_token") or login_response.get("access_token") or ""
    ).strip()
    if not token:
        raise RuntimeError("SUB2API login succeeded without returning access_token")
    return token


def list_sub2api_groups(ctx: Sub2APIContext, base_url, token):
    query = ctx.urlencode(
        {
            "page": 1,
            "page_size": 1000,
            "platform": "openai",
            "sort_by": "sort_order",
            "sort_order": "asc",
        }
    )
    response = ctx.sub2api_json_request(
        f"{base_url}/api/v1/admin/groups?{query}", token=token
    )
    data = ctx.unwrap_sub2api_data(response)
    if isinstance(data, dict):
        groups = data.get("items") or data.get("list") or data.get("groups") or []
    else:
        groups = data if isinstance(data, list) else []
    normalized = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        try:
            group_id = int(group.get("id"))
        except (TypeError, ValueError):
            continue
        normalized.append({"id": group_id, "name": name})
    return normalized


def fetch_sub2api_groups(ctx: Sub2APIContext, config):
    base_url = ctx.normalize_sub2api_base_url(config.get("url"))
    email_addr = str(config.get("email") or "").strip()
    password = str(config.get("password") or "")
    if not email_addr or not password:
        raise RuntimeError("Missing SUB2API login email or password")
    token = ctx.login_sub2api(base_url, email_addr, password)
    groups = ctx.list_sub2api_groups(base_url, token)
    if not groups:
        raise RuntimeError("No OpenAI groups were found in SUB2API")
    return {"siteUrl": base_url, "groups": groups}


def find_sub2api_group_id(ctx: Sub2APIContext, base_url, token, group_name):
    target = str(group_name or "").strip()
    if not target:
        return None
    for group in ctx.list_sub2api_groups(base_url, token):
        if group["name"].lower() == target.lower():
            return group["id"]
    raise RuntimeError(f"SUB2API group not found: {target}")


def import_agent_identity_to_sub2api(ctx: Sub2APIContext, auth_file_path, config):
    base_url = ctx.normalize_sub2api_base_url(config.get("url"))
    email_addr = str(config.get("email") or "").strip()
    password = str(config.get("password") or "")
    if not email_addr or not password:
        raise RuntimeError("Missing SUB2API login email or password")
    token = ctx.login_sub2api(base_url, email_addr, password)
    auth_payload = ctx.json.loads(ctx.Path(auth_file_path).read_text(encoding="utf-8"))
    identity = (
        auth_payload.get("agent_identity") if isinstance(auth_payload, dict) else None
    )
    if not isinstance(identity, dict):
        raise RuntimeError("Generated auth.json does not contain agent_identity")
    auth_payload["auth_mode"] = "agentIdentity"
    ctx.Path(auth_file_path).write_text(
        ctx.json.dumps(auth_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    group_id = ctx.find_sub2api_group_id(base_url, token, config.get("groupName"))
    try:
        priority = max(
            0, int(config.get("priority") if config.get("priority") is not None else 50)
        )
    except (TypeError, ValueError):
        priority = 50
    account_name = str(
        identity.get("email") or config.get("accountName") or "Codex Agent Identity"
    ).strip()
    import_payload = {
        "content": ctx.json.dumps(auth_payload, ensure_ascii=False),
        "name": account_name,
        "group_ids": [group_id] if group_id is not None else [],
        "concurrency": 3,
        "priority": priority,
        "update_existing": True,
    }
    response = ctx.sub2api_json_request(
        f"{base_url}/api/v1/admin/accounts/import/codex-session",
        method="POST",
        payload=import_payload,
        token=token,
    )
    result = ctx.unwrap_sub2api_data(response)
    if not isinstance(result, dict):
        raise RuntimeError("SUB2API import returned an invalid response")
    created = int(result.get("created") or 0)
    updated = int(result.get("updated") or 0)
    failed = int(result.get("failed") or 0)
    if failed > 0 or created + updated < 1:
        errors = result.get("errors") or result.get("items") or []
        raise RuntimeError(
            f"SUB2API Agent Identity import failed: {ctx.compact_text(ctx.json.dumps(errors, ensure_ascii=False), 1200)}"
        )
    return {
        "siteUrl": base_url,
        "groupId": group_id,
        "groupName": str(config.get("groupName") or "").strip(),
        "created": created,
        "updated": updated,
        "failed": failed,
    }
