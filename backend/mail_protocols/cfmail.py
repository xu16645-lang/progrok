"""Cloudflare Temp Email protocol."""
from __future__ import annotations

import email
import random
from email import policy
from typing import Any

import httpx

from .common import (
    MOEMAIL_API_KEY,
    MOEMAIL_BASE_URL,
    _cfmail_headers,
    _extract_codes_and_links,
    normalize_cfmail_base_url,
    secrets_token_hex_local,
)


def cfmail_list_domains(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    site_password: str | None = None,
) -> list[str]:
    """List domains from CF Temp Email public settings (``GET /open_api/settings``)."""
    base = normalize_cfmail_base_url(base_url or MOEMAIL_BASE_URL)
    headers = _cfmail_headers(api_key=api_key, site_password=site_password)
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(f"{base}/open_api/settings", headers=headers)
            if resp.status_code >= 400:
                # Older deploys may expose domains only on authenticated settings.
                resp2 = client.get(f"{base}/api/settings", headers=headers)
                if resp2.status_code >= 400:
                    return []
                data = resp2.json() if resp2.content else {}
            else:
                data = resp.json() if resp.content else {}
    except Exception:
        return []
    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(body, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for key in (
        "defaultDomains",
        "default_domains",
        "domains",
        "randomSubdomainDomains",
        "random_subdomain_domains",
    ):
        items = body.get(key)
        if isinstance(items, str):
            items = [x.strip() for x in items.split(",") if x.strip()]
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                name = item.get("domain") or item.get("name") or item.get("value")
            else:
                name = item
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip().lstrip("@").strip(".")
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


def cfmail_pick_domain(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    site_password: str | None = None,
) -> str | None:
    """Randomly pick a domain from CF Temp Email public settings."""
    domains = cfmail_list_domains(
        api_key=api_key, base_url=base_url, site_password=site_password
    )
    if not domains:
        return None
    return random.choice(domains)


def _cfmail_parse_raw_rfc822(raw: str) -> dict[str, Any]:
    """Best-effort RFC822 parse for CF Temp Email raw mail bodies."""
    out: dict[str, Any] = {}
    text = (raw or "").strip()
    if not text:
        return out
    try:
        msg = email.message_from_string(text, policy=policy.default)
    except Exception:
        out["text"] = text[:8000]
        return out
    out["subject"] = str(msg.get("subject") or "")
    out["from"] = str(msg.get("from") or "")
    out["to"] = str(msg.get("to") or "")
    texts: list[str] = []
    htmls: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = str(part.get_content_disposition() or "").lower()
            if disp == "attachment":
                continue
            try:
                payload = part.get_content()
            except Exception:
                try:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        payload = payload.decode(
                            part.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                except Exception:
                    payload = None
            if not isinstance(payload, str):
                continue
            if ctype == "text/html":
                htmls.append(payload)
            elif ctype.startswith("text/"):
                texts.append(payload)
    else:
        try:
            payload = msg.get_content()
        except Exception:
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = payload.decode(
                    msg.get_content_charset() or "utf-8", errors="replace"
                )
        if isinstance(payload, str):
            if (msg.get_content_type() or "").lower() == "text/html":
                htmls.append(payload)
            else:
                texts.append(payload)
    if texts:
        out["text"] = "\n".join(texts)
    if htmls:
        out["html"] = "\n".join(htmls)
    if not texts and not htmls:
        out["text"] = text[:8000]
    return out


def cfmail_create_mailbox(
    *,
    name: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,  # accepted for API compat; CF address is durable
    api_key: str | None = None,
    base_url: str | None = None,
    site_password: str | None = None,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    """Create an address on Cloudflare Temp Email.

    Preferred path (automation): ``POST /admin/new_address`` with admin password
    in ``x-admin-auth`` (pass as api_key).

    Fallback: ``POST /api/new_address`` (may require Turnstile / open create).

    Docs: https://github.com/dreamhunter2333/cloudflare_temp_email
    """
    key = (api_key or MOEMAIL_API_KEY or "").strip()
    base = normalize_cfmail_base_url(base_url or MOEMAIL_BASE_URL)
    # Never bleed MoeMail default domain into CF.
    dom = (domain or "").strip().lstrip("@").strip(".")
    if not dom:
        dom = cfmail_pick_domain(
            api_key=key, base_url=base, site_password=site_password
        ) or ""
    if not dom:
        raise ValueError(
            "Cloudflare Temp Email domain missing. Set domain in registration "
            "config, or ensure /open_api/settings returns domains."
        )
    local = (name or "").strip().lower()
    if not local:
        local = secrets_token_hex_local()

    payload: dict[str, Any] = {
        "name": local,
        "domain": dom,
        # Admin API field; public API ignores unknown keys.
        "enablePrefix": False,
    }
    headers = _cfmail_headers(
        api_key=key, site_password=site_password, content_type=True
    )
    # Prefer admin create (no captcha) when we have a non-JWT key.
    use_admin = bool(key) and "Authorization" not in headers

    with httpx.Client(timeout=30.0) as client:
        if use_admin:
            resp = client.post(
                f"{base}/admin/new_address", json=payload, headers=headers
            )
            if resp.status_code >= 400:
                # Fall through to public create for older/non-admin deploys.
                resp = client.post(
                    f"{base}/api/new_address", json=payload, headers=headers
                )
        else:
            resp = client.post(
                f"{base}/api/new_address", json=payload, headers=headers
            )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"CF Temp Email create failed {resp.status_code}: {resp.text[:500]}"
            )
        data = resp.json() if resp.content else {}

    body = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected CF Temp Email create response: {data}")
    address = (
        body.get("address")
        or body.get("email")
        or body.get("mail")
        or body.get("name")
    )
    jwt = (
        body.get("jwt")
        or body.get("token")
        or body.get("credential")
        or body.get("address_jwt")
        or ""
    )
    address_id = (
        body.get("address_id")
        or body.get("id")
        or body.get("addressId")
        or address
    )
    if not address or "@" not in str(address):
        # Some responses only return jwt + partial; try settings with jwt.
        if jwt:
            try:
                with httpx.Client(timeout=20.0) as client:
                    sresp = client.get(
                        f"{base}/api/settings",
                        headers=_cfmail_headers(api_key=str(jwt), bearer=True),
                    )
                    if sresp.status_code < 400:
                        sdata = sresp.json() if sresp.content else {}
                        sbody = (
                            sdata.get("data")
                            if isinstance(sdata, dict) and "data" in sdata
                            else sdata
                        )
                        if isinstance(sbody, dict):
                            address = sbody.get("address") or address
            except Exception:
                pass
    if not address or "@" not in str(address):
        raise RuntimeError(f"Unexpected CF Temp Email create response: {data}")
    if not jwt:
        # Without address JWT we cannot poll inbox.
        raise RuntimeError(
            "CF Temp Email create returned no address JWT. "
            "Use admin password (x-admin-auth) via api_key, or enable open create."
        )
    return {
        "id": str(address_id or address),
        "email": str(address).strip(),
        "token": str(jwt),
        "provider": "cfmail",
        "raw": data,
        "expiry_ms": 86_400_000 if expiry_ms is None else int(expiry_ms),
    }


def cfmail_fetch_messages(
    email_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
    address: str | None = None,
    token: str | None = None,
    site_password: str | None = None,
) -> list[dict[str, Any]]:
    """List messages for a CF Temp Email address JWT.

    Prefers parsed endpoints; falls back to raw RFC822 list/detail.
    ``token`` (address JWT) is required for inbox access. ``api_key`` may also
    be the JWT when the admin key is not needed.
    """
    jwt = (token or api_key or MOEMAIL_API_KEY or "").strip()
    if not jwt:
        return []
    base = normalize_cfmail_base_url(base_url or MOEMAIL_BASE_URL)
    headers = _cfmail_headers(
        api_key=jwt, site_password=site_password, bearer=True
    )

    with httpx.Client(timeout=30.0) as client:
        # 1) Parsed list (newer deploys)
        items: list[Any] = []
        used_parsed = False
        resp = client.get(
            f"{base}/api/parsed_mails",
            headers=headers,
            params={"limit": 20, "offset": 0},
        )
        if resp.status_code < 400:
            data = resp.json() if resp.content else {}
            body = data.get("data") if isinstance(data, dict) and "data" in data else data
            if isinstance(body, dict):
                items = body.get("results") or body.get("mails") or body.get("items") or []
            elif isinstance(body, list):
                items = body
            used_parsed = True
        else:
            # 2) Raw list fallback
            resp = client.get(
                f"{base}/api/mails",
                headers=headers,
                params={"limit": 20, "offset": 0},
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"CF Temp Email list failed {resp.status_code}: {resp.text[:500]}"
                )
            data = resp.json() if resp.content else {}
            body = data.get("data") if isinstance(data, dict) and "data" in data else data
            if isinstance(body, dict):
                items = body.get("results") or body.get("mails") or body.get("items") or []
            elif isinstance(body, list):
                items = body

        if not isinstance(items, list):
            return []

        out: list[dict[str, Any]] = []
        for raw in items[:20]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            msg_id = item.get("id") or item.get("mail_id") or item.get("message_id")
            if include_details and msg_id and not used_parsed:
                detail = client.get(
                    f"{base}/api/mail/{msg_id}",
                    headers=headers,
                )
                if detail.status_code == 200:
                    d = detail.json() if detail.content else {}
                    msg = d.get("data") if isinstance(d, dict) and "data" in d else d
                    if isinstance(msg, dict):
                        item.update(msg)
            # Normalize CF shapes → shared extractor fields.
            if not item.get("text") and not item.get("html"):
                raw_rfc = (
                    item.get("raw")
                    or item.get("source")
                    or item.get("message")
                    or item.get("content")
                    or ""
                )
                if isinstance(raw_rfc, str) and ("\n" in raw_rfc or "From:" in raw_rfc):
                    parsed = _cfmail_parse_raw_rfc822(raw_rfc)
                    for k, v in parsed.items():
                        item.setdefault(k, v)
            if item.get("sender") and not item.get("from"):
                item["from"] = item.get("sender")
            if item.get("source") and not item.get("from"):
                # Some rows store envelope sender in source.
                src = item.get("source")
                if isinstance(src, str) and "@" in src and "\n" not in src:
                    item["from"] = src
            text = "\n".join(
                str(item.get(k) or "")
                for k in (
                    "subject",
                    "text",
                    "html",
                    "content",
                    "from",
                    "sender",
                )
            )
            item["extracted"] = _extract_codes_and_links(text)
            if msg_id is not None:
                item["id"] = str(msg_id)
            out.append(item)
        return out
