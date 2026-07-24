"""ChatGPT registration probe operations."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import load_der_private_key
from nacl.bindings import crypto_sign_ed25519_sk_to_curve25519
from nacl.public import PrivateKey as Curve25519PrivateKey, SealedBox


CHATGPT_AGENT_AUTH_API_BASE = "https://auth.openai.com/api/accounts"
CHATGPT_AGENT_PROBE_INPUT = "hi"
CHATGPT_AGENT_PROBE_INSTRUCTIONS = "Reply exactly: OK"


class RegistrationContext:
    """Resolve adapter services from the live compatibility facade."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _chatgpt_probe_error(ctx, response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                detail = detail.get("message") or detail.get("error")
            return str(payload.get("message") or payload.get("error") or detail or "")[
                :300
            ]
    except Exception:
        pass
    return str(response.text or f"HTTP {response.status_code}")[:300]


def _extract_chatgpt_output_text(ctx, payload):
    """Extract model text from a non-streaming Responses payload."""
    parts: list[str] = []
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        parts.append(direct.strip())
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
                text = part.get("text") or part.get("output_text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "".join(parts).strip()


def _parse_chatgpt_probe_response(ctx, response):
    """Require both a completed upstream response and actual model reply text."""
    raw = str(response.text or "")
    completed = False
    failed_error = ""
    text_parts: list[str] = []
    saw_event = False
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        saw_event = True
        try:
            event = ctx.json.loads(data)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event_type == "response.output_text.done":
            done_text = event.get("text")
            if isinstance(done_text, str) and done_text.strip() and (not text_parts):
                text_parts.append(done_text)
        elif event_type in {"response.completed", "response.done"}:
            completed = True
            nested = event.get("response")
            if isinstance(nested, dict) and (not text_parts):
                nested_text = ctx._extract_chatgpt_output_text(nested)
                if nested_text:
                    text_parts.append(nested_text)
        elif event_type in {"response.failed", "error"}:
            error = event.get("error")
            if not error and isinstance(event.get("response"), dict):
                error = event["response"].get("error")
            if isinstance(error, dict):
                error = error.get("message") or error.get("code")
            failed_error = str(error or event.get("message") or "上游返回失败事件")[
                :300
            ]
    if not saw_event and raw.strip():
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            payload_type = str(payload.get("type") or "").strip()
            status = str(payload.get("status") or "").strip().lower()
            if payload_type in {"response.failed", "error"} or status in {
                "failed",
                "error",
                "cancelled",
            }:
                error = payload.get("error")
                if isinstance(error, dict):
                    error = error.get("message") or error.get("code")
                failed_error = str(
                    error or payload.get("message") or "上游返回失败状态"
                )[:300]
            completed = (
                payload_type in {"response.completed", "response.done"}
                or status == "completed"
            )
            output_text = ctx._extract_chatgpt_output_text(payload)
            if output_text:
                text_parts.append(output_text)
    reply = "".join(text_parts).strip()
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


def _agent_identity_fields(identity: dict[str, Any]) -> dict[str, Any]:
    nested = identity.get("agent_identity") if isinstance(identity, dict) else None
    source = nested if isinstance(nested, dict) else identity
    if not isinstance(source, dict):
        return {}
    return dict(source)


def _agent_identity_private_key(identity: dict[str, Any]) -> Ed25519PrivateKey:
    encoded = str(identity.get("agent_private_key") or "").strip()
    if not encoded:
        raise ValueError("Agent Identity 缺少私钥")
    try:
        key = load_der_private_key(base64.b64decode(encoded), password=None)
    except Exception as exc:
        raise ValueError("Agent Identity 私钥格式无效") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Agent Identity 私钥不是 Ed25519 密钥")
    return key


def _agent_identity_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_agent_identity_assertion(identity: dict[str, Any]) -> str:
    runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    task_id = str(identity.get("task_id") or "").strip()
    if not runtime_id or not task_id:
        raise ValueError("Agent Identity 缺少 runtime_id 或 task_id")
    timestamp = _agent_identity_timestamp()
    signature = _agent_identity_private_key(identity).sign(
        f"{runtime_id}:{task_id}:{timestamp}".encode("utf-8")
    )
    envelope = {
        "agent_runtime_id": runtime_id,
        "task_id": task_id,
        "timestamp": timestamp,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"AgentAssertion {encoded}"


def _decrypt_agent_identity_task(
    private_key: Ed25519PrivateKey, encrypted_task_id: str
) -> str:
    try:
        seed = private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        signing_key = seed + public_key
        curve_private = Curve25519PrivateKey(
            crypto_sign_ed25519_sk_to_curve25519(signing_key)
        )
        decrypted = SealedBox(curve_private).decrypt(
            base64.b64decode(encrypted_task_id)
        )
        task_id = decrypted.decode("utf-8").strip()
    except Exception as exc:
        raise ValueError("Agent Identity task_id 解密失败") from exc
    if not task_id:
        raise ValueError("Agent Identity task_id 解密结果为空")
    return task_id


def _task_response_error(ctx, response, operation: str) -> ValueError:
    message = ctx._chatgpt_probe_error(response) or f"HTTP {response.status_code}"
    return ValueError(f"{operation}失败：HTTP {response.status_code} {message[:240]}")


def _register_agent_identity_task(ctx, http, identity: dict[str, Any]) -> str:
    runtime_id = str(identity.get("agent_runtime_id") or "").strip()
    if not runtime_id:
        raise ValueError("Agent Identity 缺少 runtime_id")
    private_key = _agent_identity_private_key(identity)
    timestamp = _agent_identity_timestamp()
    signature = private_key.sign(f"{runtime_id}:{timestamp}".encode("utf-8"))
    response = http.post(
        f"{CHATGPT_AGENT_AUTH_API_BASE}/v1/agent/{runtime_id}/task/register",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "timestamp": timestamp,
            "signature": base64.b64encode(signature).decode("ascii"),
        },
        timeout=ctx.CHATGPT_PROBE_TIMEOUT,
    )
    if response.status_code >= 300:
        raise _task_response_error(ctx, response, "Agent Identity task 注册")
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("Agent Identity task 注册响应不是 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Agent Identity task 注册响应格式无效")
    task_id = str(payload.get("task_id") or payload.get("taskId") or "").strip()
    if task_id:
        return task_id
    encrypted = str(
        payload.get("encrypted_task_id") or payload.get("encryptedTaskId") or ""
    ).strip()
    if not encrypted:
        raise ValueError("Agent Identity task 注册响应缺少 task_id")
    return _decrypt_agent_identity_task(private_key, encrypted)


def _is_invalid_agent_task_response(response) -> bool:
    if int(getattr(response, "status_code", 0) or 0) != 401:
        return False
    try:
        body = str(response.text or "").lower()
    except Exception:
        body = ""
    compact = "".join(body.split())
    return any(
        marker in compact
        for marker in (
            '"code":"invalid_task_id"',
            '"code":"task_not_found"',
            '"code":"task_expired"',
            '"error":"invalid_task_id"',
            "invalidtask_id",
            "invalidtaskid",
            "tasknotfound",
            "taskexpired",
        )
    )


def _agent_identity_headers(ctx, identity: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Authorization": _build_agent_identity_assertion(identity),
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "OpenAI-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.144.1 (Ubuntu 22.4.0; x86_64) xterm-256color",
        "version": "0.144.1",
    }
    account_id = str(identity.get("account_id") or "").strip()
    if account_id:
        headers["chatgpt-account-id"] = account_id
    if bool(identity.get("chatgpt_account_is_fedramp")):
        headers["x-openai-fedramp"] = "true"
    return headers


def probe_chatgpt_agent_identity(
    ctx,
    identity: dict[str, Any],
    model: str = "gpt-5.5",
    *,
    proxy: str | None = None,
    client=None,
):
    """Probe ChatGPT directly with the Sub2API Agent Identity assertion flow."""
    identity = _agent_identity_fields(identity)
    model = (
        str(model or ctx.CHATGPT_DEFAULT_PROBE_MODEL).strip()
        or ctx.CHATGPT_DEFAULT_PROBE_MODEL
    )
    try:
        _agent_identity_private_key(identity)
        if not str(identity.get("agent_runtime_id") or "").strip():
            raise ValueError("Agent Identity 缺少 runtime_id")
        if not str(identity.get("task_id") or "").strip():
            raise ValueError("Agent Identity 缺少 task_id")
    except ValueError as exc:
        return {
            "ok": False,
            "available": False,
            "retryable": False,
            "classification": "invalid_credential",
            "model": model,
            "error": str(exc),
        }

    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": CHATGPT_AGENT_PROBE_INPUT}
                ],
            }
        ],
        "instructions": CHATGPT_AGENT_PROBE_INSTRUCTIONS,
        "stream": True,
        "store": False,
    }
    started = ctx.time.time()
    owned = client is None
    http = client or ctx.httpx.Client(
        proxy=proxy or None, timeout=ctx.CHATGPT_PROBE_TIMEOUT
    )
    recovered_task = False
    try:
        while True:
            response = http.post(
                ctx.CHATGPT_PROBE_URL,
                headers=_agent_identity_headers(ctx, identity),
                json=body,
                timeout=ctx.CHATGPT_PROBE_TIMEOUT,
            )
            latency_ms = int((ctx.time.time() - started) * 1000)
            if response.status_code < 400:
                parsed = ctx._parse_chatgpt_probe_response(response)
                if not parsed.get("ok"):
                    return {
                        **parsed,
                        "available": None if parsed.get("retryable") else False,
                        "model": model,
                        "status_code": response.status_code,
                        "latency_ms": latency_ms,
                        "auth_mode": "agent_identity",
                        "task_recovered": recovered_task,
                    }
                return {
                    "ok": True,
                    "available": True,
                    "classification": "available",
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                    "reply": parsed.get("reply", ""),
                    "auth_mode": "agent_identity",
                    "task_recovered": recovered_task,
                }

            if not recovered_task and _is_invalid_agent_task_response(response):
                try:
                    new_task_id = _register_agent_identity_task(ctx, http, identity)
                    from accounts import persist_chatgpt_agent_task

                    persisted = persist_chatgpt_agent_task(identity, new_task_id)
                    if not persisted.get("ok"):
                        raise ValueError(
                            str(persisted.get("error") or "Agent Identity task_id 保存失败")
                        )
                    identity["task_id"] = new_task_id
                    recovered_task = True
                    continue
                except (ValueError, ctx.httpx.HTTPError) as exc:
                    return {
                        "ok": False,
                        "available": False,
                        "retryable": False,
                        "classification": "invalid_credential",
                        "model": model,
                        "status_code": response.status_code,
                        "latency_ms": latency_ms,
                        "auth_mode": "agent_identity",
                        "error": f"Agent Identity task 恢复失败：{str(exc)[:260]}",
                    }

            error = ctx._chatgpt_probe_error(response)
            retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
            invalid = response.status_code in {401, 403}
            return {
                "ok": False,
                "available": None if retryable else False,
                "retryable": retryable,
                "classification": "invalid_credential"
                if invalid
                else "uncertain"
                if retryable
                else "model_unavailable",
                "model": model,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "auth_mode": "agent_identity",
                "task_recovered": recovered_task,
                "error": error or f"HTTP {response.status_code}",
            }
    except ctx.httpx.HTTPError as exc:
        return {
            "ok": False,
            "available": None,
            "retryable": True,
            "classification": "uncertain",
            "model": model,
            "latency_ms": int((ctx.time.time() - started) * 1000),
            "auth_mode": "agent_identity",
            "task_recovered": recovered_task,
            "error": f"ChatGPT Agent Identity 测活网络错误：{str(exc)[:220]}",
        }
    finally:
        if owned:
            http.close()


