#!/usr/bin/env python3
"""
批量导入 xAI SSO cookie 到项目 auth.json（纯 HTTP Device Flow）

用法:
  # 单个 / 批量 SSO，每个导入后按 user_id 合并到 data/auth.json
  python3 sso_to_auth_json.py --sso sso_list.txt

  # 写出多个独立 auth 文件（每个可直接 cp 到 ~/.grok/auth.json）
  python3 sso_to_auth_json.py --sso sso_list.txt --out-dir ./auth_out

  # 合并到指定 json（key 带 user_id 后缀，避免覆盖）
  python3 sso_to_auth_json.py --sso sso_list.txt --out auth_merged.json --merge

  # 单行 sso
  python3 sso_to_auth_json.py --sso-cookie 'eyJ...'

环境变量:
  GROK2API_AUTH_FILE  - 导入目标 auth.json（默认项目 data/auth.json）
  GROK2API_PROXY      - 代理地址，例如 http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import threading as _threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from curl_cffi import requests
from sso_context import SSOContext
import sso_cli as _sso_cli
import sso_device_flow as _device_flow
import sso_storage as _sso_storage

# Use project config when available, otherwise fall back to defaults
try:
    from config import AUTH_FILE, GROK_CLI_CLIENT_ID, OIDC_ISSUER, OIDC_SCOPES
except Exception:  # pragma: no cover - standalone fallback
    AUTH_FILE = Path(
        os.getenv("GROK2API_AUTH_FILE", str(Path.home() / ".grok" / "auth.json"))
    )
    GROK_CLI_CLIENT_ID = os.getenv(
        "GROK2API_OIDC_CLIENT_ID", "b1a00492-073a-47ea-816f-4c329264a828"
    )
    OIDC_ISSUER = os.getenv("GROK2API_OIDC_ISSUER", "https://auth.x.ai")
    OIDC_SCOPES = os.getenv(
        "GROK2API_OIDC_SCOPES",
        "openid profile email offline_access grok-cli:access "
        "api:access conversations:read conversations:write",
    )

AUTH_KEY = f"{OIDC_ISSUER}::{GROK_CLI_CLIENT_ID}"

# Throttle OIDC device-flow across concurrent registration workers. xAI returns
# HTTP 429 slow_down / rate_limited when many device/code+verify requests fan
# out together, so keep this bounded instead of fully serializing every account.
_DEVICE_FLOW_LOCK = _threading.RLock()
_DEVICE_FLOW_LAST_TS = 0.0


def _device_flow_concurrency() -> int:
    try:
        return max(
            1,
            min(
                4,
                int(os.getenv("GROK2API_SSO_DEVICE_CONCURRENCY", "2") or 2),
            ),
        )
    except (TypeError, ValueError):
        return 2


# The full flow is rate-limit sensitive, but serializing all registrations made
# conversion dominate the account duration. Two concurrent flows preserve a
# conservative ceiling while the request gap and 429 backoff remain in force.
_DEVICE_FLOW_SEQUENCE_SLOTS = _threading.BoundedSemaphore(_device_flow_concurrency())


def _raise_if_cancelled(should_cancel: Any | None = None) -> None:
    if callable(should_cancel):
        should_cancel()


def _sleep_interruptibly(seconds: float, should_cancel: Any | None = None) -> None:
    duration = max(0.0, float(seconds or 0.0))
    if duration <= 0:
        _raise_if_cancelled(should_cancel)
        return
    if not callable(should_cancel):
        time.sleep(duration)
        return
    deadline = time.monotonic() + duration
    while True:
        _raise_if_cancelled(should_cancel)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def _device_flow_gap_sec() -> float:
    try:
        return max(0.0, float(os.getenv("GROK2API_SSO_DEVICE_GAP_SEC", "1.2") or 1.2))
    except (TypeError, ValueError):
        return 1.2


def _device_flow_retries() -> int:
    try:
        return max(1, min(6, int(os.getenv("GROK2API_SSO_DEVICE_RETRIES", "3") or 3)))
    except (TypeError, ValueError):
        return 3


def _device_flow_backoff_sec(attempt: int) -> float:
    # attempt is 1-based after a failure
    base = 2.0 * attempt
    try:
        base = float(os.getenv("GROK2API_SSO_DEVICE_BACKOFF_SEC", str(base)) or base)
    except (TypeError, ValueError):
        pass
    return max(1.0, min(20.0, base))


def _wait_device_flow_slot(should_cancel: Any | None = None) -> None:
    """Global min-gap between device-flow starts (cross-thread)."""
    global _DEVICE_FLOW_LAST_TS
    gap = _device_flow_gap_sec()
    with _DEVICE_FLOW_LOCK:
        now = time.time()
        wait = (_DEVICE_FLOW_LAST_TS + gap) - now
        if wait > 0:
            _sleep_interruptibly(wait, should_cancel)
        _DEVICE_FLOW_LAST_TS = time.time()


def _acquire_device_flow_sequence_slot(should_cancel: Any | None = None) -> None:
    """Acquire one bounded OIDC conversion slot without trapping paused jobs."""
    while True:
        _raise_if_cancelled(should_cancel)
        if _DEVICE_FLOW_SEQUENCE_SLOTS.acquire(timeout=0.2):
            try:
                _raise_if_cancelled(should_cancel)
            except Exception:
                _DEVICE_FLOW_SEQUENCE_SLOTS.release()
                raise
            return


def _is_rate_limited_payload(
    text: str | None = None, url: str | None = None, status: int | None = None
) -> bool:
    blob = f"{status or ''} {url or ''} {text or ''}".lower()
    return any(
        k in blob
        for k in (
            "slow_down",
            "rate_limited",
            "rate limit",
            "too many",
            "429",
        )
    )


def _is_timeout_error(value: object) -> bool:
    blob = str(value or "").lower()
    return any(
        marker in blob
        for marker in (
            "curl: (28)",
            "curl error 28",
            "operation timed out",
            "timed out",
            "timeout",
            "readtimeout",
            "connecttimeout",
        )
    )


def _is_retryable_network_error(value: object) -> bool:
    blob = str(value or "").lower()
    return _is_timeout_error(blob) or any(
        marker in blob
        for marker in (
            "connection reset",
            "connection aborted",
            "connection refused",
            "temporary failure",
            "network is unreachable",
            "name resolution",
        )
    )


def _is_retryable_response(status: int | None, detail: object = "") -> bool:
    return _is_rate_limited_payload(str(detail), status=status) or int(status or 0) in {
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }


def _record_failure(
    failure: dict[str, Any] | None,
    *,
    stage: str,
    detail: object,
    status: int | None = None,
    retryable: bool | None = None,
) -> None:
    """Store a non-secret diagnostic for a caller that needs to show it."""
    if failure is None:
        return
    message = str(detail or "upstream returned an empty response").strip()
    if _is_rate_limited_payload(message, status=status):
        reason = "rate_limited"
        default_retryable = True
    elif _is_timeout_error(message):
        reason = "network_timeout"
        default_retryable = True
    elif stage == "sso_validation" and "invalid" in message.lower():
        reason = "invalid_sso"
        default_retryable = False
    else:
        reason = "upstream_rejected"
        default_retryable = _is_retryable_response(status, message)
    failure.clear()
    failure.update(
        {
            "stage": stage,
            "reason": reason,
            "detail": message[:320],
            "status": int(status or 0) or None,
            "retryable": default_retryable if retryable is None else bool(retryable),
        }
    )


def _proxy_kwargs() -> dict:
    """Return curl_cffi compatible proxy kwargs from env / proxy pool."""
    try:
        from proxy_pool import resolve_proxy_for_request, curl_proxies_arg

        url = resolve_proxy_for_request(fallback_env=True)
        proxies = curl_proxies_arg(url)
        if proxies:
            return {"proxies": proxies}
    except Exception:
        pass
    proxy = (
        os.getenv("GROK2API_XAI_PROXY")
        or os.getenv("GROK2API_PROXY")
        or os.getenv("GROK_CLI_PROXY")
        or ""
    ).strip()
    # Multi-line: take first non-empty line.
    if "\n" in proxy or "\r" in proxy:
        proxy = next(
            (
                ln.strip()
                for ln in proxy.replace("\r", "\n").split("\n")
                if ln.strip() and not ln.strip().startswith("#")
            ),
            "",
        )
    if proxy:
        return {"proxies": {"http": proxy, "https": proxy}}
    return {}


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    try:
        return json.loads(b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def rfc3339_ns(ts: float | None = None) -> str:
    """2026-07-10T01:00:00.000000000Z"""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"


def _http_timeout() -> float:
    try:
        return max(5.0, float(os.getenv("GROK2API_SSO_HTTP_TIMEOUT", "20") or 20))
    except (TypeError, ValueError):
        return 20.0


def _poll_interval_sec(raw: Any = None) -> float:
    """Device-code poll interval after approve.

    Upstream often advertises interval=5, but once the user_code is already
    approved we can poll immediately / more aggressively. Override with
    GROK2API_SSO_POLL_INTERVAL (seconds).
    """
    env = (os.getenv("GROK2API_SSO_POLL_INTERVAL") or "").strip()
    if env:
        try:
            return max(0.2, min(10.0, float(env)))
        except ValueError:
            pass
    try:
        hinted = float(raw if raw is not None else 1)
    except (TypeError, ValueError):
        hinted = 1.0
    # Prefer 1s (or the advertised value if already smaller) after approve.
    return max(0.4, min(hinted, 1.5))


def request_device_code(
    session: Any | None = None,
    *,
    failure: dict[str, Any] | None = None,
    retries: int | None = None,
    should_cancel: Any | None = None,
) -> dict | None:
    return _device_flow.request_device_code(
        _sso_context(),
        session,
        failure=failure,
        retries=retries,
        should_cancel=should_cancel,
    )


def poll_token(
    device_code: str,
    interval: int | float = 1,
    expires_in: int = 1800,
    timeout: int | float = 45,
    *,
    session: Any | None = None,
    immediate: bool = True,
    failure: dict[str, Any] | None = None,
    should_cancel: Any | None = None,
) -> dict | None:
    return _device_flow.poll_token(
        _sso_context(),
        device_code,
        interval,
        expires_in,
        timeout,
        session=session,
        immediate=immediate,
        failure=failure,
        should_cancel=should_cancel,
    )


def _copy_failure(target: dict[str, Any] | None, source: dict[str, Any]) -> None:
    if target is not None:
        target.clear()
        target.update(source)


def _retry_device_flow(
    log: Any,
    attempt: int,
    retries: int,
    failure: dict[str, Any],
    should_cancel: Any | None = None,
) -> bool:
    """Back off only for upstream failures where another full flow can help."""
    if attempt >= retries or not failure.get("retryable"):
        return False
    stage = failure.get("stage") or "device_flow"
    reason = failure.get("reason") or "upstream_error"
    log(f"  ⏳ {stage} {reason}; retrying device flow")
    _sleep_interruptibly(_device_flow_backoff_sec(attempt), should_cancel)
    return True


def sso_to_token(
    sso_cookie: str,
    *,
    quiet: bool = False,
    failure: dict[str, Any] | None = None,
    should_cancel: Any | None = None,
) -> dict | None:
    """Convert an xAI SSO cookie to an OIDC token with bounded parallel flows.

    ``failure`` is optional and receives a non-secret stage/reason diagnostic
    on failure. Existing callers that only need a token retain the same return
    type and behavior.
    """
    _acquire_device_flow_sequence_slot(should_cancel)
    try:
        return _sso_to_token_locked(
            sso_cookie,
            quiet=quiet,
            failure=failure,
            should_cancel=should_cancel,
        )
    finally:
        _DEVICE_FLOW_SEQUENCE_SLOTS.release()


def sso_to_token_with_browser(
    sso_cookie: str,
    authorize: Any,
    *,
    quiet: bool = False,
    failure: dict[str, Any] | None = None,
    should_cancel: Any | None = None,
) -> dict | None:
    """Run device consent in an authenticated browser and return OAuth tokens.

    Device-code creation and token polling remain OAuth client operations.  The
    user-facing verify and approve actions are delegated to ``authorize`` so the
    application no longer emulates those browser forms with direct HTTP posts.
    """
    log = (lambda *a, **k: None) if quiet else print
    _acquire_device_flow_sequence_slot(should_cancel)
    try:
        session = requests.Session()
        session.cookies.set("sso", sso_cookie, domain=".x.ai")
        retries = _device_flow_retries()
        for attempt in range(1, retries + 1):
            _raise_if_cancelled(should_cancel)
            attempt_failure: dict[str, Any] = {}
            _wait_device_flow_slot(should_cancel)
            dc = request_device_code(
                session=session,
                failure=attempt_failure,
                retries=1,
                should_cancel=should_cancel,
            )
            if not dc:
                _copy_failure(failure, attempt_failure)
                if _retry_device_flow(
                    log, attempt, retries, attempt_failure, should_cancel
                ):
                    continue
                return None
            user_code = str(dc.get("user_code") or "")
            device_code = str(dc.get("device_code") or "")
            verify_url = str(dc.get("verification_uri_complete") or "")
            if not user_code or not device_code or not verify_url:
                _record_failure(
                    attempt_failure,
                    stage="device_code",
                    detail="device/code response is missing required fields",
                    retryable=False,
                )
                _copy_failure(failure, attempt_failure)
                return None
            try:
                authorize(verify_url, user_code)
            except Exception as exc:  # noqa: BLE001
                _record_failure(
                    attempt_failure,
                    stage="approve",
                    detail=exc,
                    retryable=_is_retryable_network_error(exc),
                )
                _copy_failure(failure, attempt_failure)
                if _retry_device_flow(
                    log, attempt, retries, attempt_failure, should_cancel
                ):
                    continue
                return None
            token = poll_token(
                device_code,
                dc.get("interval", 1),
                dc.get("expires_in", 1800),
                timeout=float(os.getenv("GROK2API_SSO_POLL_TIMEOUT", "45") or 45),
                session=session,
                immediate=True,
                failure=attempt_failure,
                should_cancel=should_cancel,
            )
            if token:
                if failure is not None:
                    failure.clear()
                return token
            _copy_failure(failure, attempt_failure)
            if not _retry_device_flow(
                log, attempt, retries, attempt_failure, should_cancel
            ):
                return None
        return None
    finally:
        _DEVICE_FLOW_SEQUENCE_SLOTS.release()


def _sso_to_token_locked(
    sso_cookie: str,
    *,
    quiet: bool,
    failure: dict[str, Any] | None,
    should_cancel: Any | None,
) -> dict | None:
    log = (lambda *a, **k: None) if quiet else print
    s = requests.Session()
    s.cookies.set("sso", sso_cookie, domain=".x.ai")
    timeout = _http_timeout()
    proxy_kw = _proxy_kwargs()
    retries = _device_flow_retries()

    for validation_attempt in range(1, retries + 1):
        _raise_if_cancelled(should_cancel)
        validation_failure: dict[str, Any] = {}
        try:
            r = s.get(
                "https://accounts.x.ai/",
                impersonate="chrome",
                timeout=timeout,
                **proxy_kw,
            )
            status = int(getattr(r, "status_code", 0) or 0)
            final_url = str(getattr(r, "url", "") or "")
            if status >= 400:
                _record_failure(
                    validation_failure,
                    stage="sso_validation",
                    detail=f"HTTP {status}: {final_url}",
                    status=status,
                )
            elif "sign-in" in final_url or "sign-up" in final_url:
                _record_failure(
                    validation_failure,
                    stage="sso_validation",
                    detail=f"invalid SSO: redirected to {final_url}",
                    retryable=False,
                )
            else:
                log("  [ok] sso 有效")
                break
        except Exception as e:  # noqa: BLE001
            log(f"  [error] SSO 校验网络错误: {e}")
            _record_failure(validation_failure, stage="sso_validation", detail=e)

        _copy_failure(failure, validation_failure)
        if validation_attempt < retries and validation_failure.get("retryable"):
            log("  [wait] SSO 校验临时失败，稍后重试")
            _sleep_interruptibly(
                _device_flow_backoff_sec(validation_attempt), should_cancel
            )
            continue
        return None

    for attempt in range(1, retries + 1):
        _raise_if_cancelled(should_cancel)
        attempt_failure: dict[str, Any] = {}
        log(f"  [auth] Device Flow... (try {attempt}/{retries})")
        # The outer loop owns full-flow retries. One device/code request per
        # attempt avoids multiplying 429 traffic (3 inner x 3 outer retries).
        dc = request_device_code(
            session=s,
            failure=attempt_failure,
            retries=1,
            should_cancel=should_cancel,
        )
        if not dc:
            _copy_failure(failure, attempt_failure)
            if _retry_device_flow(
                log, attempt, retries, attempt_failure, should_cancel
            ):
                continue
            return None

        user_code = str(dc.get("user_code") or "")
        device_code = str(dc.get("device_code") or "")
        verify_url = str(dc.get("verification_uri_complete") or "")
        if not user_code or not device_code or not verify_url:
            _record_failure(
                attempt_failure,
                stage="device_code",
                detail="device/code response is missing required fields",
                retryable=False,
            )
            _copy_failure(failure, attempt_failure)
            return None
        log(f"  [code] user_code: {user_code}")

        try:
            _raise_if_cancelled(should_cancel)
            landing = s.get(
                verify_url,
                impersonate="chrome",
                timeout=timeout,
                **proxy_kw,
            )
            landing_status = int(getattr(landing, "status_code", 0) or 0)
            if landing_status >= 400:
                _record_failure(
                    attempt_failure,
                    stage="verify",
                    detail=f"HTTP {landing_status}: {getattr(landing, 'url', '')}",
                    status=landing_status,
                )
            else:
                _raise_if_cancelled(should_cancel)
                r = s.post(
                    f"{OIDC_ISSUER}/oauth2/device/verify",
                    data={"user_code": user_code},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=timeout,
                    allow_redirects=True,
                    **proxy_kw,
                )
                verify_status = int(getattr(r, "status_code", 0) or 0)
                verify_url_result = str(getattr(r, "url", "") or "")
                if "consent" not in verify_url_result:
                    _record_failure(
                        attempt_failure,
                        stage="verify",
                        detail=f"HTTP {verify_status or 'unknown'}: {verify_url_result}",
                        status=verify_status,
                    )
        except Exception as e:  # noqa: BLE001
            log(f"  [error] verify 异常: {e}")
            _record_failure(attempt_failure, stage="verify", detail=e)

        if attempt_failure:
            log(f"  [error] verify 失败: {attempt_failure.get('detail')}")
            _copy_failure(failure, attempt_failure)
            if _retry_device_flow(
                log, attempt, retries, attempt_failure, should_cancel
            ):
                continue
            return None

        try:
            _raise_if_cancelled(should_cancel)
            r = s.post(
                f"{OIDC_ISSUER}/oauth2/device/approve",
                data={
                    "user_code": user_code,
                    "action": "allow",
                    "principal_type": "User",
                    "principal_id": "",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                impersonate="chrome",
                timeout=timeout,
                allow_redirects=True,
                **proxy_kw,
            )
            approve_status = int(getattr(r, "status_code", 0) or 0)
            approve_url = str(getattr(r, "url", "") or "")
            if "done" not in approve_url:
                _record_failure(
                    attempt_failure,
                    stage="approve",
                    detail=f"HTTP {approve_status or 'unknown'}: {approve_url}",
                    status=approve_status,
                )
            else:
                log("  [ok] 授权确认")
        except Exception as e:  # noqa: BLE001
            log(f"  [error] approve 异常: {e}")
            _record_failure(attempt_failure, stage="approve", detail=e)

        if attempt_failure:
            log(f"  [error] approve 失败: {attempt_failure.get('detail')}")
            _copy_failure(failure, attempt_failure)
            if _retry_device_flow(
                log, attempt, retries, attempt_failure, should_cancel
            ):
                continue
            return None

        # Approve already happened — poll immediately with a short interval.
        token = poll_token(
            device_code,
            dc.get("interval", 1),
            dc.get("expires_in", 1800),
            timeout=float(os.getenv("GROK2API_SSO_POLL_TIMEOUT", "45") or 45),
            session=s,
            immediate=True,
            failure=attempt_failure,
            should_cancel=should_cancel,
        )
        if not token:
            _copy_failure(failure, attempt_failure)
            if _retry_device_flow(
                log, attempt, retries, attempt_failure, should_cancel
            ):
                continue
            return None
        if failure is not None:
            failure.clear()
        log(
            f"  [ok] access_token (expires_in={token.get('expires_in')}s)"
            + (" + refresh_token" if token.get("refresh_token") else "")
        )
        return token
    return None


def _sso_context() -> SSOContext:
    """Expose live conversion helpers to extracted services."""
    return SSOContext(globals())


def token_to_auth_entry(token: dict, email: str = "") -> tuple[str, dict]:
    return _sso_storage.token_to_auth_entry(_sso_context(), token, email)


def write_auth_json(path: Path, auth_key: str, entry: dict) -> None:
    return _sso_storage.write_auth_json(_sso_context(), path, auth_key, entry)


def merge_auth_json(
    path: Path, auth_key: str, entry: dict, unique: bool = True
) -> None:
    return _sso_storage.merge_auth_json(_sso_context(), path, auth_key, entry, unique)


def import_into_project_auth(entry: dict) -> str:
    return _sso_storage.import_into_project_auth(_sso_context(), entry)


def load_sso_list(path: str | None, single: str | None) -> list[tuple[str, str]]:
    return _sso_cli.load_sso_list(_sso_context(), path, single)


def main() -> int:
    return _sso_cli.main(_sso_context())


def process_one_sso(
    index: int,
    email_hint: str,
    sso: str,
    *,
    args_email: str,
    into_project: bool,
    out_dir: Path | None,
    out: Path | None,
    merge: bool,
    total: int,
) -> dict[str, Any]:
    return _sso_cli.process_one_sso(
        _sso_context(),
        index,
        email_hint,
        sso,
        args_email=args_email,
        into_project=into_project,
        out_dir=out_dir,
        out=out,
        merge=merge,
        total=total,
    )


def run_concurrent(
    cookies: list[tuple[str, str]],
    *,
    max_workers: int,
    delay: int,
    args_email: str,
    into_project: bool,
    out_dir: Path | None,
    out: Path | None,
    merge: bool,
) -> tuple[int, int, list[dict[str, Any]]]:
    return _sso_cli.run_concurrent(
        _sso_context(),
        cookies,
        max_workers=max_workers,
        delay=delay,
        args_email=args_email,
        into_project=into_project,
        out_dir=out_dir,
        out=out,
        merge=merge,
    )


def main() -> int:
    return _sso_cli.main(_sso_context())


if __name__ == "__main__":
    sys.exit(main())
