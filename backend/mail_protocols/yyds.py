"""YYDS Mail API protocol."""
from __future__ import annotations

import random
from typing import Any

import httpx

from .common import (
    MOEMAIL_API_KEY,
    MOEMAIL_BASE_URL,
    _extract_codes_and_links,
    _headers,
    normalize_yyds_base_url,
)


def yyds_create_mailbox(
    *,
    name: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,  # accepted for API compat; YYDS temp mail is ~24h
    api_key: str | None = None,
    base_url: str | None = None,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    """Create a temporary inbox on YYDS Mail (https://vip.215.im/docs)."""
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    if not key:
        raise ValueError(
            "YYDS Mail API key missing. Set GROK2API_MOEMAIL_API_KEY / api_key "
            "(X-API-Key, usually starts with AC-)."
        )
    base = normalize_yyds_base_url(base_url or MOEMAIL_BASE_URL)
    # Never fall back to MOEMAIL_DOMAIN (MoeMail default / example.com). Empty
    # means auto: randomly pick a healthy public domain from GET /v1/domains.
    dom = (domain or "").strip().lstrip("@").strip(".")
    if not dom:
        dom = yyds_pick_domain(api_key=key, base_url=base) or ""
    if not dom:
        raise ValueError(
            "YYDS Mail domain auto-fetch failed. Leave domain empty for random "
            "public domain, or set an explicit domain from GET /v1/domains."
        )
    local = (name or "").strip().lower() or None
    payload: dict[str, Any] = {"domain": dom}
    if local:
        payload["localPart"] = local

    with httpx.Client(timeout=30.0) as client:
        headers = {**_headers(key), "Content-Type": "application/json"}
        resp = client.post(f"{base}/v1/accounts", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"YYDS create failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()

    # Envelope: { success, data: { id, address, token, ... } }
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected YYDS create response: {data}")
    email_id = body.get("id") or body.get("inboxId") or body.get("accountId")
    address = body.get("address") or body.get("email")
    token = body.get("token") or body.get("tempToken") or ""
    if not email_id or not address:
        raise RuntimeError(f"Unexpected YYDS create response: {data}")
    return {
        "id": str(email_id),
        "email": str(address),
        "token": str(token or ""),
        "provider": "yyds",
        "raw": data,
        # Keep expiry_ms for logging only (service is ~24h temp).
        "expiry_ms": 86_400_000 if expiry_ms is None else int(expiry_ms),
    }


def yyds_list_domains(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    public_only: bool = True,
    ready_only: bool = True,
) -> list[str]:
    """List usable domains from YYDS catalog (``GET /v1/domains``)."""
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    base = normalize_yyds_base_url(base_url or MOEMAIL_BASE_URL)
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(f"{base}/v1/domains", headers=_headers(key) if key else {})
            if resp.status_code >= 400:
                return []
            data = resp.json()
    except Exception:
        return []
    items = data
    if isinstance(data, dict):
        items = data.get("data") or data.get("domains") or data.get("items") or []
    if not isinstance(items, list):
        return []
    preferred: list[str] = []
    fallback: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("domain") or item.get("name") or item.get("host")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip().lstrip("@").strip(".")
        if not name or name in seen:
            continue
        if public_only and item.get("isPublic") is False:
            continue
        if ready_only and (
            item.get("receivingReady") is False or item.get("isMxValid") is False
        ):
            continue
        seen.add(name)
        if item.get("wildcardMxValid") is True or item.get("wildcard_mx_valid") is True:
            preferred.append(name)
        else:
            fallback.append(name)
    # Prefer wildcard-MX domains first so random pick weights healthier ones.
    return preferred + fallback


def yyds_pick_domain(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str | None:
    """Randomly pick a healthy public domain from YYDS catalog.

    Catalog order is preferred (wildcard MX) then fallback. Randomize across
    the full usable set so batch registration rotates domains.
    Empty admin domain => call this.
    """
    domains = yyds_list_domains(api_key=api_key, base_url=base_url)
    if not domains:
        return None
    return random.choice(domains)


def yyds_fetch_messages(
    email_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
    address: str | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """List (+ optionally detail) messages for a YYDS inbox."""
    if not email_id and not address:
        return []
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    base = normalize_yyds_base_url(base_url or MOEMAIL_BASE_URL)
    headers = _headers(key) if key else {}
    if token and not key:
        headers = {"Authorization": f"Bearer {token}"}
    elif token and key:
        # Prefer API key; keep bearer as extra only when key missing.
        pass

    with httpx.Client(timeout=30.0) as client:
        # Prefer canonical inbox path when id is known; fall back to address query.
        messages: list[Any] = []
        if email_id:
            resp = client.get(
                f"{base}/v1/inboxes/{email_id}/messages",
                headers=headers,
                params={"limit": 20},
            )
            if resp.status_code >= 400 and address:
                resp = client.get(
                    f"{base}/v1/messages",
                    headers=headers,
                    params={"address": address, "limit": 20},
                )
            elif resp.status_code >= 400:
                raise RuntimeError(
                    f"YYDS list failed {resp.status_code}: {resp.text[:500]}"
                )
        else:
            resp = client.get(
                f"{base}/v1/messages",
                headers=headers,
                params={"address": address, "limit": 20},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"YYDS list failed {resp.status_code}: {resp.text[:500]}"
                )

        data = resp.json() if resp.content else {}
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        if isinstance(body, dict):
            messages = body.get("messages") or body.get("items") or []
        elif isinstance(body, list):
            messages = body
        if not isinstance(messages, list):
            return []

        out: list[dict[str, Any]] = []
        for raw in messages[:20]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            msg_id = item.get("id") or item.get("messageId")
            if include_details and msg_id:
                params = {"address": address} if address else None
                detail = client.get(
                    f"{base}/v1/messages/{msg_id}",
                    headers=headers,
                    params=params,
                )
                if detail.status_code == 200:
                    d = detail.json()
                    msg = d.get("data") if isinstance(d, dict) and "data" in d else d
                    if isinstance(msg, dict):
                        # Some envelopes nest { message: {...} }
                        if isinstance(msg.get("message"), dict):
                            item.update(msg["message"])
                        else:
                            item.update(msg)
            # Flatten from.address for code extractors used by the adapter.
            from_obj = item.get("from")
            if isinstance(from_obj, dict):
                item.setdefault("from_address", from_obj.get("address") or "")
                item.setdefault("from", from_obj.get("address") or from_obj.get("name") or "")
            text = "\n".join(
                str(item.get(k) or "")
                for k in (
                    "subject",
                    "content",
                    "text",
                    "textBody",
                    "html",
                    "htmlBody",
                    "body",
                    "from_address",
                    "from",
                    "verificationCode",
                )
            )
            item["extracted"] = _extract_codes_and_links(text)
            # Surface server-side OTP when present.
            vc = item.get("verificationCode")
            if vc and isinstance(item.get("extracted"), dict):
                codes = list(item["extracted"].get("codes") or [])
                s = str(vc).strip()
                if s and s not in codes:
                    codes.insert(0, s)
                    item["extracted"]["codes"] = codes
            out.append(item)
        return out
