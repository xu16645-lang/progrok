"""Adapter: ChatGPT browser-based registration → multi-account pool.

Provides the same interface as grok_build_adapter for the ChatGPT registration flow.
Uses browser automation (chatgpt_browser.py), MoeMail/YYDS for email verification,
and chatgpt_session_to_auth.py for session → auth.json conversion.

Registration flow per account:
1. Create email mailbox via MoeMail/YYDS
2. Open ChatGPT in browser → signup with email
3. Wait for verification code in mailbox → fill in browser
4. Fill random name + birthdate → complete signup
5. Extract session from chatgpt.com/api/auth/session
6. Convert session to auth.json → save locally
7. Auto-import to CPA/Sub2API if configured
"""

from __future__ import annotations

import json  # noqa: F401 - exposed through RegistrationContext(globals())
import os
import secrets
import string
import sys  # noqa: F401 - exposed through RegistrationContext(globals())
import threading
import time
import uuid  # noqa: F401 - exposed through RegistrationContext(globals())
from pathlib import Path
from typing import Any

import httpx
from chatgpt_registration import flow as _flow
from chatgpt_registration import history as _history
from chatgpt_registration import operations as _operations
from chatgpt_registration import probe as _probe
from chatgpt_registration import worker as _worker
from registration_state import clear_state, load_state, save_state  # noqa: F401

BACKEND_DIR = Path(__file__).resolve().parent
APP_DIR = BACKEND_DIR.parent
RUNTIME_DATA_DIR = APP_DIR / "runtime" / "data"
CHATGPT_SESSIONS_DIR = RUNTIME_DATA_DIR / "chatgpt_sessions"
CHATGPT_ADAPTER_BUILD = "2026-07-22-chatgpt-v1"
CHATGPT_DEFAULT_PROBE_MODEL = "gpt-5.5"
CHATGPT_PROBE_URL = "https://chatgpt.com/backend-api/codex/responses"
CHATGPT_PROBE_TIMEOUT = httpx.Timeout(75.0, connect=10.0)

# Concurrency limits
MAX_CONCURRENCY = int(os.environ.get("CHATGPT_REG_MAX_CONCURRENCY", "5") or 5)
DEFAULT_CONCURRENCY = int(os.environ.get("CHATGPT_REG_CONCURRENCY", "2") or 2)


def _registration_context() -> _flow.RegistrationContext:
    """Expose the live adapter namespace to extracted workflows."""
    return _flow.RegistrationContext(globals())


# --------------------------------------------------------------------------- #
# Session / batch state (durable monitor history)
# --------------------------------------------------------------------------- #
def _session_filename_part(value: Any, fallback: str) -> str:
    return _history.session_filename_part(_registration_context(), value, fallback)


def _save_original_chatgpt_session(
    session_data: dict[str, Any],
    *,
    email: str,
    session_id: str,
) -> str:
    return _history.save_original_session(
        _registration_context(), session_data, email=email, session_id=session_id
    )


def _saved_session_path(session_file: Any) -> Path | None:
    return _history.saved_session_path(_registration_context(), session_file)


def _load_original_chatgpt_session(session_file: Any) -> dict[str, Any]:
    return _history.load_original_session(_registration_context(), session_file)


def _session_data_for(sess: dict[str, Any]) -> dict[str, Any]:
    return _history.session_data_for(_registration_context(), sess)


def _recover_saved_chatgpt_history() -> tuple[
    dict[str, dict[str, Any]], dict[str, dict[str, Any]]
]:
    return _history.recover_saved_history(_registration_context())


_sessions, _batches = load_state("chatgpt")
if not _sessions and not _batches:
    _sessions, _batches = _recover_saved_chatgpt_history()
_lock = threading.RLock()
_active_batch_runners: dict[str, bool] = {}
_adapter_ready: bool | None = None
_adapter_error: str | None = None


def _now() -> float:
    return time.time()


