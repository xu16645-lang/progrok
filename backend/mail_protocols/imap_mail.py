"""Generic IMAP catch-all mailbox protocol."""

from __future__ import annotations

import email
import imaplib
import random
import re
import ssl
from email import policy
from email.utils import getaddresses
from typing import Any
from urllib.parse import unquote, urlparse

from .common import _extract_codes_and_links


def _pick_domain(value: str | None) -> str:
    domains = [
        item.strip().lstrip("@").strip(".").lower()
        for item in re.split(r"[,;\s]+", value or "")
        if item.strip()
    ]
    domains = list(dict.fromkeys(domains))
    if not domains:
        raise ValueError("IMAP mailbox domain is required")
    invalid = [
        domain
        for domain in domains
        if not re.fullmatch(
            r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            domain,
        )
    ]
    if invalid:
        raise ValueError(f"Invalid IMAP mailbox domain: {invalid[0]}")
    return random.choice(domains)


def _imap_config(base_url: str | None, api_key: str | None) -> dict[str, Any]:
    raw_url = (base_url or "").strip()
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"imap", "imaps"} or not parsed.hostname:
        raise ValueError("IMAP address must use imap:// or imaps://")

    secret = (api_key or "").strip()
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if secret:
        if ":" not in secret:
            raise ValueError("IMAP API Key must use username:password format")
        username, password = secret.split(":", 1)
    if not username or not password:
        raise ValueError("IMAP credentials are missing")

    secure = parsed.scheme == "imaps"
    return {
        "host": parsed.hostname,
        "port": parsed.port or (993 if secure else 143),
        "secure": secure,
        "username": username.strip(),
        "password": password,
        "folder": unquote((parsed.path or "/INBOX").lstrip("/")) or "INBOX",
    }


def _imap_connect(config: dict[str, Any]) -> imaplib.IMAP4:
    if config["secure"]:
        client = imaplib.IMAP4_SSL(
            config["host"],
            config["port"],
            ssl_context=ssl.create_default_context(),
        )
    else:
        client = imaplib.IMAP4(config["host"], config["port"])
    client.login(config["username"], config["password"])
    status, _ = client.select(config["folder"], readonly=True)
    if status != "OK":
        client.logout()
        raise RuntimeError(f"IMAP folder is unavailable: {config['folder']}")
    return client


def imap_create_mailbox(
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
    """Create a virtual address that is received by an IMAP catch-all inbox."""
    del expiry_ms, proxy, proxy_username, proxy_password
    dom = _pick_domain(domain)
    local = re.sub(r"[^a-z0-9._+-]", "", (name or "").strip().lower())
    if not local:
        raise ValueError("IMAP mailbox name is required")

    config = _imap_config(base_url, api_key)
    client = _imap_connect(config)
    try:
        client.noop()
    finally:
        client.logout()

    address = f"{local}@{dom}"
    return {
        "id": address,
        "email": address,
        "token": "",
        "provider": "imap",
    }


def _message_recipients(message: email.message.Message) -> set[str]:
    values: list[str] = []
    for header in (
        "to",
        "cc",
        "delivered-to",
        "envelope-to",
        "x-envelope-to",
        "x-original-to",
        "original-recipient",
    ):
        values.extend(message.get_all(header, []))
    recipients = {address.lower() for _, address in getaddresses(values) if address}
    # Some MTAs only preserve the catch-all target in a Received header.
    for received in message.get_all("received", []):
        recipients.update(
            match.lower()
            for match in re.findall(r"\bfor\s+<?([^>;\s]+@[^>;\s]+)>?", received, re.I)
        )
    return recipients


def _message_content(message: email.message.Message) -> tuple[str, str]:
    texts: list[str] = []
    htmls: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = (part.get_content_type() or "").lower()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            value = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                continue
            value = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        if not isinstance(value, str):
            continue
        (htmls if content_type == "text/html" else texts).append(value)
    return "\n".join(texts), "\n".join(htmls)


def imap_fetch_messages(
    email_id: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    include_details: bool = True,
    address: str | None = None,
    token: str | None = None,
) -> list[dict[str, Any]]:
    """Read recent messages addressed to one virtual catch-all address."""
    del include_details, token
    target = (address or email_id or "").strip().lower()
    if "@" not in target:
        return []

    config = _imap_config(base_url, api_key)
    client = _imap_connect(config)
    try:
        status, data = client.uid("search", None, "ALL")
        if status != "OK" or not data:
            return []
        uids = data[0].split()[-100:]
        messages: list[dict[str, Any]] = []
        for uid in reversed(uids):
            status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw = next(
                (
                    item[1]
                    for item in payload
                    if isinstance(item, tuple) and isinstance(item[1], bytes)
                ),
                None,
            )
            if raw is None:
                continue
            message = email.message_from_bytes(raw, policy=policy.default)
            if target not in _message_recipients(message):
                continue
            text, html = _message_content(message)
            content = "\n".join((str(message.get("subject") or ""), text, html))
            messages.append(
                {
                    "id": uid.decode(errors="replace"),
                    "subject": str(message.get("subject") or ""),
                    "from": str(message.get("from") or ""),
                    "to": str(message.get("to") or ""),
                    "date": str(message.get("date") or ""),
                    "text": text,
                    "html": html,
                    "content": content,
                    "extracted": _extract_codes_and_links(content),
                }
            )
            if len(messages) >= 20:
                break
        return messages
    finally:
        client.logout()