def probe_chatgpt_session(
    ctx, session_data, model="gpt-5.5", *, proxy=None, client=None
):
    """Probe a ChatGPT account through the Codex Responses endpoint."""
    token = str(session_data.get("accessToken") or "").strip()
    model = (
        str(model or ctx.CHATGPT_DEFAULT_PROBE_MODEL).strip()
        or ctx.CHATGPT_DEFAULT_PROBE_MODEL
    )
    if not token:
        return {
            "ok": False,
            "available": False,
            "classification": "invalid_credential",
            "model": model,
            "error": "ChatGPT Session 缺少 accessToken",
        }
    account = (
        session_data.get("account")
        if isinstance(session_data.get("account"), dict)
        else {}
    )
    account_id = str(session_data.get("accountId") or account.get("id") or "").strip()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "OpenAI-Beta": "responses=experimental",
        "Originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.144.1 (Ubuntu 22.4.0; x86_64) xterm-256color",
        "version": "0.144.1",
    }
    if account_id:
        headers["chatgpt-account-id"] = account_id
    body = {
        "model": model,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Reply exactly: OK"}],
            }
        ],
        "instructions": "Reply exactly: OK",
        "stream": True,
        "store": False,
    }
    started = ctx.time.time()
    owned = client is None
    http = client or ctx.httpx.Client(
        proxy=proxy or None, timeout=ctx.CHATGPT_PROBE_TIMEOUT
    )
    try:
        response = http.post(
            ctx.CHATGPT_PROBE_URL,
            headers=headers,
            json=body,
            timeout=ctx.CHATGPT_PROBE_TIMEOUT,
        )
        latency_ms = int((ctx.time.time() - started) * 1000)
        if response.status_code < 400:
            parsed = ctx._parse_chatgpt_probe_response(response)
            if not parsed.get("ok"):
                return {
                    **parsed,
                    "available": None if parsed.get("retryable") else False,
                    "model": model,
                    "status_code": response.status_code,
                    "latency_ms": latency_ms,
                }
            return {
                "ok": True,
                "available": True,
                "classification": "available",
                "model": model,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
                "reply": parsed.get("reply", ""),
            }
        error = ctx._chatgpt_probe_error(response)
        retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
        invalid = response.status_code in {401, 403}
        return {
            "ok": False,
            "available": None if retryable else False,
            "retryable": retryable,
            "classification": "invalid_credential"
            if invalid
            else "uncertain"
            if retryable
            else "model_unavailable",
            "model": model,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "error": error or f"HTTP {response.status_code}",
        }
    except ctx.httpx.HTTPError as exc:
        return {
            "ok": False,
            "available": None,
            "retryable": True,
            "classification": "uncertain",
            "model": model,
            "latency_ms": int((ctx.time.time() - started) * 1000),
            "error": f"ChatGPT 测活网络错误：{str(exc)[:220]}",
        }
    finally:
        if owned:
            http.close()