def _persist_monitor_state() -> None:
    with _lock:
        sessions = {sid: dict(session) for sid, session in _sessions.items()}
        batches = {bid: dict(batch) for bid, batch in _batches.items()}
    save_state("chatgpt", sessions, batches)


_persist_timer: threading.Timer | None = None
_persist_timer_lock = threading.Lock()


def _request_monitor_persist() -> None:
    global _persist_timer
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    with _persist_timer_lock:
        if _persist_timer is not None and _persist_timer.is_alive():
            return
        _persist_timer = threading.Timer(0.25, _persist_monitor_state)
        _persist_timer.daemon = True
        _persist_timer.start()


_TERMINAL_STATUSES = frozenset(
    {
        "imported",
        "success",
        "completed",
        "error",
        "failed",
        "cancelled",
        "stopped",
        "protocol_error",
        "account_error",
    }
)


def _is_cancel_status(status: str | None) -> bool:
    return str(status or "").lower() in ("cancelled", "stopped", "stopping")


def _session_cancel_requested(sess: dict[str, Any] | None) -> bool:
    if not isinstance(sess, dict):
        return False
    if sess.get("cancel_requested"):
        return True
    return _is_cancel_status(sess.get("status"))


class _RegCancelled(Exception):
    """Cooperative cancel for in-flight registration workers."""


def _compact_session(sess: dict[str, Any]) -> dict[str, Any]:
    """Strip internal fields before returning to API."""
    out = dict(sess)
    for key in list(out):
        if isinstance(key, str) and key.startswith("_"):
            out.pop(key, None)
    out.pop("password", None)
    out.pop("session_data", None)
    # Drop full auth payload from list responses
    out.pop("auth_json", None)
    return out


def _append_session_event(
    sess: dict[str, Any], status: str, message: str, *, at: float | None = None
) -> None:
    events = [
        dict(item) for item in (sess.get("events") or []) if isinstance(item, dict)
    ]
    event = {
        "at": float(at or _now()),
        "status": str(status or "unknown"),
        "message": str(message or ""),
    }
    if events and all(events[-1].get(k) == event[k] for k in ("status", "message")):
        return
    events.append(event)
    sess["events"] = events[-60:]
    _request_monitor_persist()


def _chatgpt_probe_error(response: httpx.Response) -> str:
    return _probe._chatgpt_probe_error(_registration_context(), response)


def _extract_chatgpt_output_text(payload: dict[str, Any]) -> str:
    return _probe._extract_chatgpt_output_text(_registration_context(), payload)


def _parse_chatgpt_probe_response(response: httpx.Response) -> dict[str, Any]:
    return _probe._parse_chatgpt_probe_response(_registration_context(), response)


