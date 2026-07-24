"""Shared constants and response helpers for account pipeline protocols."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx


DEFAULT_PROBE_MODEL = "grok-4.5"
PROBE_PERMISSION_RETRY_DELAYS = (5.0, 10.0, 20.0)
PROBE_RESPONSE_TIMEOUT = httpx.Timeout(75.0, connect=10.0)
PROBE_MODELS_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
PROBE_TRANSIENT_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
CHATGPT_SUB2API_MODELS = (
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
)


def cpa_api_base_url(value: str) -> str:
    """Accept either the CPA site root or its management page URL."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if path.endswith("/management.html"):
        path = path[: -len("/management.html")]
    elif path.endswith("/v0/management"):
        path = path[: -len("/v0/management")]
    return urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", ""))


def error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(
                data.get("message") or data.get("error") or data.get("detail") or ""
            )[:300]
    except Exception:
        pass
    return f"HTTP {response.status_code}"