def _probe_registration_session(ctx, session_id, config=None):
    with ctx._lock:
        sess = dict(ctx._sessions.get(session_id) or {})
    if not sess:
        return {"ok": False, "error": "registration session not found"}
    session_data = ctx._session_data_for(sess)
    cfg = dict(sess.get("_post_registration") or {})
    cfg.update(dict(config or {}))
    with ctx._lock:
        current = ctx._sessions.get(session_id) or sess
        current["probe"] = {
            "state": "running",
            "model": str(cfg.get("model") or ctx.CHATGPT_DEFAULT_PROBE_MODEL),
        }
        current["updated_at"] = ctx._now()
        ctx._append_session_event(
            current,
            "probing",
            f"开始测活 ChatGPT 模型：{cfg.get('model') or ctx.CHATGPT_DEFAULT_PROBE_MODEL}",
            at=current["updated_at"],
        )
        ctx._sessions[session_id] = current
    model = str(cfg.get("model") or ctx.CHATGPT_DEFAULT_PROBE_MODEL)
    try:
        from accounts import load_chatgpt_agent_identity

        identity = load_chatgpt_agent_identity(email=str(sess.get("email") or ""))
    except RuntimeError:
        identity = None
    if identity is not None:
        result = ctx.probe_chatgpt_agent_identity(
            identity,
            model,
            proxy=str(sess.get("proxy") or "") or None,
        )
    else:
        result = ctx.probe_chatgpt_session(
            session_data,
            model,
            proxy=str(sess.get("proxy") or "") or None,
        )
    summary = {
        "state": "complete" if not result.get("retryable") else "uncertain",
        "count": 1,
        "ok": 1 if result.get("ok") else 0,
        "fail": 0 if result.get("ok") or result.get("retryable") else 1,
        "uncertain": 1 if result.get("retryable") else 0,
        "results": [result],
        "model": result.get("model"),
    }
    with ctx._lock:
        current = ctx._sessions.get(session_id) or sess
        current["probe"] = summary
        current["updated_at"] = ctx._now()
        status = (
            "probe_complete"
            if result.get("ok")
            else "probe_uncertain"
            if result.get("retryable")
            else "probe_complete"
        )
        message = (
            f"ChatGPT 测活通过：{result.get('model')}；上游回复：{result.get('reply')}"
            if result.get("ok")
            else f"ChatGPT 测活失败：{result.get('error') or '未知错误'}"
        )
        ctx._append_session_event(current, status, message, at=current["updated_at"])
        ctx._sessions[session_id] = current
    return {
        "ok": bool(result.get("ok")),
        "session_id": session_id,
        "probe": summary,
        "error": result.get("error"),
    }