def probe_chatgpt_session(
    session_data: dict[str, Any],
    model: str = CHATGPT_DEFAULT_PROBE_MODEL,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _probe.probe_chatgpt_session(
        _registration_context(), session_data, model, proxy=proxy, client=client
    )


def probe_chatgpt_agent_identity(
    identity: dict[str, Any],
    model: str = CHATGPT_DEFAULT_PROBE_MODEL,
    *,
    proxy: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _probe.probe_chatgpt_agent_identity(
        _registration_context(), identity, model, proxy=proxy, client=client
    )


def _probe_registration_session(
    session_id: str, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _probe._probe_registration_session(
        _registration_context(), session_id, config
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def registration_available() -> dict[str, Any]:
    """Non-raising health probe for admin UI / startup logs."""
    global _adapter_ready, _adapter_error
    if _adapter_ready is not None:
        return {
            "ok": _adapter_ready,
            "available": _adapter_ready,
            "engine": "chatgpt-browser",
            "adapter_build": CHATGPT_ADAPTER_BUILD,
            "error": _adapter_error,
        }

    # Check browser availability
    try:
        from chatgpt_browser import _ensure_camoufox

        _ensure_camoufox()
        _adapter_ready = True
        _adapter_error = None
    except Exception as e:
        _adapter_ready = False
        _adapter_error = str(e)

    return {
        "ok": bool(_adapter_ready),
        "available": bool(_adapter_ready),
        "engine": "chatgpt-browser",
        "adapter_build": CHATGPT_ADAPTER_BUILD,
        "error": _adapter_error,
    }


# --------------------------------------------------------------------------- #
# Mail receiver (reuses the same infrastructure as Grok adapter)
# --------------------------------------------------------------------------- #
def _make_email_receiver(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    should_cancel: Any | None = None,
):
    """Create a temporary email mailbox for receiving ChatGPT verification codes."""
    if str(mail_provider or "").strip().lower() == "hotmail_local":
        from hotmail_local import create_receiver

        return create_receiver(hotmail_local_base_url, should_cancel=should_cancel)

    from moemail import (
        create_mailbox,
        extract_verification_codes,
        fetch_messages,
        normalize_mail_provider,
    )
    from config import (
        MOEMAIL_API_KEY,
        MOEMAIL_BASE_URL,
        MOEMAIL_DOMAIN,
        MOEMAIL_EXPIRY_MS,
    )

    key = (api_key or MOEMAIL_API_KEY or "").strip()
    if not key:
        raise ValueError(
            "Mail API key missing. Configure in settings or set environment variables."
        )

    base = (base_url or MOEMAIL_BASE_URL or "").rstrip("/")
    prov = normalize_mail_provider(mail_provider, base_url=base)

    # Domain handling per provider
    if prov in {"yyds", "gptmail", "cfmail", "stalwart"}:
        dom = (domain or "").strip().lstrip("@").strip(".")
    else:
        dom = (domain or MOEMAIL_DOMAIN or "").strip().lstrip("@").strip(".")

    # Generate random local-part
    alphabet = string.ascii_lowercase + string.digits
    pre = "".join(secrets.choice(alphabet) for _ in range(12))

    mailbox = create_mailbox(
        provider=prov,
        name=pre,
        domain=dom or None,
        expiry_ms=expiry_ms if expiry_ms is not None else MOEMAIL_EXPIRY_MS,
        api_key=key,
        base_url=base,
    )
    email_id = mailbox["id"]
    address = mailbox["email"]
    token = str(mailbox.get("token") or "")

    class _MailReceiver:
        def __init__(
            self,
            email: str,
            email_id: str,
            api_key: str | None,
            base_url: str | None,
            *,
            provider: str,
            token: str = "",
        ):
            self.email = email
            self.email_id = email_id
            self.api_key = api_key
            if provider == "yyds":
                default_base = "https://maliapi.215.im"
            elif provider == "gptmail":
                default_base = "https://mail.chatgpt.org.uk"
            elif provider == "cfmail":
                default_base = "https://temp-email-api.awsl.uk"
            elif provider == "stalwart":
                default_base = ""
            else:
                default_base = "https://moemail.521884.xyz"
            self.base_url = base_url or default_base
            self.provider = provider
            self.token = token

        def wait_for_code(
            self,
            timeout: float = 120,
            *,
            should_cancel=None,
            poll_interval: float | None = None,
            exclude_codes: set[str] | None = None,
        ) -> str:
            deadline = time.time() + float(timeout or 120)
            poll = float(poll_interval if poll_interval is not None else 1.0)
            poll = max(0.4, min(poll, 2.0))
            consecutive_fetch_failures = 0

            while time.time() < deadline:
                if callable(should_cancel) and should_cancel():
                    raise _RegCancelled("cancelled while waiting for email code")
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
                            )
                        )
                        extracted = item.get("extracted") or {}
                        codes = (
                            extract_verification_codes(text)
                            or extracted.get("codes")
                            or []
                        )
                        for code in codes:
                            clean = str(code).strip()
                            if (
                                len(clean) == 6
                                and clean.isdigit()
                                and clean not in (exclude_codes or set())
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
                        raise _RegCancelled("cancelled while waiting for email code")
                    time.sleep(min(0.25, poll - slept))
                    slept += min(0.25, poll - slept)
                poll = min(2.0, poll + 0.15)
            raise RuntimeError("timeout waiting for ChatGPT email verification code")

    return address, _MailReceiver(
        address, email_id, api_key=key, base_url=base, provider=prov, token=token
    )


# --------------------------------------------------------------------------- #
# Proxy resolution
# --------------------------------------------------------------------------- #
def _proxy_url() -> str:
    try:
        from proxy_pool import resolve_proxy_for_request

        return resolve_proxy_for_request(fallback_env=True) or ""
    except Exception:
        return ""


def _proxy_pool(
    proxy_text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
) -> list[str]:
    try:
        from proxy_pool import parse_proxy_pool

        pool = parse_proxy_pool(
            proxy_text, username=username, password=password, fallback_env=True
        )
        if pool:
            return pool
    except Exception:
        pass
    one = (proxy_text or "").strip() or _proxy_url()
    return [one] if one else []


def _pick_proxy_from_pool(
    pool: list[str], *, strategy: str | None = None, index: int | None = None
) -> str:
    try:
        from proxy_pool import pick_proxy

        return pick_proxy(pool, strategy=strategy or "round_robin", index=index) or ""
    except Exception:
        return pool[0] if pool else ""


def _proxy_for_browser(proxy_url: str) -> dict[str, str] | None:
    """Convert proxy URL to browser context proxy dict."""
    if not proxy_url or not proxy_url.strip():
        return None
    url = proxy_url.strip()
    # Ensure scheme
    if "://" not in url:
        url = f"http://{url}"
    return {"server": url}


# --------------------------------------------------------------------------- #
# Registration session management
# --------------------------------------------------------------------------- #
def _prepare_registration_session(
    *,
    proxy: str,
    moemail_api_key: str | None = None,
    moemail_base_url: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
    batch_total: int | None = None,
    start_delay: float = 0.0,
    post_registration: dict[str, Any] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    return _flow._prepare_registration_session(
        _registration_context(),
        proxy=proxy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        batch_id=batch_id,
        batch_index=batch_index,
        batch_total=batch_total,
        start_delay=start_delay,
        post_registration=post_registration,
        headless=headless,
    )


def _run_registration(
    sid: str, proxy: str, receiver: Any, browser_runtime: Any = None
) -> None:
    return _worker._run_registration(
        _registration_context(), sid, proxy, receiver, browser_runtime
    )


def _start_one_registration(
    *,
    proxy: str,
    moemail_api_key: str | None = None,
    moemail_base_url: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
    batch_total: int | None = None,
    start_delay: float = 0.0,
    post_registration: dict[str, Any] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    return _flow._start_one_registration(
        _registration_context(),
        proxy=proxy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        batch_id=batch_id,
        batch_index=batch_index,
        batch_total=batch_total,
        start_delay=start_delay,
        post_registration=post_registration,
        headless=headless,
    )


def _snapshot_reg_config(
    *,
    proxy: str,
    moemail_api_key: str | None,
    moemail_base_url: str | None,
    domain: str | None,
    expiry_ms: int | None,
    mail_provider: str | None,
    hotmail_local_base_url: str | None,
    concurrency: int,
    stagger_ms: int,
    post_registration: dict[str, Any] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    return _flow._snapshot_reg_config(
        _registration_context(),
        proxy=proxy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        concurrency=concurrency,
        stagger_ms=stagger_ms,
        post_registration=post_registration,
        headless=headless,
    )


def start_registration(
    *,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    proxy_strategy: str | None = None,
    moemail_api_key: str | None = None,
    moemail_base_url: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    count: int | None = None,
    concurrency: int | None = None,
    stagger_ms: int | None = None,
    post_registration: dict[str, Any] | None = None,
    start_paused: bool = False,
    headless: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    return _flow.start_registration(
        _registration_context(),
        proxy=proxy,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_strategy=proxy_strategy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        count=count,
        concurrency=concurrency,
        stagger_ms=stagger_ms,
        post_registration=post_registration,
        start_paused=start_paused,
        headless=headless,
        **kwargs,
    )


def _batch_spawner(
    batch_id: str,
    total: int,
    workers: int,
    stagger_ms: int,
    proxy_pool: list[str],
    proxy_strat: str,
    moemail_api_key: str | None,
    moemail_base_url: str | None,
    domain: str | None,
    expiry_ms: int | None,
    mail_provider: str | None,
    hotmail_local_base_url: str | None,
    post_registration: dict[str, Any] | None,
    headless: bool,
) -> None:
    return _flow._batch_spawner(
        _registration_context(),
        batch_id,
        total,
        workers,
        stagger_ms,
        proxy_pool,
        proxy_strat,
        moemail_api_key,
        moemail_base_url,
        domain,
        expiry_ms,
        mail_provider,
        hotmail_local_base_url,
        post_registration,
        headless,
    )


# --------------------------------------------------------------------------- #
# Post-registration pipeline (import)
# --------------------------------------------------------------------------- #
def _enqueue_import(sid: str) -> None:
    return _operations._enqueue_import(_registration_context(), sid)


def _retry_import_now(sid: str, config: dict[str, Any]) -> dict[str, Any]:
    return _operations._retry_import_now(_registration_context(), sid, config)


def retry_registration_import(
    session_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    return _operations.retry_registration_import(
        _registration_context(), session_id, config
    )


def retry_registration_batch_import(
    batch_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    return _operations.retry_registration_batch_import(
        _registration_context(), batch_id, config
    )


def retry_registration_probe(session_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return _operations.retry_registration_probe(
        _registration_context(), session_id, config
    )


def retry_registration_batch_probe(
    batch_id: str, config: dict[str, Any]
) -> dict[str, Any]:
    return _operations.retry_registration_batch_probe(
        _registration_context(), batch_id, config
    )


def pause_registration_batch_probe(batch_id: str) -> dict[str, Any]:
    return _operations.pause_registration_batch_probe(_registration_context(), batch_id)


def resume_registration_batch_probe(batch_id: str) -> dict[str, Any]:
    return _operations.resume_registration_batch_probe(
        _registration_context(), batch_id
    )


def list_registration_sessions() -> dict[str, Any]:
    return _operations.list_registration_sessions(_registration_context())


def get_registration_session(session_id: str) -> dict[str, Any] | None:
    return _operations.get_registration_session(_registration_context(), session_id)


def get_registration_batch(batch_id: str) -> dict[str, Any] | None:
    return _operations.get_registration_batch(_registration_context(), batch_id)


def stop_registration_session(session_id: str) -> dict[str, Any]:
    return _operations.stop_registration_session(_registration_context(), session_id)


def stop_registration_batch(batch_id: str) -> dict[str, Any]:
    return _operations.stop_registration_batch(_registration_context(), batch_id)


def pause_registration_batch(batch_id: str) -> dict[str, Any]:
    return _operations.pause_registration_batch(_registration_context(), batch_id)


def resume_registration_batch(batch_id: str, force: bool = False) -> dict[str, Any]:
    return _operations.resume_registration_batch(
        _registration_context(), batch_id, force
    )


def reset_registration_monitor() -> dict[str, Any]:
    return _operations.reset_registration_monitor(_registration_context())


def probe_local_solver(url: str | None = None, timeout: float = 0.35) -> dict[str, Any]:
    return _operations.probe_local_solver(_registration_context(), url, timeout)


def get_registration_performance_profile(
    provider: str = "", local_solver_url: str = ""
) -> dict[str, Any]:
    return _operations.get_registration_performance_profile(
        _registration_context(), provider, local_solver_url
    )


def wait_pipeline_stagger(phase: str, delay_ms: int | float | None) -> None:
    return _operations.wait_pipeline_stagger(_registration_context(), phase, delay_ms)
