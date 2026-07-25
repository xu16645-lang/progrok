"""Extracted mailbox-helper build_xoauth2 to select_latest_code operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class MailboxContext:
    """Resolve helper services from the live compatibility facade."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


FETCH_LIMIT_DEFAULT = 5


def try_refresh_access_token(ctx: MailboxContext, endpoint, client_id, refresh_token):
    request_data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
        **(endpoint.get("extra_data") or {}),
    }
    started_at = ctx.time.monotonic()
    try:
        payload = ctx.post_form(endpoint["url"], request_data)
    except ctx.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return {
            "ok": False,
            "endpoint": endpoint["name"],
            "url": endpoint["url"],
            "status": getattr(exc, "code", None),
            "error": ctx.compact_text(detail or str(exc)),
            "elapsed_ms": int((ctx.time.monotonic() - started_at) * 1000),
        }
    except ctx.URLError as exc:
        return {
            "ok": False,
            "endpoint": endpoint["name"],
            "url": endpoint["url"],
            "status": None,
            "error": ctx.compact_text(f"Token request failed: {exc}"),
            "elapsed_ms": int((ctx.time.monotonic() - started_at) * 1000),
        }
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return {
            "ok": False,
            "endpoint": endpoint["name"],
            "url": endpoint["url"],
            "status": 200,
            "error": ctx.compact_text(
                payload.get("error_description")
                or payload.get("error")
                or ctx.json.dumps(payload, ensure_ascii=False)
            ),
            "elapsed_ms": int((ctx.time.monotonic() - started_at) * 1000),
        }
    return {
        "ok": True,
        "endpoint": endpoint["name"],
        "url": endpoint["url"],
        "elapsed_ms": int((ctx.time.monotonic() - started_at) * 1000),
        "payload": {
            "access_token": access_token,
            "next_refresh_token": str(payload.get("refresh_token") or "").strip(),
            "expires_in": payload.get("expires_in"),
        },
    }


def refresh_access_token(
    ctx: MailboxContext, client_id, refresh_token, strategy_names=None
):
    errors = []
    selected_endpoints = [
        ctx.TOKEN_ENDPOINTS[name]
        for name in strategy_names
        or ["live", "entra-consumers-delegated", "entra-common-delegated"]
        if name in ctx.TOKEN_ENDPOINTS
    ]
    ctx.log_info(
        f"token refresh start clientId={ctx.mask_secret(client_id)} refreshToken={ctx.mask_secret(refresh_token)} strategies={[item['name'] for item in selected_endpoints]}"
    )
    for endpoint in selected_endpoints:
        result = ctx.try_refresh_access_token(endpoint, client_id, refresh_token)
        if result["ok"]:
            ctx.log_info(
                f"token refresh success endpoint={result['endpoint']} elapsedMs={result['elapsed_ms']}"
            )
            return {
                "access_token": result["payload"]["access_token"],
                "next_refresh_token": result["payload"]["next_refresh_token"],
                "expires_in": result["payload"].get("expires_in"),
                "token_endpoint": result["endpoint"],
                "token_url": result["url"],
            }
        errors.append(result)
        ctx.log_info(
            f"token refresh failed endpoint={result['endpoint']} status={result['status']} elapsedMs={result['elapsed_ms']} detail={result['error']}"
        )
        ctx.log_token_refresh_failure_diagnosis(result)
    details = " | ".join(
        (f"{item['endpoint']}({item['status']}): {item['error']}" for item in errors)
    )
    raise RuntimeError(f"Token refresh failed on all endpoints: {details}")


def build_xoauth2(ctx: MailboxContext, email_addr, access_token):
    return f"user={email_addr}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def open_mailbox(ctx: MailboxContext, email_addr, access_token):
    client = ctx.imaplib.IMAP4_SSL(
        ctx.IMAP_HOST,
        ctx.IMAP_PORT,
        timeout=getattr(ctx, "IMAP_TIMEOUT_SECONDS", ctx.REQUEST_TIMEOUT_SECONDS),
    )
    client.authenticate(
        "XOAUTH2", lambda _: ctx.build_xoauth2(email_addr, access_token)
    )
    return client


