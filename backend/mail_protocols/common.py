"""Shared mailbox normalization and parsing helpers."""
from __future__ import annotations

import email
import html
import re
from email import policy
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

import httpx

from config import (
    MOEMAIL_API_KEY,
    MOEMAIL_BASE_URL,
    MOEMAIL_DOMAIN,
    MOEMAIL_EXPIRY_MS,
    XAI_PROXY,
    XAI_PROXY_PASSWORD,
    XAI_PROXY_USERNAME,
)


YYDS_DEFAULT_BASE_URL = "https://maliapi.215.im"
YYDS_DEFAULT_DOMAIN = ""
GPTMAIL_DEFAULT_BASE_URL = "https://mail.chatgpt.org.uk"
GPTMAIL_PUBLIC_TEST_KEY = "gpt-test"
CFMAIL_DEFAULT_BASE_URL = "https://temp-email-api.awsl.uk"
STALWART_DEFAULT_BASE_URL = ""


def _headers(api_key: str | None = None) -> dict[str, str]:
    key = api_key or MOEMAIL_API_KEY
    if not key:
        return {}
    return {"X-API-Key": key}


def normalize_mail_provider(provider: str | None, *, base_url: str | None = None) -> str:
    """Return a supported provider identifier.

    Infer from base_url when provider is empty.
    """
    p = (provider or "").strip().lower()
    base = (base_url or "").strip().lower()
    if p in {"imap", "imaps", "imap_mail", "imap-mail"}:
        return "imap"
    if p in {"yyds", "yydsmail", "yyds_mail", "vip215", "215", "maliapi"}:
        return "yyds"
    if p in {"custom", "selfhosted", "self-hosted", "自定义"}:
        # The custom provider is protocol-driven. Existing HTTP deployments
        # keep using the CF Temp Email adapter, while standard mail servers
        # can be connected directly through IMAP.
        return "imap" if base.startswith(("imap://", "imaps://")) else "cfmail"
    if p in {"cloudflare_grokfree", "grokfree"}:
        return "cfmail"
    if p in {
        "gptmail",
        "gpt-mail",
        "gpt_mail",
        "chatgptmail",
        "chatgpt-mail",
        "mail.chatgpt",
        "chatgpt.org.uk",
    }:
        return "gptmail"
    if p in {
        "cfmail",
        "cf-mail",
        "cf_mail",
        "cloudflare",
        "cloudflare_temp_email",
        "cloudflare-temp-email",
        "temp-email",
        "tempmail_cf",
        "awsl",
    }:
        return "cfmail"
    if p in {"moemail", "moe", "moe-mail"}:
        return "moemail"
    if p in {"stalwart", "stalwart_mail", "stalwart-mail"}:
        return "stalwart"
    if base.startswith(("imap://", "imaps://")):
        return "imap"
    if any(x in base for x in ("maliapi.215.im", "vip.215.im", "215.im/v1", "yyds")):
        return "yyds"
    if any(
        x in base
        for x in (
            "mail.chatgpt.org.uk",
            "chatgpt.org.uk",
            "gptmail",
        )
    ):
        return "gptmail"
    if any(
        x in base
        for x in (
            "temp-email-api",
            "temp-email",
            "cloudflare_temp_email",
            "awsl.uk",
            "/api/new_address",
            "/open_api/settings",
        )
    ):
        return "cfmail"
    return "moemail"


def normalize_stalwart_base_url(base_url: str | None = None) -> str:
    raw = (base_url or "").strip() or STALWART_DEFAULT_BASE_URL
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.netloc:
        raise ValueError("Stalwart API address is invalid")
    return f"{parsed.scheme or 'http'}://{parsed.netloc}".rstrip("/")


