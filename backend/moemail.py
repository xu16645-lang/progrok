"""Unified temporary-mailbox facade with provider-specific implementations."""
from __future__ import annotations

from typing import Any

import httpx

from mail_protocols.common import (  # noqa: F401 - compatibility facade
    CFMAIL_DEFAULT_BASE_URL,
    GPTMAIL_DEFAULT_BASE_URL,
    GPTMAIL_PUBLIC_TEST_KEY,
    MOEMAIL_API_KEY,
    MOEMAIL_BASE_URL,
    MOEMAIL_DOMAIN,
    MOEMAIL_EXPIRY_MS,
    STALWART_DEFAULT_BASE_URL,
    XAI_PROXY,
    XAI_PROXY_PASSWORD,
    XAI_PROXY_USERNAME,
    YYDS_DEFAULT_BASE_URL,
    YYDS_DEFAULT_DOMAIN,
    _cfmail_headers,
    _extract_codes_and_links,
    _headers,
    _moemail_infer_domain,
    _normalize_proxy_config,
    _stalwart_credentials,
    _visible_message_text,
    extract_verification_codes,
    normalize_cfmail_base_url,
    normalize_gptmail_base_url,
    normalize_mail_provider,
    normalize_proxy_config,
    normalize_stalwart_base_url,
    normalize_yyds_base_url,
    secrets_token_hex_local,
)
from mail_protocols.cfmail import (  # noqa: F401 - compatibility facade
    _cfmail_parse_raw_rfc822,
    cfmail_create_mailbox,
    cfmail_fetch_messages,
    cfmail_list_domains,
    cfmail_pick_domain,
)
from mail_protocols.gptmail import (  # noqa: F401 - compatibility facade
    gptmail_create_mailbox,
    gptmail_fetch_messages,
    gptmail_pick_domain,
)
from mail_protocols.imap_mail import imap_create_mailbox, imap_fetch_messages
from mail_protocols.moemail import moemail_create_mailbox, moemail_fetch_messages
from mail_protocols.stalwart import stalwart_create_mailbox, stalwart_fetch_messages
from mail_protocols.yyds import (  # noqa: F401 - compatibility facade
    yyds_create_mailbox,
    yyds_fetch_messages,
    yyds_list_domains,
    yyds_pick_domain,
)


# Private aliases matching historical names used by adapter code paths.
_moemail_create_mailbox = moemail_create_mailbox
_moemail_fetch_messages = moemail_fetch_messages


def create_mailbox(
    *,
    provider: str | None = None,
    name: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    """Provider-aware mailbox creation."""
    prov = normalize_mail_provider(provider, base_url=base_url)
    if prov == "imap":
        return imap_create_mailbox(
            name=name,
            domain=domain,
            expiry_ms=expiry_ms,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
    if prov == "stalwart":
        return stalwart_create_mailbox(
            name=name,
            domain=domain,
            expiry_ms=expiry_ms,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
    if prov == "yyds":
        return yyds_create_mailbox(
            name=name,
            domain=domain,
            expiry_ms=expiry_ms,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
    if prov == "gptmail":
        return gptmail_create_mailbox(
            name=name,
            domain=domain,
            expiry_ms=expiry_ms,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
    if prov == "cfmail":
        return cfmail_create_mailbox(
            name=name,
            domain=domain,
            expiry_ms=expiry_ms,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
            proxy_username=proxy_username,
            proxy_password=proxy_password,
        )
    box = moemail_create_mailbox(
        name=name,
        domain=domain,
        expiry_ms=expiry_ms,
        api_key=api_key,
        base_url=base_url,
        proxy=proxy,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
    )
    box.setdefault("provider", "moemail")
    box.setdefault("token", "")
    return box

def fetch_messages(
    email_id: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
    address: str | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Provider-aware message list."""
    prov = normalize_mail_provider(provider, base_url=base_url)
    if prov == "imap":
        return imap_fetch_messages(
            email_id,
            api_key=api_key,
            base_url=base_url,
            include_details=include_details,
            address=address,
            token=token,
        )
    if prov == "stalwart":
        return stalwart_fetch_messages(
            email_id,
            api_key=api_key,
            base_url=base_url,
            include_details=include_details,
            address=address,
            token=token,
        )
    if prov == "yyds":
        return yyds_fetch_messages(
            email_id,
            api_key=api_key,
            base_url=base_url,
            include_details=include_details,
            address=address,
            token=token,
        )
    if prov == "gptmail":
        return gptmail_fetch_messages(
            email_id,
            api_key=api_key,
            base_url=base_url,
            include_details=include_details,
            address=address or email_id,
            token=token,
        )
    if prov == "cfmail":
        return cfmail_fetch_messages(
            email_id,
            api_key=api_key,
            base_url=base_url,
            include_details=include_details,
            address=address,
            token=token,
        )
    return moemail_fetch_messages(
        email_id,
        api_key=api_key,
        base_url=base_url,
        include_details=include_details,
    )

def test_xai_proxy(
    *,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
) -> dict[str, Any]:
    """Smoke-test whether a proxy can reach accounts.x.ai."""
    try:
        proxy_cfg = normalize_proxy_config(
            proxy,
            username=proxy_username,
            password=proxy_password,
        )
    except ValueError as e:
        return {"ok": False, "error": str(e), "proxy_enabled": False}

    url = "https://accounts.x.ai/sign-up?redirect=grok-com"
    headers = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        ),
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        from curl_cffi import requests as curl_requests
    except Exception:
        curl_requests = None

    if curl_requests is not None:
        try:
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": 45,
                "allow_redirects": True,
                "impersonate": "chrome",
            }
            if proxy_cfg:
                kwargs["proxies"] = {
                    "http": proxy_cfg["proxy"],
                    "https": proxy_cfg["proxy"],
                }
            resp = curl_requests.get(url, **kwargs)
            return {
                "ok": 200 <= int(resp.status_code) < 400,
                "status_code": int(resp.status_code),
                "body_preview": (resp.text or "")[:500],
                "transport": "curl_cffi",
                "proxy_enabled": bool(proxy_cfg),
            }
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "status_code": 0,
                "body_preview": str(e)[:500],
                "transport": "curl_cffi",
                "proxy_enabled": bool(proxy_cfg),
            }

    try:
        with httpx.Client(
            timeout=45.0,
            proxy=proxy_cfg["proxy"] if proxy_cfg else None,
            follow_redirects=True,
        ) as client:
            resp = client.get(url, headers=headers)
            return {
                "ok": 200 <= int(resp.status_code) < 400,
                "status_code": int(resp.status_code),
                "body_preview": (resp.text or "")[:500],
                "transport": "httpx",
                "proxy_enabled": bool(proxy_cfg),
            }
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "status_code": 0,
            "body_preview": str(e)[:500],
            "transport": "httpx",
            "proxy_enabled": bool(proxy_cfg),
        }