def decode_mime_header(ctx: MailboxContext, value):
    if not value:
        return ""
    parts = []
    for chunk, charset in ctx.decode_header(value):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(charset or "utf-8", errors="ignore"))
        else:
            parts.append(str(chunk))
    return "".join(parts).strip()


def extract_text_part(ctx: MailboxContext, message):
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if "attachment" in str(part.get("Content-Disposition") or "").lower():
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="ignore").strip()
            if part.get_content_type() == "text/plain" and text:
                return text
            if part.get_content_type() == "text/html" and text:
                return ctx.re.sub(
                    "\\s+", " ", ctx.re.sub("<[^>]+>", " ", ctx.html.unescape(text))
                ).strip()
        return ""
    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="ignore").strip()
    if message.get_content_type() == "text/html":
        return ctx.re.sub(
            "\\s+", " ", ctx.re.sub("<[^>]+>", " ", ctx.html.unescape(text))
        ).strip()
    return text


def mailbox_candidates(ctx: MailboxContext, mailbox):
    normalized = str(mailbox or "INBOX").strip().lower()
    if normalized in {"junk", "junk email", "junk e-mail", "junkemail"}:
        return ["Junk", "Junk Email", "Junk E-Mail"]
    return ["INBOX"]


def normalize_mailbox_label(ctx: MailboxContext, mailbox):
    normalized = str(mailbox or "INBOX").strip().lower()
    if normalized in {"junk", "junk email", "junk e-mail", "junkemail"}:
        return "Junk"
    return "INBOX"


def normalize_mailbox_id(ctx: MailboxContext, mailbox):
    normalized = str(mailbox or "INBOX").strip().lower()
    if normalized in {"junk", "junk email", "junk e-mail", "junkemail"}:
        return "junkemail"
    return "inbox"


def select_mailbox(ctx: MailboxContext, client, mailbox):
    for candidate in ctx.mailbox_candidates(mailbox):
        status, _ = client.select(candidate)
        if status == "OK":
            return candidate
    raise RuntimeError(f"Mailbox not found: {mailbox}")


def to_timestamp_ms(ctx: MailboxContext, raw_date):
    if not raw_date:
        return 0
    try:
        parsed = ctx.parsedate_to_datetime(raw_date)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ctx.timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return 0


