"""MoeMail API protocol."""
from __future__ import annotations

from typing import Any

import httpx

from .common import (
    MOEMAIL_API_KEY,
    MOEMAIL_BASE_URL,
    MOEMAIL_DOMAIN,
    MOEMAIL_EXPIRY_MS,
    _extract_codes_and_links,
    _headers,
    _moemail_infer_domain,
)


def moemail_create_mailbox(
    *,
    name: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    proxy: str | None = None,  # accepted for API compat; unused by httpx path
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    if not (api_key or MOEMAIL_API_KEY):
        raise ValueError(
            "MoeMail API key missing. Set GROK2API_MOEMAIL_API_KEY or pass api_key."
        )

    base = (base_url or MOEMAIL_BASE_URL).rstrip("/")
    # MoeMail only accepts official presets: 3600000 / 86400000 / 259200000 / 0.
    # Do not use `expiry_ms or default` — permanent is 0 and must be preserved.
    _OFFICIAL = {3_600_000, 86_400_000, 259_200_000, 0}
    if expiry_ms is None:
        chosen = int(MOEMAIL_EXPIRY_MS)
    else:
        chosen = int(expiry_ms)
    if chosen not in _OFFICIAL:
        # snap to nearest timed preset (never invent permanent from bad input)
        timed = (3_600_000, 86_400_000, 259_200_000)
        chosen = min(timed, key=lambda p: abs(p - chosen))
    payload: dict[str, Any] = {
        "expiryTime": chosen,
        "domain": domain or MOEMAIL_DOMAIN,
    }
    if name:
        payload["name"] = name

    with httpx.Client(timeout=30.0) as client:
        headers = {**_headers(api_key), "Content-Type": "application/json"}
        resp = client.post(f"{base}/api/emails/generate", json=payload, headers=headers)
        if resp.status_code == 400 and "域名" in resp.text and not domain:
            inferred = _moemail_infer_domain(client, base, api_key=api_key)
            if inferred and inferred != payload.get("domain"):
                payload["domain"] = inferred
                resp = client.post(
                    f"{base}/api/emails/generate",
                    json=payload,
                    headers=headers,
                )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"MoeMail create failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()

    email_id = data.get("id") or data.get("emailId")
    address = data.get("email") or data.get("address")
    if not email_id or not address:
        raise RuntimeError(f"Unexpected MoeMail create response: {data}")
    return {"id": str(email_id), "email": str(address), "raw": data}


def moemail_fetch_messages(
    email_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
) -> list[dict[str, Any]]:
    if not email_id:
        return []
    if not (api_key or MOEMAIL_API_KEY):
        return []

    base = (base_url or MOEMAIL_BASE_URL).rstrip("/")
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{base}/api/emails/{email_id}", headers=_headers(api_key))
        if resp.status_code >= 400:
            raise RuntimeError(
                f"MoeMail list failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json()
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return []

        out: list[dict[str, Any]] = []
        for raw in messages[:20]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            msg_id = item.get("id") or item.get("messageId")
            if include_details and msg_id:
                detail = client.get(
                    f"{base}/api/emails/{email_id}/{msg_id}",
                    headers=_headers(api_key),
                )
                if detail.status_code == 200:
                    d = detail.json()
                    msg = d.get("message") if isinstance(d, dict) else None
                    if isinstance(msg, dict):
                        item.update(msg)
            text = "\n".join(
                str(item.get(k) or "")
                for k in ("subject", "content", "html", "from_address", "from")
            )
            item["extracted"] = _extract_codes_and_links(text)
            out.append(item)
        return out
