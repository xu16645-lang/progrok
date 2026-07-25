"""Grok account probe protocol implementation."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from export_formats import CPA_BASE_URL, CPA_TOKEN_ENDPOINT, GROK_CLIENT_ID
from .common import (
    DEFAULT_PROBE_MODEL,
    PROBE_MODELS_TIMEOUT,
    PROBE_PERMISSION_RETRY_DELAYS,
    PROBE_RESPONSE_TIMEOUT,
    PROBE_TRANSIENT_STATUS_CODES,
    error_text as _error_text,
)


GROK_CLI_VERSION = "0.2.93"
GROK_UPSTREAM_USER_AGENT = "sub2api-grok/1.0"
GROK_OAUTH_USER_AGENT = "sub2api-grok-oauth/1.0"
GROK_QUOTA_PROBE_INPUT = "hi"
TOKEN_REFRESH_SKEW_SECONDS = 300.0
DEFAULT_TOKEN_TTL_SECONDS = 21600
_refresh_locks_guard = threading.RLock()
_refresh_locks: dict[str, threading.RLock] = {}


def _unix_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return timestamp if timestamp > 0 else None
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).strip().replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(value.decode("utf-8"))
        return _unix_timestamp(data.get("exp")) if isinstance(data, dict) else None
    except (IndexError, UnicodeDecodeError, ValueError):
        return None


def _token_expiry(record: dict[str, Any], token: str) -> float | None:
    for field in ("expires_at", "expired"):
        parsed = _unix_timestamp(record.get(field))
        if parsed is not None:
            return parsed
    return _jwt_expiry(token)


def _refresh_lock(record: dict[str, Any]) -> threading.RLock:
    identity = "|".join(
        str(record.get(field) or "").strip().lower()
        for field in ("sub", "user_id", "principal_id", "email")
    ).strip("|")
    if not identity:
        identity = hashlib.sha256(
            str(record.get("refresh_token") or "").encode("utf-8")
        ).hexdigest()
    with _refresh_locks_guard:
        lock = _refresh_locks.get(identity)
        if lock is None:
            lock = threading.RLock()
            _refresh_locks[identity] = lock
        return lock


def _rfc3339(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refresh_required(record: dict[str, Any], token: str) -> bool:
    if not token:
        return True
    expires_at = _token_expiry(record, token)
    return expires_at is not None and expires_at - time.time() <= TOKEN_REFRESH_SKEW_SECONDS


def _refresh_failure(
    *,
    model: str,
    started: float,
    error: str,
    status_code: int | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "available": None if retryable else False,
        "retryable": retryable,
        "classification": "uncertain" if retryable else "invalid_credential",
        "model": model,
        "status_code": status_code,
        "latency_ms": int((time.time() - started) * 1000),
        "error": error[:300],
    }


def _persist_refreshed_record(record: dict[str, Any], refreshed: dict[str, Any]) -> dict[str, Any]:
    if not record.get("_local_account_path") and not record.get("_local_auth_key"):
        return {"ok": True, "persisted": False}
    try:
        from accounts import persist_xai_oauth_refresh

        return persist_xai_oauth_refresh(record, refreshed)
    except Exception as exc:  # pragma: no cover - safety net around local persistence
        return {"ok": False, "error": f"刷新凭证已获取但本地保存失败：{str(exc)[:180]}"}


def _refresh_xai_oauth(
    http: httpx.Client,
    record: dict[str, Any],
    *,
    model: str,
    started: float,
    force: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Refresh an xAI OAuth token using the same pre-request lifecycle as Sub2API."""
    refresh_token = str(record.get("refresh_token") or "").strip()
    if not refresh_token:
        return None, _refresh_failure(
            model=model,
            started=started,
            error="xAI access_token 已过期或被拒绝，且未保存 refresh_token",
        )

    with _refresh_lock(record):
        current_token = str(record.get("access_token") or record.get("key") or "").strip()
        if current_token and not force and not _refresh_required(record, current_token):
            return record, None

        endpoint = str(record.get("token_endpoint") or CPA_TOKEN_ENDPOINT).strip() or CPA_TOKEN_ENDPOINT
        client_id = str(
            record.get("client_id") or record.get("oidc_client_id") or GROK_CLIENT_ID
        ).strip() or GROK_CLIENT_ID
        try:
            response = http.post(
                endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": GROK_OAUTH_USER_AGENT,
                },
                timeout=PROBE_MODELS_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            return None, _refresh_failure(
                model=model,
                started=started,
                error=f"刷新 xAI OAuth 凭证时网络异常：{str(exc)[:180]}",
                retryable=True,
            )

        if response.status_code >= 400:
            error = _error_text(response) or f"HTTP {response.status_code}"
            retryable = response.status_code in PROBE_TRANSIENT_STATUS_CODES
            return None, _refresh_failure(
                model=model,
                started=started,
                error=f"刷新 xAI OAuth 凭证失败：{error}",
                status_code=response.status_code,
                retryable=retryable,
            )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict) or not str(payload.get("access_token") or "").strip():
            return None, _refresh_failure(
                model=model,
                started=started,
                error="刷新 xAI OAuth 凭证失败：响应中缺少 access_token",
            )

        try:
            expires_in = int(float(payload.get("expires_in") or DEFAULT_TOKEN_TTL_SECONDS))
        except (TypeError, ValueError):
            expires_in = DEFAULT_TOKEN_TTL_SECONDS
        expires_in = max(1, expires_in)
        refreshed_at = _rfc3339(time.time())
        refreshed = dict(record)
        refreshed.update(
            {
                "access_token": str(payload["access_token"]).strip(),
                "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
                "expires_at": _rfc3339(time.time() + expires_in),
                "last_refresh": refreshed_at,
            }
        )
        if "key" in refreshed:
            refreshed["key"] = refreshed["access_token"]
        if "expired" in refreshed:
            refreshed["expired"] = refreshed["expires_at"]
        for field in ("id_token", "token_type", "scope"):
            value = str(payload.get(field) or "").strip()
            if value:
                refreshed[field] = value

        persisted = _persist_refreshed_record(record, refreshed)
        if not persisted.get("ok"):
            return None, _refresh_failure(
                model=model,
                started=started,
                error=str(persisted.get("error") or "刷新后的 xAI 凭证未能保存"),
            )
        return refreshed, None


