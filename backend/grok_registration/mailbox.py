"""Mailbox and proxy adapters used by Grok registration workers."""
from __future__ import annotations

import secrets
import string
import time
from typing import Any


def make_email_receiver(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    prefix: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    cancelled_error: type[Exception],
) -> tuple[str, Any]:
    """Create a mailbox receiver that extracts xAI verification codes."""
    if str(mail_provider or "").strip().lower() == "hotmail_local":
        from hotmail_local import create_receiver

        return create_receiver(
            hotmail_local_base_url,
            verification_target="xai",
        )

    from config import MOEMAIL_API_KEY, MOEMAIL_BASE_URL, MOEMAIL_DOMAIN, MOEMAIL_EXPIRY_MS
    from moemail import create_mailbox, fetch_messages, normalize_mail_provider

    key = (api_key or MOEMAIL_API_KEY or "").strip()
    if not key:
        raise ValueError(
            "Mail API key missing. Set GROK2API_MOEMAIL_API_KEY or pass api_key."
        )
    base = (base_url or MOEMAIL_BASE_URL).rstrip("/")
    provider = normalize_mail_provider(mail_provider, base_url=base)
    if provider in {"yyds", "gptmail", "cfmail", "stalwart"}:
        mailbox_domain = (domain or "").strip().lstrip("@").strip(".")
    else:
        mailbox_domain = (domain or MOEMAIL_DOMAIN or "").strip().lstrip("@").strip(".")
    alphabet = string.ascii_lowercase + string.digits
    mailbox_prefix = "".join(secrets.choice(alphabet) for _ in range(12))

    mailbox = create_mailbox(
        provider=provider,
        name=mailbox_prefix,
        domain=mailbox_domain or None,
        expiry_ms=expiry_ms if expiry_ms is not None else MOEMAIL_EXPIRY_MS,
        api_key=key,
        base_url=base,
    )
    email_id = mailbox["id"]
    address = mailbox["email"]
    token = str(mailbox.get("token") or "")

    class MailReceiver:
        def __init__(
            self,
            email: str,
            mailbox_id: str,
            mailbox_api_key: str | None,
            mailbox_base_url: str | None,
            *,
            provider_name: str,
            mailbox_token: str = "",
        ):
            self.email = email
            self.email_id = mailbox_id
            self.api_key = mailbox_api_key
            if provider_name == "yyds":
                default_base = "https://maliapi.215.im"
            elif provider_name == "gptmail":
                default_base = "https://mail.chatgpt.org.uk"
            elif provider_name == "cfmail":
                default_base = "https://temp-email-api.awsl.uk"
            elif provider_name == "stalwart":
                default_base = ""
            else:
                default_base = "https://moemail.521884.xyz"
            self.base_url = mailbox_base_url or default_base
            self.provider = provider_name
            self.token = mailbox_token

        def wait_for_code(
            self,
            timeout: float = 120,
            *,
            should_cancel=None,
            poll_interval: float | None = None,
            exclude_codes: set[str] | None = None,
        ) -> str:
            import re as _re

            deadline = time.time() + float(timeout or 120)
            excluded = {
                _re.sub(r"[^A-Za-z0-9]", "", str(code or "")).upper()
                for code in (exclude_codes or set())
                if str(code or "").strip()
            }
            poll = float(poll_interval if poll_interval is not None else 1.0)
            poll = max(0.4, min(poll, 2.0))
            consecutive_fetch_failures = 0
            while time.time() < deadline:
                if callable(should_cancel) and should_cancel():
                    raise cancelled_error("cancelled while waiting for email code")
                try:
                    messages = fetch_messages(
                        self.email_id,
                        provider=self.provider,
                        api_key=self.api_key,
                        base_url=self.base_url,
                        include_details=True,
                        address=self.email,
                        token=self.token or None,
                    )
                    for item in messages:
                        text = "\n".join(
                            str(item.get(key) or "")
                            for key in (
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
                        match = _re.search(
                            r"\b([A-Z0-9]{3})-([A-Z0-9]{3})\b", text, flags=_re.I
                        )
                        if match:
                            code = "".join(match.groups()).upper()
                            if code not in excluded:
                                return code
                        match2 = _re.search(r"\b([A-Z0-9]{6})\b", text, flags=_re.I)
                        if match2 and "x.ai" in text.lower():
                            code = match2.group(1).upper()
                            if code not in excluded:
                                return code
                        extracted = item.get("extracted") or {}
                        for code in extracted.get("codes") or []:
                            clean = str(code).replace("-", "").strip().upper()
                            if (
                                len(clean) == 6
                                and _re.fullmatch(r"[A-Z0-9]{6}", clean)
                                and clean not in excluded
                            ):
                                return clean
                    consecutive_fetch_failures = 0
                except Exception as exc:
                    consecutive_fetch_failures += 1
                    if consecutive_fetch_failures >= 3:
                        raise RuntimeError(
                            "mailbox polling failed after 3 consecutive attempts: "
                            f"{str(exc)[:240]}"
                        ) from exc
                slept = 0.0
                while slept < poll:
                    if callable(should_cancel) and should_cancel():
                        raise cancelled_error("cancelled while waiting for email code")
                    step = min(0.25, poll - slept)
                    time.sleep(step)
                    slept += step
                poll = min(2.0, poll + 0.15)
            raise RuntimeError("timeout waiting for xAI email verification code")

    return address, MailReceiver(
        address,
        email_id,
        mailbox_api_key=key,
        mailbox_base_url=base,
        provider_name=provider,
        mailbox_token=token,
    )


def proxy_url() -> str:
    """Pick one proxy from the configured pool."""
    try:
        from proxy_pool import resolve_proxy_for_request

        return resolve_proxy_for_request(fallback_env=True) or ""
    except Exception:
        try:
            from config import XAI_PROXY
            from moemail import normalize_proxy_config

            config = normalize_proxy_config(XAI_PROXY or None)
            return config["proxy"] if config else ""
        except Exception:
            return ""


def proxy_pool(
    proxy_text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
) -> list[str]:
    """Parse multi-line proxy text into full proxy URLs."""
    try:
        from proxy_pool import parse_proxy_pool

        pool = parse_proxy_pool(
            proxy_text,
            username=username,
            password=password,
            fallback_env=True,
        )
        if pool:
            return pool
    except Exception:
        pass
    one = (proxy_text or "").strip() or proxy_url()
    return [one] if one else []


def pick_proxy_from_pool(
    pool: list[str],
    *,
    strategy: str | None = None,
    index: int | None = None,
) -> str:
    try:
        from proxy_pool import normalize_proxy_strategy, pick_proxy

        selected_strategy = strategy
        if not selected_strategy:
            try:
                from config import XAI_PROXY_STRATEGY as configured_strategy
            except Exception:
                configured_strategy = "round_robin"
            selected_strategy = configured_strategy
        return (
            pick_proxy(
                pool,
                strategy=normalize_proxy_strategy(selected_strategy),
                index=index,
            )
            or ""
        )
    except Exception:
        return pool[0] if pool else ""


def proxy_for_browser(proxy_url_value: str | None) -> dict[str, str] | None:
    """Convert a proxy URL into a Playwright/Camoufox proxy dictionary."""
    raw = str(proxy_url_value or "").strip()
    if not raw:
        return None
    try:
        from urllib.parse import unquote, urlparse

        source = raw if "://" in raw else f"http://{raw}"
        parsed = urlparse(source)
        if not parsed.hostname:
            return {"server": source}
        server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result
    except Exception:
        return {"server": raw if "://" in raw else f"http://{raw}"}