def _stalwart_credentials(api_key: str | None, domain: str | None) -> tuple[str, str]:
    secret = (api_key or "").strip()
    dom = (domain or "").strip().lstrip("@").strip(".").lower()
    if not secret:
        raise ValueError("Stalwart administrator password is missing")
    if ":" in secret:
        username, password = secret.split(":", 1)
        if username.strip() and password:
            return username.strip(), password
    if not dom:
        raise ValueError("Stalwart mail domain is missing")
    return f"admin@{dom}", secret


def normalize_yyds_base_url(base_url: str | None = None) -> str:
    """Normalize user input (docs URL / trailing /v1) to API origin."""
    raw = (base_url or "").strip()
    if not raw:
        return YYDS_DEFAULT_BASE_URL
    # Common mistakes: paste docs portal or bare path.
    lower = raw.lower()
    if "vip.215.im" in lower and "maliapi" not in lower:
        return YYDS_DEFAULT_BASE_URL
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")
    if not parsed.netloc:
        return YYDS_DEFAULT_BASE_URL
    # Strip accidental /v1 /docs suffixes from path-only pastes handled above.
    return origin or YYDS_DEFAULT_BASE_URL


def normalize_gptmail_base_url(base_url: str | None = None) -> str:
    """Normalize docs / language path pastes to GPTMail origin."""
    raw = (base_url or "").strip()
    if not raw:
        return GPTMAIL_DEFAULT_BASE_URL
    lower = raw.lower()
    if "chatgpt.org.uk" in lower or "gptmail" in lower:
        # Always pin to official origin (docs may be /zh/api, /api, etc.).
        return GPTMAIL_DEFAULT_BASE_URL
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")
    return origin or GPTMAIL_DEFAULT_BASE_URL


def normalize_cfmail_base_url(base_url: str | None = None) -> str:
    """Normalize Cloudflare Temp Email Workers / Pages URL to API origin.

    Accepts worker host, docs host, or accidental ``/api`` / ``/admin`` suffixes.
    Users should deploy their own Workers URL; demo host is only a fallback.
    """
    raw = (base_url or "").strip()
    if not raw:
        return CFMAIL_DEFAULT_BASE_URL
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}".rstrip("/")
    if not parsed.netloc:
        return CFMAIL_DEFAULT_BASE_URL
    return origin or CFMAIL_DEFAULT_BASE_URL


