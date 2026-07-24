"""Proxy pool helpers for protocol registration / outbound HTTP.

Supports multi-line proxy lists (one proxy per line) with shared optional
username/password, plus simple rotation strategies for batch jobs.

Accepted line formats:
  - http://host:port
  - http://user:pass@host:port
  - socks5://host:port
  - host:port
  - host:port:user:pass
  - scheme://host:port:user:pass  (common residential-provider style)

Legacy single-proxy config continues to work unchanged.
"""

from __future__ import annotations

import os
import random
import threading
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlparse, urlunparse

_lock = threading.Lock()
_rr_index = 0


def _env_proxy_text() -> str:
    # Prefer dedicated pool env, then the classic single-proxy vars.
    for key in (
        "GROK2API_XAI_PROXY_POOL",
        "GROK2API_PROXY_POOL",
        "GROK2API_XAI_PROXY",
        "GROK2API_PROXY",
        "GROK_CLI_PROXY",
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return ""


def _env_proxy_user() -> str:
    return (
        os.getenv("GROK2API_XAI_PROXY_USERNAME")
        or os.getenv("GROK2API_PROXY_USERNAME")
        or ""
    ).strip()


def _env_proxy_pass() -> str:
    return (
        os.getenv("GROK2API_XAI_PROXY_PASSWORD")
        or os.getenv("GROK2API_PROXY_PASSWORD")
        or ""
    ).strip()


def split_proxy_text(text: str | None) -> list[str]:
    """Split multi-proxy text into raw lines (comma / newline / semicolon)."""
    raw = (text or "").strip()
    if not raw:
        return []
    # Normalize common separators while preserving URL schemes (://).
    # First split on newlines / semicolons; then on commas only when the token
    # does not look like a single URL with query string.
    chunks: list[str] = []
    for part in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        part = part.strip()
        if not part:
            continue
        if ";" in part:
            for sub in part.split(";"):
                sub = sub.strip()
                if sub:
                    chunks.append(sub)
            continue
        # Comma-separated lists: "a,b,c" — but not "http://x?a=1,b=2" (rare).
        if "," in part and "://" not in part.split(",", 1)[0]:
            for sub in part.split(","):
                sub = sub.strip()
                if sub:
                    chunks.append(sub)
            continue
        # Also allow "url1,url2" when each segment has a scheme.
        if "," in part:
            maybe = [s.strip() for s in part.split(",") if s.strip()]
            if maybe and all("://" in s or s.count(":") >= 1 for s in maybe):
                chunks.extend(maybe)
                continue
        chunks.append(part)
    # Drop comments / empty.
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        line = c.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _normalize_line_scheme(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    lower = s.lower()
    if lower.startswith("soket5://"):
        return "socks5://" + s.split("://", 1)[1]
    if lower.startswith("socket5://"):
        return "socks5://" + s.split("://", 1)[1]
    return s


def _hostport_userpass(raw: str) -> str | None:
    """Parse host:port:user:pass (or scheme://host:port:user:pass) → URL."""
    s = _normalize_line_scheme(raw)
    if not s:
        return None
    scheme = "http"
    rest = s
    if "://" in s:
        scheme, rest = s.split("://", 1)
        scheme = (scheme or "http").strip().lower() or "http"
        if scheme in {"soket5", "socket5"}:
            scheme = "socks5"
    # Already a normal URL with optional userinfo.
    if "@" in rest or rest.count(":") <= 1:
        if "://" not in s:
            return f"{scheme}://{rest}"
        return f"{scheme}://{rest}" if not s.startswith(f"{scheme}://") else s

    # host:port:user:pass  (user/pass may contain ':')
    # IPv6 is not supported in this shorthand (use full URL).
    parts = rest.split(":")
    if len(parts) < 4:
        if "://" not in s:
            return f"{scheme}://{rest}"
        return s
    host = parts[0].strip()
    port = parts[1].strip()
    user = parts[2]
    password = ":".join(parts[3:])
    if not host or not port:
        return None
    try:
        int(port)
    except ValueError:
        return None
    auth = quote(user, safe="")
    if password != "":
        auth = f"{auth}:{quote(password, safe='')}"
    return f"{scheme}://{auth}@{host}:{port}"


def canonicalize_proxy_line(
    raw: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Return a single proxy URL with optional shared auth applied.

    Raises ValueError when the line is not a usable proxy.
    """
    from moemail import normalize_proxy_config

    line = (raw or "").strip()
    if not line:
        raise ValueError("empty proxy line")
    # Expand host:port:user:pass shorthand first.
    expanded = _hostport_userpass(line) or line
    cfg = normalize_proxy_config(
        expanded,
        username=username,
        password=password,
    )
    if not cfg or not cfg.get("proxy"):
        raise ValueError("invalid proxy")
    return str(cfg["proxy"])


def parse_proxy_pool(
    text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    fallback_env: bool = True,
) -> list[str]:
    """Parse proxy pool text into a de-duplicated list of full proxy URLs.

    Invalid lines are skipped (not raised) so a large paste still yields the
    usable subset. Callers that need strict validation should use
    ``validate_proxy_pool``.
    """
    raw = (text if text is not None else "").strip()
    if not raw and fallback_env:
        raw = _env_proxy_text()
    lines = split_proxy_text(raw)
    if not lines:
        return []

    user = username
    pwd = password
    if user is None and fallback_env:
        user = _env_proxy_user() or None
    if pwd is None and fallback_env:
        pwd = _env_proxy_pass() or None
    # Empty string means "explicitly none"; None means "use default/env".
    user_s = None if user is None else str(user).strip()
    pass_s = None if pwd is None else str(pwd).strip()

    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        try:
            url = canonicalize_proxy_line(line, username=user_s, password=pass_s)
        except Exception:
            continue
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def validate_proxy_pool(
    text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    fallback_env: bool = False,
) -> dict[str, Any]:
    """Validate every non-empty line; return ok/errors/proxies summary."""
    lines = split_proxy_text(text or "")
    if not lines and fallback_env:
        lines = split_proxy_text(_env_proxy_text())
    user = username
    pwd = password
    if user is None and fallback_env:
        user = _env_proxy_user() or None
    if pwd is None and fallback_env:
        pwd = _env_proxy_pass() or None
    user_s = None if user is None else str(user).strip()
    pass_s = None if pwd is None else str(pwd).strip()

    proxies: list[str] = []
    errors: list[dict[str, str]] = []
    for i, line in enumerate(lines, start=1):
        try:
            url = canonicalize_proxy_line(line, username=user_s, password=pass_s)
            proxies.append(url)
        except Exception as e:  # noqa: BLE001
            errors.append({"line": i, "raw": line[:200], "error": str(e)[:200]})
    return {
        "ok": not errors and bool(proxies),
        "count": len(proxies),
        "proxies": proxies,
        "errors": errors,
        "empty": not lines,
    }


def normalize_proxy_strategy(value: str | None) -> str:
    s = (value or "round_robin").strip().lower().replace("-", "_")
    if s in {"rr", "round", "roundrobin", "round_robin"}:
        return "round_robin"
    if s in {"rand", "random"}:
        return "random"
    if s in {"sticky", "first", "fixed"}:
        return "sticky"
    return "round_robin"


def pick_proxy(
    proxies: Iterable[str] | None,
    *,
    strategy: str | None = "round_robin",
    index: int | None = None,
) -> str | None:
    """Pick one proxy URL from a pool.

    - round_robin: global counter (thread-safe), or ``index`` when provided
    - random: uniform random
    - sticky: always first
    """
    pool = [str(p).strip() for p in (proxies or []) if str(p).strip()]
    if not pool:
        return None
    mode = normalize_proxy_strategy(strategy)
    if mode == "sticky" or len(pool) == 1:
        return pool[0]
    if mode == "random":
        return random.choice(pool)
    # round_robin
    if index is not None:
        return pool[int(index) % len(pool)]
    global _rr_index
    with _lock:
        i = _rr_index
        _rr_index = (i + 1) % (10**9)
    return pool[i % len(pool)]


def resolve_proxy_for_request(
    *,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    strategy: str | None = None,
    index: int | None = None,
    fallback_env: bool = True,
) -> str | None:
    """High-level: parse pool text + pick one URL for this job/request."""
    pool = parse_proxy_pool(
        proxy,
        username=proxy_username,
        password=proxy_password,
        fallback_env=fallback_env,
    )
    if not pool:
        return None
    strat = strategy
    if strat is None:
        strat = (
            os.getenv("GROK2API_PROXY_STRATEGY")
            or os.getenv("GROK2API_XAI_PROXY_STRATEGY")
            or "round_robin"
        )
    return pick_proxy(pool, strategy=strat, index=index)


def pool_summary(
    text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
    strategy: str | None = None,
    fallback_env: bool = True,
) -> dict[str, Any]:
    pool = parse_proxy_pool(
        text,
        username=username,
        password=password,
        fallback_env=fallback_env,
    )
    return {
        "enabled": bool(pool),
        "count": len(pool),
        "strategy": normalize_proxy_strategy(strategy),
        # Mask credentials in previews.
        "preview": [_mask_proxy_url(p) for p in pool[:8]],
    }


def _mask_proxy_url(url: str) -> str:
    try:
        p = urlparse(url)
        if not p.hostname:
            return url[:48]
        host = p.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{p.port}" if p.port else ""
        user = unquote(p.username) if p.username else ""
        if user:
            return f"{p.scheme}://{user}:***@{host}{port}"
        return f"{p.scheme}://{host}{port}"
    except Exception:
        return (url or "")[:48]


def httpx_proxy_arg(proxy_url: str | None) -> str | None:
    """httpx Client(proxy=...) expects a single URL string (or None)."""
    s = (proxy_url or "").strip()
    return s or None


def curl_proxies_arg(proxy_url: str | None) -> dict[str, str] | None:
    """curl_cffi / requests style proxies dict."""
    s = (proxy_url or "").strip()
    if not s:
        return None
    return {"http": s, "https": s}


# ── Outbound (account pool) proxy selection ─────────────────────────────────


def get_outbound_proxy_source() -> dict[str, Any]:
    """Load effective outbound proxy pool text/auth/strategy.

    Preference order:
      1) settings_store.outbound_proxy_config (admin UI)
      2) env GROK2API_XAI_PROXY_POOL / GROK2API_XAI_PROXY
      3) registration_config.proxy (shared pool fallback)
    """
    text = ""
    user = ""
    password = ""
    strategy = "round_robin"
    enabled = True
    source = "none"

    try:
        from settings_store import get_outbound_proxy_config

        cfg = get_outbound_proxy_config(include_secrets=True) or {}
        if isinstance(cfg, dict):
            enabled = bool(cfg.get("enabled", True))
            text = str(cfg.get("proxy") or "").strip()
            user = str(cfg.get("proxy_username") or "").strip()
            password = str(cfg.get("proxy_password") or "").strip()
            strategy = normalize_proxy_strategy(
                str(cfg.get("proxy_strategy") or "round_robin")
            )
            if text:
                source = "settings"
    except Exception:
        pass

    if not text:
        env_text = _env_proxy_text()
        if env_text:
            text = env_text
            user = user or _env_proxy_user()
            password = password or _env_proxy_pass()
            strategy = normalize_proxy_strategy(
                os.getenv("GROK2API_XAI_PROXY_STRATEGY")
                or os.getenv("GROK2API_PROXY_STRATEGY")
                or strategy
            )
            source = "env"

    if not text:
        try:
            from settings_store import get_registration_config

            reg = get_registration_config(include_secrets=True) or {}
            if isinstance(reg, dict) and str(reg.get("proxy") or "").strip():
                text = str(reg.get("proxy") or "").strip()
                user = user or str(reg.get("proxy_username") or "").strip()
                password = password or str(reg.get("proxy_password") or "").strip()
                strategy = normalize_proxy_strategy(
                    str(reg.get("proxy_strategy") or strategy)
                )
                source = "registration"
        except Exception:
            pass

    if not enabled:
        return {
            "enabled": False,
            "proxy": "",
            "proxy_username": user,
            "proxy_password": password,
            "proxy_strategy": strategy,
            "source": source,
            "pool": [],
        }

    pool = parse_proxy_pool(
        text,
        username=user or None,
        password=password or None,
        fallback_env=False,
    )
    return {
        "enabled": bool(pool),
        "proxy": text,
        "proxy_username": user,
        "proxy_password": password,
        "proxy_strategy": strategy,
        "source": source if pool else "none",
        "pool": pool,
    }


def pick_proxy_for_account(
    account_id: str | None = None,
    *,
    strategy: str | None = None,
    pool: list[str] | None = None,
) -> str | None:
    """Pick a proxy for an account-pool outbound request.

    Account traffic defaults to **stable sticky-by-account** so multi-turn
    affinity keeps the same egress IP. Explicit strategies:
      - sticky: always first proxy
      - random: random each call
      - round_robin: stable hash(account_id) when account_id given, else global RR
    """
    if pool is None:
        src = get_outbound_proxy_source()
        if not src.get("enabled"):
            return None
        pool = list(src.get("pool") or [])
        if strategy is None:
            strategy = str(src.get("proxy_strategy") or "round_robin")
    pool = [str(p).strip() for p in (pool or []) if str(p).strip()]
    if not pool:
        return None
    mode = normalize_proxy_strategy(strategy)
    if mode == "sticky" or len(pool) == 1:
        return pool[0]
    if mode == "random":
        return random.choice(pool)
    # round_robin / default: pin by account id when available
    aid = str(account_id or "").strip()
    if aid:
        # FNV-1a 32-bit — fast stable hash, no crypto dependency.
        h = 2166136261
        for ch in aid.encode("utf-8", errors="ignore"):
            h ^= ch
            h = (h * 16777619) & 0xFFFFFFFF
        return pool[h % len(pool)]
    return pick_proxy(pool, strategy="round_robin")


def outbound_pool_public_summary() -> dict[str, Any]:
    src = get_outbound_proxy_source()
    pool = list(src.get("pool") or [])
    return {
        "enabled": bool(src.get("enabled") and pool),
        "count": len(pool),
        "strategy": normalize_proxy_strategy(
            str(src.get("proxy_strategy") or "round_robin")
        ),
        "source": src.get("source") or "none",
        "preview": [_mask_proxy_url(p) for p in pool[:8]],
    }