def _probe_headers(record: dict[str, Any], token: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": GROK_UPSTREAM_USER_AGENT,
        "X-Grok-Client-Version": GROK_CLI_VERSION,
        "X-Grok-Client-Mode": "interactive",
    }
    # CPA exports include historical Grok Pager headers.  Sub2API's current
    # account test only uses the CLI identity above, so do not leak those
    # defaults into the direct upstream probe.  Preserve only unrelated
    # per-account overrides (for example a deployment-specific trace header).
    controlled = {
        "authorization",
        "accept",
        "content-type",
        "user-agent",
        "x-xai-token-auth",
        "x-authenticateresponse",
        "x-grok-client-identifier",
        "x-grok-client-version",
        "x-grok-client-mode",
    }
    extra_headers = record.get("headers")
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if (
                isinstance(key, str)
                and isinstance(value, str)
                and key.strip()
                and key.strip().lower() not in controlled
            ):
                headers[key] = value
    return headers


def _event_error(event: dict[str, Any]) -> str:
    error = event.get("error")
    if not error and isinstance(event.get("response"), dict):
        error = event["response"].get("error")
    if isinstance(error, dict):
        error = error.get("message") or error.get("error") or error.get("code")
    return str(error or event.get("message") or "上游返回失败事件").strip()[:300]


