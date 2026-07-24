"""GPTMail API protocol."""
from __future__ import annotations

from typing import Any

import httpx

from .common import (
    GPTMAIL_PUBLIC_TEST_KEY,
    MOEMAIL_API_KEY,
    MOEMAIL_BASE_URL,
    _extract_codes_and_links,
    _headers,
    normalize_gptmail_base_url,
)


def gptmail_create_mailbox(
    *,
    name: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,  # accepted for API compat; GPTMail retains ~24h
    api_key: str | None = None,
    base_url: str | None = None,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    """Create a temporary inbox on GPTMail (https://mail.chatgpt.org.uk/zh/api/)."""
    key = (api_key or MOEMAIL_API_KEY or "").strip() or GPTMAIL_PUBLIC_TEST_KEY
    base = normalize_gptmail_base_url(base_url or MOEMAIL_BASE_URL)
    # Never fall back to MOEMAIL_DOMAIN (MoeMail default). Empty => GPTMail
    # random generate / public domain pick.
    dom = (domain or "").strip().lstrip("@").strip(".")
    pre = (name or "").strip().lower() or None

    # Prefer server-side generate so we get a real active domain when none given.
    # Docs: GET /api/generate-email random; POST with {prefix, domain}.
    with httpx.Client(timeout=30.0) as client:
        headers = {**_headers(key), "Content-Type": "application/json"}
        if pre or dom:
            payload: dict[str, Any] = {}
            if pre:
                payload["prefix"] = pre
            if dom:
                payload["domain"] = dom
            resp = client.post(
                f"{base}/api/generate-email",
                json=payload,
                headers=headers,
            )
        else:
            resp = client.get(f"{base}/api/generate-email", headers=headers)

        if resp.status_code >= 400:
            # Auth / quota failures must surface — composed addresses still need
            # a valid key to poll /api/emails.
            err_l = (resp.text or "").lower()
            if resp.status_code in (401, 403) or (
                "api key" in err_l
                or "api_key" in err_l
                or "无效" in (resp.text or "")
                and "key" in err_l
            ):
                raise RuntimeError(
                    f"GPTMail create failed {resp.status_code}: {resp.text[:500]}"
                )
            # Retry without domain, then compose prefix@public-domain.
            # Docs allow skipping generate when a public domain is known.
            if pre and dom:
                resp2 = client.post(
                    f"{base}/api/generate-email",
                    json={"prefix": pre},
                    headers=headers,
                )
                if resp2.status_code < 400:
                    resp = resp2
                elif resp2.status_code in (401, 403):
                    raise RuntimeError(
                        f"GPTMail create failed {resp2.status_code}: {resp2.text[:500]}"
                    )
                else:
                    picked = gptmail_pick_domain(api_key=key, base_url=base) or dom
                    if pre and picked:
                        address = f"{pre}@{picked}"
                        return {
                            "id": address,
                            "email": address,
                            "token": "",
                            "provider": "gptmail",
                            "raw": {
                                "composed": True,
                                "error": resp.text[:300],
                                "domain": picked,
                            },
                            "expiry_ms": 86_400_000
                            if expiry_ms is None
                            else int(expiry_ms),
                        }
            elif pre and resp.status_code not in (401, 403):
                picked = dom or gptmail_pick_domain(api_key=key, base_url=base)
                if picked:
                    address = f"{pre}@{picked}"
                    return {
                        "id": address,
                        "email": address,
                        "token": "",
                        "provider": "gptmail",
                        "raw": {
                            "composed": True,
                            "error": resp.text[:300],
                            "domain": picked,
                        },
                        "expiry_ms": 86_400_000
                        if expiry_ms is None
                        else int(expiry_ms),
                    }
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"GPTMail create failed {resp.status_code}: {resp.text[:500]}"
                )

        data = resp.json() if resp.content else {}

    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected GPTMail create response: {data}")
    address = body.get("email") or body.get("address")
    if not address or "@" not in str(address):
        raise RuntimeError(f"Unexpected GPTMail create response: {data}")
    address = str(address).strip()
    # GPTMail uses the email address itself as the mailbox key for list/clear.
    return {
        "id": address,
        "email": address,
        "token": "",
        "provider": "gptmail",
        "raw": data,
        "expiry_ms": 86_400_000 if expiry_ms is None else int(expiry_ms),
    }


def gptmail_pick_domain(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> str | None:
    """Pick an active public domain from GPTMail catalog."""
    base = normalize_gptmail_base_url(base_url or MOEMAIL_BASE_URL)
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    try:
        with httpx.Client(timeout=20.0) as client:
            # Public domain list does not require a key.
            resp = client.get(
                f"{base}/api/domains/public",
                headers=_headers(key) if key else {},
            )
            if resp.status_code >= 400:
                return None
            data = resp.json()
    except Exception:
        return None
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    items = body.get("domains") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("domain_name") or item.get("domain") or item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if item.get("is_active") in (0, False, "0", "false"):
            continue
        return name.strip().lstrip("@").strip(".")
    return None


def gptmail_fetch_messages(
    email_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
    address: str | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """List messages for a GPTMail inbox.

    GPTMail keys mailboxes by the full email address (``?email=``).
    ``email_id`` may be either the address or a message id when fetching detail.
    """
    addr = (address or email_id or "").strip()
    if not addr or "@" not in addr:
        # If only a message id was passed, we cannot list; need address.
        if address and "@" in address:
            addr = address.strip()
        else:
            return []
    key = (api_key or MOEMAIL_API_KEY or "").strip() or GPTMAIL_PUBLIC_TEST_KEY
    base = normalize_gptmail_base_url(base_url or MOEMAIL_BASE_URL)
    headers = _headers(key)

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(
            f"{base}/api/emails",
            headers=headers,
            params={"email": addr},
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"GPTMail list failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json() if resp.content else {}
        body = data.get("data") if isinstance(data, dict) and "data" in data else data
        messages: list[Any] = []
        if isinstance(body, dict):
            messages = body.get("emails") or body.get("messages") or body.get("items") or []
        elif isinstance(body, list):
            messages = body
        if not isinstance(messages, list):
            return []

        out: list[dict[str, Any]] = []
        for raw in messages[:20]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            msg_id = item.get("id") or item.get("messageId") or item.get("email_id")
            # List payload often already includes content; detail is optional.
            if include_details and msg_id and not (
                item.get("content") or item.get("html_content") or item.get("html")
            ):
                detail = client.get(
                    f"{base}/api/email/{msg_id}",
                    headers=headers,
                )
                if detail.status_code == 200:
                    d = detail.json() if detail.content else {}
                    msg = d.get("data") if isinstance(d, dict) and "data" in d else d
                    if isinstance(msg, dict):
                        if isinstance(msg.get("email"), dict):
                            item.update(msg["email"])
                        elif isinstance(msg.get("message"), dict):
                            item.update(msg["message"])
                        else:
                            item.update(msg)
            # Normalize field names for shared code extractors.
            if item.get("html_content") and not item.get("html"):
                item["html"] = item.get("html_content")
            if item.get("content") and not item.get("text"):
                item["text"] = item.get("content")
            if item.get("from_address") and not item.get("from"):
                item["from"] = item.get("from_address")
            text = "\n".join(
                str(item.get(k) or "")
                for k in (
                    "subject",
                    "content",
                    "text",
                    "html",
                    "html_content",
                    "from_address",
                    "from",
                )
            )
            item["extracted"] = _extract_codes_and_links(text)
            out.append(item)
        return out
