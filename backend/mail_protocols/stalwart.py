"""Stalwart JMAP mailbox protocol."""
from __future__ import annotations

import re
import random
from typing import Any

import httpx

from .common import (
    _extract_codes_and_links,
    _stalwart_credentials,
    normalize_stalwart_base_url,
)


def stalwart_create_mailbox(
    *,
    name: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    """Create a virtual address handled by the domain's catch-all mailbox."""
    del expiry_ms, proxy, proxy_username, proxy_password
    dom = (domain or "").strip().lstrip("@").strip(".").lower()
    username, password = _stalwart_credentials(api_key, dom)
    base = normalize_stalwart_base_url(base_url)
    local = re.sub(r"[^a-z0-9._-]", "", (name or "").strip().lower())
    if not local:
        local = "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(12))
    if not dom:
        raise ValueError("Stalwart mail domain is missing")

    with httpx.Client(timeout=20.0, auth=(username, password)) as client:
        response = client.get(f"{base}/jmap/session")
        if response.status_code >= 400:
            raise RuntimeError(
                f"Stalwart authentication failed {response.status_code}: {response.text[:300]}"
            )
        session = response.json()
        account_id = (session.get("primaryAccounts") or {}).get(
            "urn:ietf:params:jmap:mail"
        )
        if not account_id:
            raise RuntimeError("Stalwart administrator mailbox is unavailable")

    address = f"{local}@{dom}"
    return {
        "id": address,
        "email": address,
        "token": "",
        "provider": "stalwart",
    }


def stalwart_fetch_messages(
    email_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
    address: str | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Read recent messages for one virtual catch-all address over JMAP."""
    del email_id, include_details, token
    target = (address or "").strip().lower()
    if "@" not in target:
        return []
    domain = target.rsplit("@", 1)[1]
    username, password = _stalwart_credentials(api_key, domain)
    base = normalize_stalwart_base_url(base_url)

    with httpx.Client(timeout=25.0, auth=(username, password)) as client:
        session_response = client.get(f"{base}/jmap/session")
        if session_response.status_code >= 400:
            raise RuntimeError(
                f"Stalwart authentication failed {session_response.status_code}: "
                f"{session_response.text[:300]}"
            )
        session = session_response.json()
        account_id = (session.get("primaryAccounts") or {}).get(
            "urn:ietf:params:jmap:mail"
        )
        if not account_id:
            raise RuntimeError("Stalwart administrator mailbox is unavailable")

        payload = {
            "using": [
                "urn:ietf:params:jmap:core",
                "urn:ietf:params:jmap:mail",
            ],
            "methodCalls": [
                [
                    "Email/query",
                    {
                        "accountId": account_id,
                        "filter": {"to": target},
                        "sort": [{"property": "receivedAt", "isAscending": False}],
                        "limit": 20,
                    },
                    "query",
                ],
                [
                    "Email/get",
                    {
                        "accountId": account_id,
                        "#ids": {
                            "resultOf": "query",
                            "name": "Email/query",
                            "path": "/ids",
                        },
                        "properties": [
                            "id",
                            "subject",
                            "from",
                            "to",
                            "receivedAt",
                            "textBody",
                            "htmlBody",
                            "bodyValues",
                        ],
                        "fetchTextBodyValues": True,
                        "fetchHTMLBodyValues": True,
                        "maxBodyValueBytes": 200000,
                    },
                    "get",
                ],
            ],
        }
        response = client.post(f"{base}/jmap/", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"Stalwart JMAP failed {response.status_code}: {response.text[:500]}"
            )
        data = response.json()

    items: list[dict[str, Any]] = []
    for method_name, result, call_id in data.get("methodResponses") or []:
        if method_name != "Email/get" or call_id != "get" or not isinstance(result, dict):
            continue
        for raw in result.get("list") or []:
            if not isinstance(raw, dict):
                continue
            recipients = [
                str(item.get("email") or "").strip().lower()
                for item in raw.get("to") or []
                if isinstance(item, dict)
            ]
            if target not in recipients:
                continue
            body_values = raw.get("bodyValues") or {}
            content = "\n".join(
                str(value.get("value") or "")
                for value in body_values.values()
                if isinstance(value, dict)
            )
            senders = [
                str(item.get("email") or "")
                for item in raw.get("from") or []
                if isinstance(item, dict)
            ]
            item = {
                **raw,
                "content": content,
                "text": content,
                "from_address": ", ".join(x for x in senders if x),
            }
            item["extracted"] = _extract_codes_and_links(
                "\n".join((str(item.get("subject") or ""), content))
            )
            items.append(item)
    return items
