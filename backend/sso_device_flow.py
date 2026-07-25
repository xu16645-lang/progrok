"""OIDC Device Flow HTTP protocol operations."""

from __future__ import annotations

from typing import Any


def _missing_token_fields(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["access_token", "refresh_token"]
    return [
        field
        for field in ("access_token", "refresh_token")
        if not str(data.get(field) or "").strip()
    ]


def request_device_code(
    ctx,
    session: Any | None = None,
    *,
    failure: dict[str, Any] | None = None,
    retries: int | None = None,
    should_cancel: Any | None = None,
) -> dict | None:
    """Request an OIDC device code, retrying rate-limited requests."""
    form = {"client_id": ctx.GROK_CLI_CLIENT_ID, "scope": ctx.OIDC_SCOPES}
    timeout = ctx._http_timeout()
    attempts = max(1, int(retries or ctx._device_flow_retries()))
    for attempt in range(1, attempts + 1):
        ctx._raise_if_cancelled(should_cancel)
        ctx._wait_device_flow_slot(should_cancel)
        if session is not None:
            try:
                response = session.post(
                    f"{ctx.OIDC_ISSUER}/oauth2/device/code",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=timeout,
                    **ctx._proxy_kwargs(),
                )
                status = int(getattr(response, "status_code", 0) or 0)
                body = (getattr(response, "text", None) or "")[:300]
                if status >= 400:
                    detail = f"HTTP {status}: {body[:200]}"
                    print(f"  [error] device/code {detail}")
                    ctx._record_failure(
                        failure,
                        stage="device_code",
                        detail=detail,
                        status=status,
                    )
                    if ctx._is_retryable_response(status, body) and attempt < attempts:
                        ctx._sleep_interruptibly(
                            ctx._device_flow_backoff_sec(attempt), should_cancel
                        )
                        continue
                    return None
                data = response.json()
                if isinstance(data, dict):
                    if failure is not None:
                        failure.clear()
                    return data
                ctx._record_failure(
                    failure,
                    stage="device_code",
                    detail="device/code returned a non-object response",
                )
                return None
            except Exception as exc:  # noqa: BLE001
                ctx._raise_if_cancelled(should_cancel)
                print(f"  [error] device/code: {exc}")
                ctx._record_failure(failure, stage="device_code", detail=exc)
                if attempt < attempts and ctx._is_retryable_network_error(exc):
                    ctx._sleep_interruptibly(
                        ctx._device_flow_backoff_sec(attempt), should_cancel
                    )
                    continue
                return None

        data = ctx.urllib.parse.urlencode(form).encode()
        request = ctx.urllib.request.Request(
            f"{ctx.OIDC_ISSUER}/oauth2/device/code",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with ctx.urllib.request.urlopen(request, timeout=timeout) as response:
                data = ctx.json.loads(response.read())
                if isinstance(data, dict):
                    if failure is not None:
                        failure.clear()
                    return data
                ctx._record_failure(
                    failure,
                    stage="device_code",
                    detail="device/code returned a non-object response",
                )
                return None
        except ctx.urllib.error.HTTPError as exc:
            body = exc.read().decode()[:300]
            detail = f"HTTP {exc.code}: {body[:200]}"
            print(f"  [error] device/code {detail}")
            ctx._record_failure(
                failure,
                stage="device_code",
                detail=detail,
                status=exc.code,
            )
            if ctx._is_retryable_response(exc.code, body) and attempt < attempts:
                ctx._sleep_interruptibly(
                    ctx._device_flow_backoff_sec(attempt), should_cancel
                )
                continue
            return None
        except Exception as exc:  # noqa: BLE001
            ctx._raise_if_cancelled(should_cancel)
            print(f"  [error] device/code: {exc}")
            ctx._record_failure(failure, stage="device_code", detail=exc)
            if attempt < attempts and ctx._is_retryable_network_error(exc):
                ctx._sleep_interruptibly(
                    ctx._device_flow_backoff_sec(attempt), should_cancel
                )
                continue
            return None
    return None


def poll_token(
    ctx,
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
    """Exchange an approved device code for an access token."""
    interval_seconds = ctx._poll_interval_sec(interval)
    deadline = ctx.time.time() + min(float(expires_in or 1800), float(timeout or 45))
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": ctx.GROK_CLI_CLIENT_ID,
        "device_code": device_code,
    }
    http_timeout = ctx._http_timeout()
    first = True
    last_detail = ""
    last_status: int | None = None
    while ctx.time.time() < deadline:
        ctx._raise_if_cancelled(should_cancel)
        if not (first and immediate):
            ctx._sleep_interruptibly(interval_seconds, should_cancel)
        first = False
        ctx._raise_if_cancelled(should_cancel)

        if session is not None:
            try:
                response = session.post(
                    f"{ctx.OIDC_ISSUER}/oauth2/token",
                    data=form,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate="chrome",
                    timeout=http_timeout,
                    **ctx._proxy_kwargs(),
                )
                status = int(getattr(response, "status_code", 0) or 0)
                if status < 400:
                    data = response.json()
                    missing_fields = _missing_token_fields(data)
                    if not missing_fields:
                        if failure is not None:
                            failure.clear()
                        return data
                    ctx._record_failure(
                        failure,
                        stage="token_poll",
                        detail=(
                            "token response missing required field(s): "
                            + ", ".join(missing_fields)
                        ),
                    )
                    return None
                try:
                    error_payload = response.json() if response.content else {}
                except Exception:
                    error_payload = {}
                error = str((error_payload or {}).get("error") or "")
                detail = error or f"HTTP {status}"
                last_detail = detail
                last_status = status
                if error == "authorization_pending":
                    continue
                if error == "slow_down":
                    interval_seconds = min(10.0, interval_seconds + 1.0)
                    continue
                print(f"  [error] token: {detail}")
                ctx._record_failure(
                    failure,
                    stage="token_poll",
                    detail=detail,
                    status=status,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                last_detail = f"network error: {exc}"
                if ctx.time.time() >= deadline:
                    print(f"  [error] token network: {exc}")
                    ctx._record_failure(
                        failure,
                        stage="token_poll",
                        detail=last_detail,
                    )
                    return None
                continue

        data = ctx.urllib.parse.urlencode(form).encode()
        request = ctx.urllib.request.Request(
            f"{ctx.OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with ctx.urllib.request.urlopen(request, timeout=http_timeout) as response:
                data = ctx.json.loads(response.read())
                missing_fields = _missing_token_fields(data)
                if not missing_fields:
                    if failure is not None:
                        failure.clear()
                    return data
                ctx._record_failure(
                    failure,
                    stage="token_poll",
                    detail=(
                        "token response missing required field(s): "
                        + ", ".join(missing_fields)
                    ),
                )
                return None
        except ctx.urllib.error.HTTPError as exc:
            try:
                error_payload = ctx.json.loads(exc.read())
            except Exception:
                error_payload = {}
            error = str(error_payload.get("error", "") or "")
            detail = error or f"HTTP {exc.code}"
            last_detail = detail
            last_status = exc.code
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval_seconds = min(10.0, interval_seconds + 1.0)
                continue
            print(f"  [error] token: {detail}")
            ctx._record_failure(
                failure,
                stage="token_poll",
                detail=detail,
                status=exc.code,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            last_detail = f"network error: {exc}"
            if ctx.time.time() >= deadline:
                print(f"  [error] token network: {exc}")
                ctx._record_failure(
                    failure,
                    stage="token_poll",
                    detail=last_detail,
                )
                return None
            continue
    detail = last_detail or "device authorization polling timed out"
    print(f"  [error] 轮询超时: {detail}")
    ctx._record_failure(
        failure,
        stage="token_poll",
        detail=detail,
        status=last_status,
        retryable=True,
    )
    return None