def _response_output_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("output_text", "text", "delta"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                value = part.get("text") or part.get("output_text")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    return "".join(parts).strip()


def _parse_grok_probe_response(response: httpx.Response) -> dict[str, Any]:
    """Apply the same SSE completion semantics as Sub2API's account test."""
    raw = str(response.text or "")
    completed = False
    failed_error = ""
    reply_parts: list[str] = []
    saw_event = False

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        encoded = line[5:].strip()
        if not encoded:
            continue
        if encoded == "[DONE]":
            continue
        saw_event = True
        try:
            event = json.loads(encoded)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                reply_parts.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text.strip() and not reply_parts:
                reply_parts.append(text)
        elif event_type in {"response.completed", "response.done"}:
            completed = True
            nested = event.get("response")
            if isinstance(nested, dict) and not reply_parts:
                output_text = _response_output_text(nested)
                if output_text:
                    reply_parts.append(output_text)
        elif event_type in {"response.failed", "error"}:
            failed_error = _event_error(event)

    if not saw_event and raw.strip():
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            event_type = str(payload.get("type") or "").strip()
            status = str(payload.get("status") or "").strip().lower()
            if event_type in {"response.failed", "error"} or status in {
                "failed",
                "error",
                "cancelled",
            }:
                failed_error = _event_error(payload)
            completed = event_type in {"response.completed", "response.done"} or status == "completed"
            output_text = _response_output_text(payload)
            if output_text:
                reply_parts.append(output_text)

    reply = "".join(reply_parts).strip()
    if failed_error:
        return {
            "ok": False,
            "retryable": False,
            "classification": "upstream_failed",
            "error": failed_error,
        }
    if not completed:
        return {
            "ok": False,
            "retryable": True,
            "classification": "incomplete_stream" if reply else "no_reply",
            "error": "上游响应流未完成" if reply else "上游未返回模型回复",
        }
    if not reply:
        return {
            "ok": False,
            "retryable": True,
            "classification": "no_reply",
            "error": "上游响应已完成，但没有模型回复内容",
        }
    return {
        "ok": True,
        "retryable": False,
        "classification": "available",
        "reply": reply[:160],
    }


def _is_transient_permission_error(status_code: int, error: str) -> bool:
    text = str(error or "").lower()
    return status_code == 403 and (
        "access to the chat endpoint is denied" in text
        or "log into console.x.ai and update the permissions" in text
    )


def _models_from_payload(payload: Any) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("data")
    if not isinstance(models, list):
        models = payload.get("models")
    return models if isinstance(models, list) else []


def _lightweight_probe(
    http: httpx.Client,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    started: float,
    response_error: str,
    response_status_code: int | None = None,
) -> dict[str, Any]:
    """Fallback to /models so slow inference is not mistaken for a dead account."""
    try:
        response = http.get(
            f"{base_url}/models",
            headers=headers,
            timeout=PROBE_MODELS_TIMEOUT,
        )
        latency_ms = int((time.time() - started) * 1000)
        if response.status_code < 400:
            try:
                models = _models_from_payload(response.json())
            except Exception:
                models = []
            if models:
                return {
                    "ok": True,
                    "available": True,
                    "chat_ready": False,
                    "degraded": True,
                    "fallback": "models",
                    "classification": "model_busy",
                    "model": model,
                    "status_code": response.status_code,
                    "response_status_code": response_status_code,
                    "latency_ms": latency_ms,
                    "retry_count": 0,
                    "message": "消息响应异常，但轻量模型验证通过",
                    "response_error": response_error[:220],
                }
            return {
                "ok": False,
                "available": None,
                "retryable": True,
                "classification": "uncertain",
                "fallback": "models",
                "model": model,
                "status_code": response.status_code,
                "response_status_code": response_status_code,
                "latency_ms": latency_ms,
                "error": "消息响应异常，且 /models 暂未返回可用模型",
            }
        error = _error_text(response)
        if response.status_code == 401:
            return {
                "ok": False,
                "available": False,
                "retryable": False,
                "classification": "invalid_credential",
                "fallback": "models",
                "model": model,
                "status_code": response.status_code,
                "response_status_code": response_status_code,
                "latency_ms": latency_ms,
                "error": error or "认证凭证无效",
            }
        retryable = response.status_code in PROBE_TRANSIENT_STATUS_CODES or response.status_code == 403
        return {
            "ok": False,
            "available": None if retryable else False,
            "retryable": retryable,
            "classification": "uncertain" if retryable else "probe_failed",
            "fallback": "models",
            "model": model,
            "status_code": response.status_code,
            "response_status_code": response_status_code,
            "latency_ms": latency_ms,
            "error": error or response_error,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "available": None,
            "retryable": True,
            "classification": "uncertain",
            "fallback": "models",
            "model": model,
            "response_status_code": response_status_code,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"消息与轻量验证均暂时无响应：{str(exc)[:180]}",
        }


def probe_account(
    record: dict[str, Any],
    model: str = DEFAULT_PROBE_MODEL,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Run Sub2API's Grok Responses probe directly against the xAI upstream."""
    model = str(model or DEFAULT_PROBE_MODEL).strip() or DEFAULT_PROBE_MODEL
    base_url = str(record.get("base_url") or CPA_BASE_URL).rstrip("/")
    if "api.x.ai" in base_url:
        base_url = CPA_BASE_URL.rstrip("/")
    body = {
        "model": model,
        "input": GROK_QUOTA_PROBE_INPUT,
        "stream": True,
    }
    started = time.time()
    owned = client is None
    http = client or httpx.Client(timeout=PROBE_RESPONSE_TIMEOUT)
    try:
        current = dict(record)
        token = str(current.get("access_token") or current.get("key") or "").strip()
        token_refreshed = False
        if _refresh_required(current, token):
            current, refresh_failure = _refresh_xai_oauth(
                http,
                current,
                model=model,
                started=started,
            )
            if refresh_failure is not None:
                return refresh_failure
            if current is None:  # Defensive: successful refresh always returns a record.
                return _refresh_failure(
                    model=model,
                    started=started,
                    error="刷新 xAI OAuth 凭证后未返回有效账号数据",
                )
            token = str(current.get("access_token") or current.get("key") or "").strip()
            token_refreshed = True
        if not token:
            return {
                "ok": False,
                "available": False,
                "classification": "invalid_credential",
                "model": model,
                "error": "缺少 access_token，且无法使用 refresh_token 刷新",
            }

        headers = _probe_headers(current, token)
        retry_count = 0
        retried_after_401 = False
        while True:
            response = http.post(
                f"{base_url}/responses",
                headers=headers,
                json=body,
                timeout=PROBE_RESPONSE_TIMEOUT,
            )
            latency_ms = int((time.time() - started) * 1000)
            if response.status_code < 400:
                parsed = _parse_grok_probe_response(response)
                if not parsed.get("ok"):
                    return {
                        **parsed,
                        "available": None if parsed.get("retryable") else False,
                        "model": model,
                        "status_code": response.status_code,
                        "latency_ms": latency_ms,
                        "retry_count": retry_count,
                        "token_refreshed": token_refreshed,
                    }
                return {
                    "ok": True,
                    "available": True,
                    "classification": "available",
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "retry_count": retry_count,
                    "token_refreshed": token_refreshed,
                    "reply": parsed.get("reply", ""),
                }
            error = _error_text(response)
            if response.status_code == 401:
                if not retried_after_401 and str(current.get("refresh_token") or "").strip():
                    retried_after_401 = True
                    current, refresh_failure = _refresh_xai_oauth(
                        http,
                        current,
                        model=model,
                        started=started,
                        force=True,
                    )
                    if refresh_failure is not None:
                        return refresh_failure
                    if current is None:
                        return _refresh_failure(
                            model=model,
                            started=started,
                            error="刷新 xAI OAuth 凭证后未返回有效账号数据",
                        )
                    token = str(current.get("access_token") or current.get("key") or "").strip()
                    if not token:
                        return _refresh_failure(
                            model=model,
                            started=started,
                            error="刷新 xAI OAuth 凭证后仍缺少 access_token",
                        )
                    headers = _probe_headers(current, token)
                    token_refreshed = True
                    continue
                return {
                    "ok": False,
                    "available": False,
                    "retryable": False,
                    "classification": "invalid_credential",
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "retry_count": retry_count,
                    "token_refreshed": token_refreshed,
                    "error": error or "xAI OAuth 凭证无效或已过期",
                }
            if _is_transient_permission_error(response.status_code, error):
                if retry_count < len(PROBE_PERMISSION_RETRY_DELAYS):
                    delay = PROBE_PERMISSION_RETRY_DELAYS[retry_count]
                    retry_count += 1
                    time.sleep(delay)
                    continue
                return {
                    "ok": False,
                    "available": None,
                    "retryable": True,
                    "classification": "permission_pending",
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "retry_count": retry_count,
                    "token_refreshed": token_refreshed,
                    "error": (
                        "xAI 聊天权限尚未生效，已完成权限传播重试："
                        f"{error or 'Access to the chat endpoint is denied'}"
                    )[:300],
                }
            retryable = response.status_code in PROBE_TRANSIENT_STATUS_CODES
            return {
                "ok": False,
                "available": None if retryable else False,
                "retryable": retryable,
                "classification": "rate_limited"
                if response.status_code == 429
                else "uncertain"
                if retryable
                else "upstream_failed",
                "model": model,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "retry_count": retry_count,
                "token_refreshed": token_refreshed,
                "error": error or f"HTTP {response.status_code}",
            }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "available": None,
            "retryable": True,
            "classification": "uncertain",
            "model": model,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"xAI 探活网络错误：{str(exc)[:220]}",
        }
    finally:
        if owned:
            http.close()
