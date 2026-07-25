"""Local xAI Authorization Code + PKCE flow.

The browser owns interactive consent. This module owns the PKCE session and
token exchange so the resulting credential can be saved and then exported to
either Sub2API or CPA without either site participating in OAuth.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests


AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
REDIRECT_URI = "http://127.0.0.1:56121/callback"
SCOPE = "openid profile email offline_access grok-cli:access api:access"


class XaiPKCEError(RuntimeError):
    """Raised when local PKCE authorization or exchange fails."""


@dataclass(frozen=True)
class PKCESession:
    session_id: str
    state: str
    nonce: str
    code_verifier: str
    code_challenge: str
    client_id: str = CLIENT_ID
    redirect_uri: str = REDIRECT_URI
    scope: str = SCOPE
    created_at: float = 0.0


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        segment = str(token or "").split(".")[1]
        segment += "=" * (-len(segment) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment))
        return payload if isinstance(payload, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


def token_to_auth_payload(token: dict[str, Any], *, email: str) -> dict[str, Any]:
    access_token = str(token.get("access_token") or "").strip()
    refresh_token = str(token.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise XaiPKCEError("xAI OAuth 凭证缺少 access_token 或 refresh_token")
    claims = _jwt_claims(access_token)
    try:
        expires_at = float(
            claims.get("exp")
            or time.time() + int(token.get("expires_in") or 21_600)
        )
    except (TypeError, ValueError):
        expires_at = time.time() + 21_600
    expires_iso = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return {
        "key": access_token,
        "access_token": access_token,
        "auth_mode": "oidc",
        "email": str(email or claims.get("email") or "").strip().lower(),
        "refresh_token": refresh_token,
        "id_token": str(token.get("id_token") or ""),
        "token_type": str(token.get("token_type") or "Bearer"),
        "expires_in": token.get("expires_in"),
        "expires_at": expires_iso,
        "scope": str(token.get("scope") or SCOPE),
        "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": CLIENT_ID,
    }


def proxy_kwargs(assigned_proxy: str = "") -> dict[str, Any]:
    try:
        from proxy_pool import curl_proxies_arg, resolve_proxy_for_request

        proxy_url = str(assigned_proxy or "").strip() or resolve_proxy_for_request(
            fallback_env=True
        )
        proxies = curl_proxies_arg(proxy_url)
        return {"proxies": proxies} if proxies else {}
    except Exception:
        return {}


def http_timeout() -> float:
    try:
        return max(5.0, float(os.getenv("XAI_OAUTH_HTTP_TIMEOUT", "20") or 20))
    except (TypeError, ValueError):
        return 20.0


def create_pkce_session() -> PKCESession:
    verifier = _b64url(secrets.token_bytes(32))
    return PKCESession(
        session_id=secrets.token_hex(16),
        state=secrets.token_hex(32),
        nonce=secrets.token_hex(16),
        code_verifier=verifier,
        code_challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
        created_at=time.time(),
    )


def build_authorization_url(session: PKCESession) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": session.client_id,
            "redirect_uri": session.redirect_uri,
            "scope": session.scope,
            "state": session.state,
            "nonce": session.nonce,
            "code_challenge": session.code_challenge,
            "code_challenge_method": "S256",
            "plan": "generic",
            "referrer": "sub2api",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def save_pkce_session(session: PKCESession, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")


def clear_pkce_verifier(path: Path) -> None:
    """Retain non-secret diagnostics while removing the one-time verifier."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["code_verifier"] = ""
        payload["code_challenge"] = ""
        payload["completed_at"] = time.time()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return


def parse_callback(value: str, expected_state: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise XaiPKCEError("xAI OAuth 未返回授权码")

    parsed = urlparse(raw)
    query = parsed.query
    if not query and raw.startswith("?"):
        query = raw[1:]
    if query:
        values = parse_qs(query)
        error = str((values.get("error") or [""])[0]).strip()
        if error:
            detail = str((values.get("error_description") or [error])[0]).strip()
            raise XaiPKCEError(f"xAI OAuth 授权被拒绝：{detail[:240]}")
        code = str((values.get("code") or [""])[0]).strip()
        state = str((values.get("state") or [""])[0]).strip()
        if not code:
            raise XaiPKCEError("xAI OAuth 回调缺少授权码")
        if not state or not secrets.compare_digest(state, expected_state):
            raise XaiPKCEError("xAI OAuth 回调 state 校验失败")
        return code

    # The browser helper validates state before returning a code found in page
    # content. Keep this compatibility path for providers that render the code.
    if any(char.isspace() for char in raw):
        raise XaiPKCEError("xAI OAuth 返回了无效授权码")
    return raw


def _response_detail(response: Any) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(
                payload.get("error_description")
                or payload.get("error")
                or payload.get("message")
                or ""
            )[:240]
    except Exception:
        pass
    return str(getattr(response, "text", "") or "")[:240]


def exchange_code(
    code: str,
    session: PKCESession,
    *,
    http_session: Any | None = None,
    proxy_kwargs: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    client = http_session or requests.Session()
    response = client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": session.client_id,
            "code": code,
            "redirect_uri": session.redirect_uri,
            "code_verifier": session.code_verifier,
        },
        headers={
            "Accept": "application/json",
            "User-Agent": "sub2api-grok-oauth/1.0",
        },
        timeout=timeout,
        **(proxy_kwargs or {}),
    )
    status = int(getattr(response, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        detail = _response_detail(response) or "上游未返回错误详情"
        raise XaiPKCEError(f"xAI OAuth 兑换失败（HTTP {status}）：{detail}")
    try:
        token = response.json()
    except Exception as exc:
        raise XaiPKCEError("xAI OAuth 兑换响应不是有效 JSON") from exc
    if not isinstance(token, dict):
        raise XaiPKCEError("xAI OAuth 兑换响应格式错误")
    if not str(token.get("access_token") or "").strip():
        raise XaiPKCEError("xAI OAuth 兑换响应缺少 access_token")
    if not str(token.get("refresh_token") or "").strip():
        raise XaiPKCEError("xAI OAuth 兑换响应缺少 refresh_token")
    return token


def authorize_and_exchange(
    authorize: Callable[[str, str], str],
    *,
    session_path: Path,
    should_cancel: Callable[[], Any] | None = None,
    on_progress: Callable[[str], Any] | None = None,
    http_session: Any | None = None,
    proxy_kwargs: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    def check_cancel() -> None:
        if should_cancel is not None:
            should_cancel()

    def progress(stage: str) -> None:
        if on_progress is not None:
            on_progress(stage)

    session = create_pkce_session()
    save_pkce_session(session, session_path)
    progress("session_created")
    try:
        check_cancel()
        callback = authorize(build_authorization_url(session), session.state)
        progress("callback_captured")
        check_cancel()
        code = parse_callback(callback, session.state)
        token = exchange_code(
            code,
            session,
            http_session=http_session,
            proxy_kwargs=proxy_kwargs,
            timeout=timeout,
        )
        progress("token_exchanged")
        return token
    finally:
        clear_pkce_verifier(session_path)