def _cfmail_headers(
    *,
    api_key: str | None = None,
    site_password: str | None = None,
    content_type: bool = False,
    bearer: bool = False,
) -> dict[str, str]:
    """Build CF Temp Email headers.

    - Address JWT (from create / login): ``Authorization: Bearer <jwt>``
    - Admin password (create via admin API): ``x-admin-auth``
    - Optional private-site password: ``x-custom-auth``
    """
    headers: dict[str, str] = {}
    key = (api_key or "").strip()
    if key:
        # Admin create uses x-admin-auth; mailbox read uses a Bearer address
        # token. Self-hosted deployments may issue opaque (non-JWT) mailbox
        # tokens, so callers must explicitly identify mailbox credentials.
        parts = key.split(".")
        if bearer or (len(parts) == 3 and all(parts)):
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers["x-admin-auth"] = key
    site = (site_password or "").strip()
    if site:
        headers["x-custom-auth"] = site
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def normalize_proxy_config(
    proxy: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a proxy URL into curl/httpx-friendly forms."""
    raw = (proxy or XAI_PROXY or "").strip()
    if not raw:
        return None
    env_user = XAI_PROXY_USERNAME
    env_pass = XAI_PROXY_PASSWORD
    lower = raw.lower()
    if lower.startswith("soket5://"):
        raw = "socks5://" + raw.split("://", 1)[1]
    elif lower.startswith("socket5://"):
        raw = "socks5://" + raw.split("://", 1)[1]
    elif "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("proxy scheme must be http, https, socks5, or socks5h")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("proxy must include host and port")
    try:
        port = parsed.port
    except ValueError as e:
        raise ValueError("proxy port is invalid") from e
    proxy_user = (username if username is not None else "").strip()
    proxy_pass = (password if password is not None else "").strip()
    if not proxy_user and username is None:
        proxy_user = env_user
    if not proxy_pass and password is None:
        proxy_pass = env_pass
    if not proxy_user and parsed.username:
        proxy_user = unquote(parsed.username)
    if not proxy_pass and parsed.password:
        proxy_pass = unquote(parsed.password)

    if proxy_pass and not proxy_user:
        raise ValueError("proxy username is required when proxy password is set")

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    proxy_no_auth = urlunparse(
        (
            parsed.scheme,
            host,
            parsed.path or "",
            parsed.params or "",
            parsed.query or "",
            parsed.fragment or "",
        )
    )
    proxy_auth = (proxy_user, proxy_pass) if proxy_user else None
    proxy_with_auth = proxy_no_auth
    if proxy_user:
        auth = quote(proxy_user, safe="")
        if proxy_pass:
            auth = f"{auth}:{quote(proxy_pass, safe='')}"
        proxy_with_auth = urlunparse(
            (
                parsed.scheme,
                f"{auth}@{host}",
                parsed.path or "",
                parsed.params or "",
                parsed.query or "",
                parsed.fragment or "",
            )
        )
    return {
        "proxy": proxy_with_auth,
        "curl_proxy": proxy_no_auth,
        "proxy_auth": proxy_auth,
    }


def _visible_message_text(text: str) -> str:
    raw = str(text or "")
    visible_parts: list[str] = []
    if re.search(r"(?im)^(?:from|to|subject|content-type):", raw):
        try:
            message = email.message_from_string(raw, policy=policy.default)
            parts = message.walk() if message.is_multipart() else (message,)
            for part in parts:
                if part.get_content_disposition() == "attachment":
                    continue
                if part.get_content_type() not in {"text/plain", "text/html"}:
                    continue
                content = part.get_content()
                if isinstance(content, str):
                    visible_parts.append(content)
        except Exception:
            visible_parts = []
    visible = "\n".join(visible_parts) if visible_parts else raw
    visible = html.unescape(visible)
    visible = re.sub(r"(?is)<(?:style|script)\b[^>]*>.*?</(?:style|script)>", " ", visible)
    visible = re.sub(r"(?s)<[^>]+>", " ", visible)
    return re.sub(r"\s+", " ", visible).strip()


def extract_verification_codes(text: str) -> list[str]:
    visible = _visible_message_text(text)
    contextual: list[str] = []
    for pattern in (
        r"(?is)(?:verification|security|one[ -]?time|temporary)\s+code.{0,240}?(?<!\d)(\d{6,8})(?!\d)",
        r"(?is)验证码.{0,120}?(?<!\d)(\d{6,8})(?!\d)",
    ):
        contextual.extend(re.findall(pattern, visible))
    candidates = contextual or re.findall(r"(?<![\d#])\d{6,8}(?!\d)", visible)
    return list(dict.fromkeys(candidates))


def _extract_codes_and_links(text: str) -> dict[str, list[str]]:
    codes = extract_verification_codes(text)
    links = sorted(set(re.findall(r"https?://[^\s\"'<>)]+", text or "")))
    return {"codes": codes, "links": links}


def _moemail_infer_domain(
    client: httpx.Client,
    base: str,
    *,
    api_key: str | None = None,
) -> str | None:
    try:
        resp = client.get(f"{base}/api/emails", headers=_headers(api_key))
        if resp.status_code >= 400:
            return None
        data = resp.json()
    except Exception:
        return None
    emails = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(emails, list):
        return None
    for item in emails:
        if not isinstance(item, dict):
            continue
        address = item.get("email") or item.get("address")
        if isinstance(address, str) and "@" in address:
            return address.rsplit("@", 1)[1].strip() or None
    return None


def secrets_token_hex_local() -> str:
    """Local-part generator without importing secrets at module top for clarity."""
    import secrets as _secrets

    return _secrets.token_hex(5).lower()


_normalize_proxy_config = normalize_proxy_config