def to_iso_string(ctx: MailboxContext, timestamp_ms):
    if not timestamp_ms:
        return ""
    return (
        ctx.datetime.fromtimestamp(timestamp_ms / 1000, tz=ctx.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _collect_header_addresses(ctx: MailboxContext, parsed, *headers: str) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for header in headers:
        values = parsed.get_all(header, []) if hasattr(parsed, "get_all") else [parsed.get(header, "")]
        for raw in values or []:
            text = ctx.decode_mime_header(str(raw or ""))
            # A single header may contain multiple comma-separated addresses.
            for part in str(text or "").replace(";", ",").split(","):
                _name, addr = ctx.parseaddr(part)
                value = str(addr or part or "").strip().lower()
                if not value or "@" not in value or value in seen:
                    continue
                seen.add(value)
                addresses.append(value)
    return addresses


def normalize_message(ctx: MailboxContext, message_id, raw_bytes, mailbox):
    parsed = ctx.email.message_from_bytes(raw_bytes)
    sender_name, sender_addr = ctx.parseaddr(parsed.get("From", ""))
    subject = ctx.decode_mime_header(parsed.get("Subject", ""))
    body = ctx.extract_text_part(parsed)
    timestamp_ms = ctx.to_timestamp_ms(parsed.get("Date"))
    recipients = _collect_header_addresses(
        ctx,
        parsed,
        "To",
        "Delivered-To",
        "X-Original-To",
        "X-Forwarded-To",
        "Cc",
        "Envelope-To",
    )
    return {
        "id": str(message_id),
        "mailbox": mailbox,
        "subject": subject,
        "from": {
            "emailAddress": {
                "address": sender_addr.strip(),
                "name": sender_name.strip(),
            }
        },
        "toRecipients": [
            {"emailAddress": {"address": address}} for address in recipients
        ],
        "recipients": recipients,
        "bodyPreview": body[:500],
        "body": {"content": body},
        "receivedDateTime": ctx.to_iso_string(timestamp_ms),
        "receivedTimestamp": timestamp_ms,
    }


def fetch_messages(
    ctx: MailboxContext,
    email_addr,
    access_token,
    mailbox="INBOX",
    top=FETCH_LIMIT_DEFAULT,
):
    client = None
    logical_mailbox = ctx.normalize_mailbox_label(mailbox)
    try:
        client = ctx.open_mailbox(email_addr, access_token)
        ctx.select_mailbox(client, mailbox)
        status, data = client.search(None, "ALL")
        if status != "OK" or not data or (not data[0]):
            return {"mailbox": logical_mailbox, "messages": [], "count": 0}
        message_ids = data[0].split()
        selected_ids = list(
            reversed(
                message_ids[-max(1, min(int(top or ctx.FETCH_LIMIT_DEFAULT), 30)) :]
            )
        )
        messages = []
        # Fetch the selected messages in one IMAP round trip. Downloading each
        # RFC822 body separately makes every verification poll scale with `top`.
        fetch_status, fetch_data = client.fetch(b",".join(selected_ids), "(RFC822)")
        if fetch_status == "OK" and fetch_data:
            fallback_index = 0
            for item in fetch_data:
                if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
                    continue
                metadata = item[0] if isinstance(item[0], bytes) else b""
                match = ctx.re.match(rb"\s*(\d+)", metadata)
                if match:
                    message_id = match.group(1)
                elif fallback_index < len(selected_ids):
                    message_id = selected_ids[fallback_index]
                else:
                    continue
                fallback_index += 1
                messages.append(
                    ctx.normalize_message(
                        message_id.decode("utf-8", errors="ignore"),
                        item[1],
                        logical_mailbox,
                    )
                )
        return {
            "mailbox": logical_mailbox,
            "messages": messages,
            "count": len(messages),
        }
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def fetch_messages_for_mailboxes(
    ctx: MailboxContext, email_addr, access_token, mailboxes, top
):
    mailbox_results = []
    all_messages = []
    for mailbox in mailboxes or ["INBOX"]:
        result = ctx.fetch_messages(email_addr, access_token, mailbox=mailbox, top=top)
        mailbox_results.append(result)
        all_messages.extend(result["messages"])
    all_messages.sort(
        key=lambda item: int(item.get("receivedTimestamp") or 0), reverse=True
    )
    return {"mailboxResults": mailbox_results, "messages": all_messages}


def _graph_recipient_addresses(message: dict) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for key in ("toRecipients", "ccRecipients", "bccRecipients"):
        rows = message.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            email_addr = row.get("emailAddress") if isinstance(row.get("emailAddress"), dict) else {}
            value = str(email_addr.get("address") or "").strip().lower()
            if not value or "@" not in value or value in seen:
                continue
            seen.add(value)
            addresses.append(value)
    return addresses


def normalize_graph_message(ctx: MailboxContext, message, mailbox):
    sender = message.get("from", {}) or {}
    email_addr = sender.get("emailAddress", {}) if isinstance(sender, dict) else {}
    received = str(message.get("receivedDateTime") or "").strip()
    recipients = _graph_recipient_addresses(message if isinstance(message, dict) else {})
    return {
        "id": str(message.get("id") or message.get("internetMessageId") or "").strip(),
        "mailbox": mailbox,
        "subject": str(message.get("subject") or "").strip(),
        "from": {
            "emailAddress": {
                "address": str(email_addr.get("address") or "").strip(),
                "name": str(email_addr.get("name") or "").strip(),
            }
        },
        "toRecipients": [
            {"emailAddress": {"address": address}} for address in recipients
        ],
        "recipients": recipients,
        "bodyPreview": str(message.get("bodyPreview") or "").strip(),
        "receivedDateTime": received,
        "receivedTimestamp": int(
            ctx.datetime.fromisoformat(received.replace("Z", "+00:00")).timestamp()
            * 1000
        )
        if received
        else 0,
    }


def _outlook_recipient_addresses(message: dict) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for key in (
        "ToRecipients",
        "toRecipients",
        "CcRecipients",
        "ccRecipients",
        "BccRecipients",
        "bccRecipients",
    ):
        rows = message.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            email_addr = {}
            if isinstance(row.get("EmailAddress"), dict):
                email_addr = row["EmailAddress"]
            elif isinstance(row.get("emailAddress"), dict):
                email_addr = row["emailAddress"]
            value = str(
                email_addr.get("Address") or email_addr.get("address") or ""
            ).strip().lower()
            if not value or "@" not in value or value in seen:
                continue
            seen.add(value)
            addresses.append(value)
    return addresses


def normalize_outlook_message(ctx: MailboxContext, message, mailbox):
    sender = message.get("From", {}) or message.get("from", {}) or {}
    email_addr = sender.get("EmailAddress", {}) if isinstance(sender, dict) else {}
    if isinstance(sender, dict) and (not email_addr):
        email_addr = sender.get("emailAddress", {}) if isinstance(sender, dict) else {}
    received = str(
        message.get("ReceivedDateTime") or message.get("receivedDateTime") or ""
    ).strip()
    recipients = _outlook_recipient_addresses(message if isinstance(message, dict) else {})
    return {
        "id": str(message.get("Id") or message.get("id") or "").strip(),
        "mailbox": mailbox,
        "subject": str(message.get("Subject") or message.get("subject") or "").strip(),
        "from": {
            "emailAddress": {
                "address": str(
                    email_addr.get("Address") or email_addr.get("address") or ""
                ).strip(),
                "name": str(
                    email_addr.get("Name") or email_addr.get("name") or ""
                ).strip(),
            }
        },
        "toRecipients": [
            {"emailAddress": {"address": address}} for address in recipients
        ],
        "recipients": recipients,
        "bodyPreview": str(
            message.get("BodyPreview") or message.get("bodyPreview") or ""
        ).strip(),
        "receivedDateTime": received,
        "receivedTimestamp": int(
            ctx.datetime.fromisoformat(received.replace("Z", "+00:00")).timestamp()
            * 1000
        )
        if received
        else 0,
    }


def fetch_graph_messages(
    ctx: MailboxContext, access_token, mailbox="INBOX", top=FETCH_LIMIT_DEFAULT
):
    mailbox_id = ctx.normalize_mailbox_id(mailbox)
    query = ctx.urlencode(
        {
            "$top": max(1, min(int(top or ctx.FETCH_LIMIT_DEFAULT), 30)),
            "$select": "id,internetMessageId,subject,from,toRecipients,ccRecipients,bodyPreview,receivedDateTime",
            "$orderby": "receivedDateTime desc",
        }
    )
    url = f"{ctx.GRAPH_API_ORIGIN}/v1.0/me/mailFolders/{mailbox_id}/messages?{query}"
    try:
        _, payload = ctx.get_json(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
    except ctx.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Graph request failed: {detail or exc}") from exc
    except ctx.URLError as exc:
        raise RuntimeError(f"Graph request failed: {exc}") from exc
    messages = [
        ctx.normalize_graph_message(item, ctx.normalize_mailbox_label(mailbox))
        for item in payload.get("value") or []
    ]
    return {
        "mailbox": ctx.normalize_mailbox_label(mailbox),
        "messages": messages,
        "count": len(messages),
    }


def fetch_outlook_api_messages(
    ctx: MailboxContext, access_token, mailbox="INBOX", top=FETCH_LIMIT_DEFAULT
):
    mailbox_id = ctx.normalize_mailbox_id(mailbox)
    query = ctx.urlencode(
        {
            "$top": max(1, min(int(top or ctx.FETCH_LIMIT_DEFAULT), 30)),
            "$select": "Id,Subject,From,ToRecipients,CcRecipients,BodyPreview,ReceivedDateTime",
            "$orderby": "ReceivedDateTime desc",
        }
    )
    url = f"{ctx.OUTLOOK_API_ORIGIN}/api/v2.0/me/mailfolders/{mailbox_id}/messages?{query}"
    try:
        _, payload = ctx.get_json(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
        )
    except ctx.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Outlook API request failed: {detail or exc}") from exc
    except ctx.URLError as exc:
        raise RuntimeError(f"Outlook API request failed: {exc}") from exc
    messages = [
        ctx.normalize_outlook_message(item, ctx.normalize_mailbox_label(mailbox))
        for item in payload.get("value") or []
    ]
    return {
        "mailbox": ctx.normalize_mailbox_label(mailbox),
        "messages": messages,
        "count": len(messages),
    }


def collect_imap_messages(
    ctx: MailboxContext, email_addr, client_id, refresh_token, mailboxes, top
):
    token_payload = ctx.refresh_access_token(
        client_id,
        refresh_token,
        ["live", "entra-consumers-delegated", "entra-common-delegated"],
    )
    result = ctx.fetch_messages_for_mailboxes(
        email_addr, token_payload["access_token"], mailboxes, top
    )
    result["transport"] = "imap"
    result["token_payload"] = token_payload
    return result


def collect_graph_messages(
    ctx: MailboxContext, email_addr, client_id, refresh_token, mailboxes, top
):
    token_payload = ctx.refresh_access_token(
        client_id,
        refresh_token,
        ["entra-common-delegated", "entra-consumers-delegated", "entra-common-default"],
    )
    mailbox_results = [
        ctx.fetch_graph_messages(
            token_payload["access_token"], mailbox=mailbox, top=top
        )
        for mailbox in mailboxes
    ]
    messages = []
    for item in mailbox_results:
        messages.extend(item["messages"])
    messages.sort(
        key=lambda item: int(item.get("receivedTimestamp") or 0), reverse=True
    )
    return {
        "transport": "graph",
        "token_payload": token_payload,
        "mailboxResults": mailbox_results,
        "messages": messages,
    }


def collect_outlook_messages(
    ctx: MailboxContext, email_addr, client_id, refresh_token, mailboxes, top
):
    token_payload = ctx.refresh_access_token(
        client_id, refresh_token, ["entra-common-outlook", "entra-common-delegated"]
    )
    mailbox_results = [
        ctx.fetch_outlook_api_messages(
            token_payload["access_token"], mailbox=mailbox, top=top
        )
        for mailbox in mailboxes
    ]
    messages = []
    for item in mailbox_results:
        messages.extend(item["messages"])
    messages.sort(
        key=lambda item: int(item.get("receivedTimestamp") or 0), reverse=True
    )
    return {
        "transport": "outlook",
        "token_payload": token_payload,
        "mailboxResults": mailbox_results,
        "messages": messages,
    }


def collect_messages(
    ctx: MailboxContext, email_addr, client_id, refresh_token, mailboxes, top
):
    errors = []
    # IMAP is the authoritative mailbox transport for these delegated tokens.
    # Keep its socket timeout bounded, then use API transports as fallbacks.
    collectors = [
        ("imap", ctx.collect_imap_messages),
        ("graph", ctx.collect_graph_messages),
        ("outlook", ctx.collect_outlook_messages),
    ]
    for transport_name, collector in collectors:
        try:
            ctx.log_info(f"message collection start transport={transport_name}")
            result = collector(email_addr, client_id, refresh_token, mailboxes, top)
            ctx.log_info(
                f"message collection success transport={transport_name} tokenEndpoint={result['token_payload'].get('token_endpoint', '')}"
            )
            return result
        except Exception as exc:
            message = ctx.compact_text(str(exc), 600)
            errors.append(f"{transport_name}: {message}")
            ctx.log_info(
                f"message collection failed transport={transport_name} detail={message}"
            )
    raise RuntimeError(
        f"Message collection failed on all transports: {' | '.join(errors)}"
    )


def extract_code(ctx: MailboxContext, text, code_patterns=None):
    source = str(text or "")
    for pattern in code_patterns or []:
        try:
            source_pattern = str((pattern or {}).get("source") or "").strip()
            if not source_pattern:
                continue
            flags = str((pattern or {}).get("flags") or "").lower()
            re_flags = 0
            if "i" in flags:
                re_flags |= ctx.re.IGNORECASE
            if "m" in flags:
                re_flags |= ctx.re.MULTILINE
            if "s" in flags:
                re_flags |= ctx.re.DOTALL
            match = ctx.re.search(source_pattern, source, flags=re_flags)
            if not match:
                continue
            if match.lastindex:
                groups = [
                    str(match.group(group_index) or "").strip()
                    for group_index in range(1, match.lastindex + 1)
                ]
                candidate = "".join(group for group in groups if group)
                if candidate:
                    return candidate
            candidate = str(match.group(0) or "").strip()
            if candidate:
                return candidate
        except ctx.re.error:
            continue
    patterns = [
        "(?:代码为|验证码[^0-9]*?)[\\s：:]*(\\d{6})",
        "(?:log-?in\\s+code|enter\\s+this\\s+code)[^0-9]{0,24}(\\d{6})",
        "code(?:\\s+is|[\\s:])+(\\d{6})",
        "\\b(\\d{6})\\b",
    ]
    for pattern in patterns:
        match = ctx.re.search(pattern, source, flags=ctx.re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _message_recipient_addresses(message: dict) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    raw_list = message.get("recipients")
    if isinstance(raw_list, list):
        for value in raw_list:
            addr = str(value or "").strip().lower()
            if addr and "@" in addr and addr not in seen:
                seen.add(addr)
                addresses.append(addr)
    for key in ("toRecipients", "ccRecipients"):
        rows = message.get(key) or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            email_addr = row.get("emailAddress") if isinstance(row.get("emailAddress"), dict) else {}
            addr = str(email_addr.get("address") or "").strip().lower()
            if addr and "@" in addr and addr not in seen:
                seen.add(addr)
                addresses.append(addr)
    return addresses


def select_latest_code(
    ctx: MailboxContext,
    messages,
    sender_filters,
    subject_filters,
    exclude_codes,
    filter_after_timestamp,
    required_keywords=None,
    code_patterns=None,
    allow_time_fallback=True,
    recipient_filters=None,
):
    sender_keywords = [
        str(item).strip().lower() for item in sender_filters or [] if str(item).strip()
    ]
    subject_keywords = [
        str(item).strip().lower() for item in subject_filters or [] if str(item).strip()
    ]
    required_keyword_hints = [
        str(item).strip().lower()
        for item in required_keywords or []
        if str(item).strip()
    ]
    recipient_targets = [
        str(item).strip().lower()
        for item in recipient_filters or []
        if str(item).strip() and "@" in str(item)
    ]
    excluded = {str(item).strip() for item in exclude_codes or [] if str(item).strip()}

    def match_message(message, apply_time_filter):
        timestamp = int(message.get("receivedTimestamp") or 0)
        if (
            apply_time_filter
            and filter_after_timestamp
            and timestamp
            and (timestamp < int(filter_after_timestamp))
        ):
            return None
        if recipient_targets:
            recipients = _message_recipient_addresses(message if isinstance(message, dict) else {})
            # Fail closed: without recipient metadata we cannot prove the code
            # belongs to this registration alias, so reject the candidate.
            if not recipients or not any(target in recipients for target in recipient_targets):
                return None
        sender = str(
            message.get("from", {}).get("emailAddress", {}).get("address", "")
        ).lower()
        subject = str(message.get("subject", ""))
        preview = str(message.get("bodyPreview", ""))
        combined = " ".join([sender, subject.lower(), preview.lower()])
        code = ctx.extract_code(
            " ".join([subject, preview, sender]), code_patterns=code_patterns
        )
        if not code:
            body_content = ctx.get_message_body_content(message)
            if body_content:
                code = ctx.extract_code(
                    " ".join([subject, body_content, sender]),
                    code_patterns=code_patterns,
                )
        if not code or code in excluded:
            return None
        sender_ok = bool(sender_keywords) and any(
            (keyword in combined for keyword in sender_keywords)
        )
        subject_ok = bool(subject_keywords) and any(
            (keyword in combined for keyword in subject_keywords)
        )
        keyword_ok = bool(required_keyword_hints) and any(
            (keyword in combined for keyword in required_keyword_hints)
        )
        if (
            (sender_keywords or subject_keywords or required_keyword_hints)
            and (not sender_ok)
            and (not subject_ok)
            and (not keyword_ok)
        ):
            return None
        return {"code": code, "message": message}

    fallback_modes = [False, True] if allow_time_fallback else [False]
    for use_time_fallback in fallback_modes:
        matched = []
        for message in messages:
            result = match_message(message, apply_time_filter=not use_time_fallback)
            if result:
                matched.append(result)
        if matched:
            matched.sort(
                key=lambda item: int(item["message"].get("receivedTimestamp") or 0),
                reverse=True,
            )
            best = matched[0]
            return {
                "code": best["code"],
                "message": best["message"],
                "usedTimeFallback": use_time_fallback,
            }
    return {"code": "", "message": None, "usedTimeFallback": False}
