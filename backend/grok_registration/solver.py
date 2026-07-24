"""Vendored xconsole loading and local Turnstile solver health checks."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any


_xconsole_ready = False
_xconsole_error: str | None = None


def ensure_xconsole(gba: Path) -> None:
    """Ensure vendored ``xconsole_client`` is importable."""
    global _xconsole_ready, _xconsole_error
    if _xconsole_ready:
        return
    if _xconsole_error:
        raise RuntimeError(_xconsole_error)

    if not gba.is_dir():
        _xconsole_error = (
            "grok-build-auth 目录不存在。请确认仓库完整检出，"
            "或重新 clone 本项目。"
        )
        raise RuntimeError(_xconsole_error)

    xconsole_dir = gba / "xconsole_client"
    if not xconsole_dir.is_dir():
        _xconsole_error = (
            "grok-build-auth/xconsole_client 不存在。"
            "请确认仓库完整检出（该目录已内置，不再使用 git submodule）。"
        )
        raise RuntimeError(_xconsole_error)

    gba_str = str(gba.resolve())
    if gba_str not in sys.path:
        sys.path.insert(0, gba_str)

    try:
        import xconsole_client  # noqa: F401
        from xconsole_client import (  # noqa: F401
            XConsoleAuthClient,
            YesCaptchaSolver,
            create_solver,
            xai_oauth_login_protocol,
        )
        from xconsole_client.oauth_protocol import (  # noqa: F401
            extract_cookies_from_auth_client,
        )
        from xconsole_client.xai_oauth import (  # noqa: F401
            CLIPROXYAPI_GROK_HEADERS,
            build_cliproxyapi_auth_record,
        )
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        if (
            missing in ("curl_cffi", "requests")
            or "curl_cffi" in str(exc)
            or "requests" in str(exc)
        ):
            _xconsole_error = (
                f"注册机依赖缺失: {missing}。请执行: pip install -r requirements.txt"
            )
        else:
            _xconsole_error = (
                f"无法导入 xconsole_client ({exc})。请执行: pip install -r requirements.txt"
            )
        raise RuntimeError(_xconsole_error) from exc
    except Exception as exc:  # noqa: BLE001
        _xconsole_error = f"加载 grok-build-auth 失败: {exc}"
        raise RuntimeError(_xconsole_error) from exc

    _xconsole_ready = True
    _xconsole_error = None


def local_solver_base_url(
    url: str | None = None,
    *,
    configured_url: str | None = None,
) -> str:
    """Always pin local captcha traffic to the inline loopback solver."""
    raw = (
        (url or "").strip()
        or (configured_url or "").strip()
        or os.environ.get("GROK2API_LOCAL_SOLVER_URL")
        or os.environ.get("LOCAL_SOLVER_URL")
        or "http://127.0.0.1:5072"
    ).strip().rstrip("/")
    if not raw or ("127.0.0.1" not in raw and "localhost" not in raw):
        return "http://127.0.0.1:5072"
    return raw


def probe_local_solver(
    url: str | None = None,
    *,
    timeout: float = 2.0,
    configured_url: str | None = None,
) -> dict[str, Any]:
    """Probe inline Turnstile Solver readiness."""
    base = local_solver_base_url(url, configured_url=configured_url)
    out: dict[str, Any] = {
        "ok": False,
        "ready": False,
        "url": base,
        "http_up": False,
        "lazy": None,
        "pool_ready": None,
        "error": None,
        "status_code": None,
        "thread": None,
        "queue": None,
        "owned": None,
        "in_flight": None,
    }
    try:
        import urllib.error
        import urllib.request
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"urllib unavailable: {exc}"
        return out

    try:
        request = urllib.request.Request(
            f"{base}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=max(0.5, float(timeout))) as response:
            out["status_code"] = int(getattr(response, "status", 200) or 200)
            body = response.read().decode("utf-8", errors="replace")
        out["http_up"] = True
        try:
            data = json.loads(body) if body else {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            out["lazy"] = data.get("lazy")
            out["pool_ready"] = data.get("pool_ready")
            for key in ("thread", "queue", "owned", "in_flight"):
                out[key] = data.get(key)
            if data.get("ok") is False:
                out["error"] = f"solver health ok=false body={body[:200]}"
            else:
                out["ok"] = True
                out["ready"] = True
                return out
        else:
            out["ok"] = True
            out["ready"] = True
            return out
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"health: {exc}"

    try:
        request = urllib.request.Request(f"{base}/", method="GET")
        with urllib.request.urlopen(request, timeout=max(0.5, float(timeout))) as response:
            out["status_code"] = int(getattr(response, "status", 200) or 200)
            _ = response.read(256)
        out["http_up"] = True
        out["ok"] = True
        out["ready"] = True
        out["error"] = None
        return out
    except Exception as exc:  # noqa: BLE001
        if not out.get("error"):
            out["error"] = f"root: {exc}"
        else:
            out["error"] = f"{out['error']}; root: {exc}"
        return out


def wait_for_local_solver(
    url: str | None = None,
    *,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
    progress: Any | None = None,
    configured_url: str | None = None,
    default_wait_sec: float = 120.0,
    default_poll_sec: float = 1.0,
) -> dict[str, Any]:
    """Block until the local Turnstile Solver is HTTP-ready, or fail."""
    base = local_solver_base_url(url, configured_url=configured_url)
    try:
        wait_s = float(timeout_sec if timeout_sec is not None else default_wait_sec)
    except (TypeError, ValueError):
        wait_s = 120.0
    wait_s = max(1.0, min(wait_s, 600.0))
    try:
        every = float(poll_sec if poll_sec is not None else default_poll_sec)
    except (TypeError, ValueError):
        every = 1.0
    every = max(0.2, min(every, 5.0))

    deadline = time.time() + wait_s
    last: dict[str, Any] = {
        "ok": False,
        "ready": False,
        "url": base,
        "waited_sec": 0.0,
        "error": "not started",
    }
    started = time.time()
    attempt = 0
    while True:
        attempt += 1
        last = probe_local_solver(
            base,
            timeout=min(2.0, every + 0.5),
            configured_url=configured_url,
        )
        last["waited_sec"] = round(time.time() - started, 2)
        last["attempts"] = attempt
        last["url"] = base
        if last.get("ready"):
            last["ok"] = True
            if progress:
                try:
                    progress(f"本地过盾已就绪 url={base} waited={last['waited_sec']}s")
                except Exception:
                    pass
            return last
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        message = (
            f"等待本地过盾就绪… url={base} "
            f"attempt={attempt} left={remaining:.0f}s "
            f"err={last.get('error') or 'down'}"
        )
        if progress:
            try:
                progress(message)
            except Exception:
                pass
        if attempt == 1 or attempt % 5 == 0:
            print(f"[grok-build-auth] {message}")
        time.sleep(min(every, max(0.2, remaining)))

    last["ok"] = False
    last["ready"] = False
    last["error"] = (
        f"本地过盾服务未在 {wait_s:.0f}s 内就绪 "
        f"(url={base}; last={last.get('error') or 'unreachable'}). "
        f"请确认 inline Turnstile Solver 已启动（entrypoint / TURNSTILE_PORT），"
        f"或稍后重试注册。"
    )
    return last


def registration_available(
    *,
    gba: Path,
    adapter_build: str,
    captcha_provider: str,
    yescaptcha_key: str,
    local_solver_url: str,
) -> dict[str, Any]:
    """Return a non-raising availability probe for the registration engine."""
    moemail_configured = bool(
        os.environ.get("GROK2API_MOEMAIL_API_KEY")
        or os.environ.get("MOEMAIL_API_KEY")
    )
    try:
        from config import MOEMAIL_API_KEY as configured_moemail_key

        moemail_configured = moemail_configured or bool(configured_moemail_key)
    except Exception:
        pass
    provider = (
        captcha_provider
        or os.environ.get("GROK2API_CAPTCHA_PROVIDER")
        or os.environ.get("CAPTCHA_PROVIDER")
        or "local"
    ).strip().lower()
    if provider not in {"local", "yescaptcha"}:
        provider = "local"
    local_url = local_solver_base_url(
        local_solver_url
        or os.environ.get("GROK2API_LOCAL_SOLVER_URL")
        or os.environ.get("LOCAL_SOLVER_URL")
        or "",
        configured_url=local_solver_url,
    )
    captcha_ready = bool(local_url) if provider == "local" else bool(yescaptcha_key)
    local_solver_live: dict[str, Any] | None = None
    if provider == "local":
        local_solver_live = probe_local_solver(
            local_url,
            timeout=1.2,
            configured_url=local_solver_url,
        )
        captcha_ready = bool(local_solver_live.get("ready"))
    try:
        ensure_xconsole(gba)
        return {
            "ok": True,
            "available": True,
            "engine": "dongguatanglinux/grok-build-auth",
            "path": str(gba),
            "vendored": True,
            "adapter_build": adapter_build,
            "captcha_provider": provider,
            "local_solver_url": local_url,
            "local_solver_configured": bool(local_url),
            "local_solver_ready": (
                bool(local_solver_live.get("ready")) if local_solver_live else None
            ),
            "local_solver_probe": local_solver_live,
            "yescaptcha_configured": (
                captcha_ready if provider == "local" else bool(yescaptcha_key)
            ),
            "moemail_configured": moemail_configured,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "available": False,
            "engine": "dongguatanglinux/grok-build-auth",
            "path": str(gba),
            "vendored": True,
            "adapter_build": adapter_build,
            "error": str(exc),
            "captcha_provider": provider,
            "local_solver_url": local_url,
            "local_solver_configured": bool(local_url),
            "local_solver_ready": (
                bool(local_solver_live.get("ready")) if local_solver_live else None
            ),
            "local_solver_probe": local_solver_live,
            "yescaptcha_configured": (
                captcha_ready if provider == "local" else bool(yescaptcha_key)
            ),
            "moemail_configured": moemail_configured,
        }
