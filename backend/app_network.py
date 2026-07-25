"""Extracted application network services."""

from __future__ import annotations

from typing import Any



def detect_solver(ctx):
    """Scan common loopback ports and return the first healthy local solver."""
    cfg = ctx.load_config()
    preferred_ports: list[int] = []
    try:
        parsed = ctx.urlparse(str(cfg.get("local_solver_url") or ""))
        if parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port:
            preferred_ports.append(int(parsed.port))
    except Exception:
        pass
    ports = preferred_ports + [5072] + list(range(5070, 5091))
    ports = list(dict.fromkeys((p for p in ports if 1 <= p <= 65535)))

    def probe(port: int) -> dict[str, Any]:
        url = f"http://127.0.0.1:{port}"
        adapter = ctx._get_registration_adapter()
        result = adapter.probe_local_solver(url, timeout=0.35)
        return {"port": port, "url": url, **result}

    found: list[dict[str, Any]] = []
    with ctx.ThreadPoolExecutor(max_workers=min(12, len(ports))) as pool:
        futures = [pool.submit(probe, port) for port in ports]
        for future in ctx.as_completed(futures):
            try:
                result = future.result()
                if result.get("ready"):
                    found.append(result)
            except Exception:
                pass
    rank = {port: index for index, port in enumerate(ports)}
    found.sort(key=lambda item: rank.get(int(item.get("port") or 0), 9999))
    if found:
        return {"ok": True, "found": True, "url": found[0]["url"], "solver": found[0]}
    return {
        "ok": True,
        "found": False,
        "url": None,
        "error": "未在本机 5070-5090 端口检测到 Turnstile Solver",
    }


def _normalize_detected_proxy(ctx, raw, *, scheme_hint=""):
    value = str(raw or "").strip()
    hint = str(scheme_hint or "").lower()
    if not value:
        return None
    if ";" in value or ("=" in value and "://" not in value):
        choices: dict[str, str] = {}
        for chunk in value.split(";"):
            key, separator, item = chunk.partition("=")
            if separator and item.strip():
                choices[key.strip().lower()] = item.strip()
        for key in ("https", "http", "socks", "socks5"):
            if choices.get(key):
                value = choices[key]
                hint = key
                break
    if "://" not in value:
        scheme = "socks5" if hint.startswith("socks") else "http"
        value = f"{scheme}://{value}"
    try:
        parsed = ctx.urlparse(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks5", "socks5h"}:
        return None
    if not parsed.hostname or not port:
        return None
    host = parsed.hostname
    if ":" in host and (not host.startswith("[")):
        host = f"[{host}]"
    return {
        "proxy": f"{scheme}://{host}:{port}",
        "proxy_username": ctx.unquote(parsed.username or ""),
        "proxy_password": ctx.unquote(parsed.password or ""),
    }


def _detect_windows_system_proxy(ctx):
    try:
        import winreg

        path = "Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            try:
                enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            except OSError:
                enabled = False
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except OSError:
                server = ""
            try:
                pac_url = str(winreg.QueryValueEx(key, "AutoConfigURL")[0] or "")
            except OSError:
                pac_url = ""
        if enabled and server:
            return (ctx._normalize_detected_proxy(server), "Windows 系统代理")
        return (
            None,
            "检测到 PAC 自动代理，无法直接提取固定代理地址" if pac_url else "",
        )
    except (ImportError, OSError):
        return (None, "")


def _detect_local_proxy(ctx):
    detected, note = ctx._detect_windows_system_proxy()
    if detected:
        return {"ok": True, "found": True, "source": "Windows 系统代理", **detected}
    for key in (
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
    ):
        detected = ctx._normalize_detected_proxy(ctx.os.environ.get(key, ""))
        if detected:
            return {"ok": True, "found": True, "source": f"环境变量 {key}", **detected}
    common_ports = [
        (7890, "http"),
        (7897, "http"),
        (10809, "http"),
        (10808, "socks5"),
        (1080, "socks5"),
    ]

    def listening(item: tuple[int, str]) -> tuple[int, str] | None:
        port, scheme = item
        try:
            with ctx.socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return (port, scheme)
        except OSError:
            return None

    open_ports: list[tuple[int, str]] = []
    with ctx.ThreadPoolExecutor(max_workers=len(common_ports)) as pool:
        for result in pool.map(listening, common_ports):
            if result:
                open_ports.append(result)
    if open_ports:
        rank = {port: index for index, (port, _) in enumerate(common_ports)}
        port, scheme = min(open_ports, key=lambda item: rank[item[0]])
        return {
            "ok": True,
            "found": True,
            "source": f"本地监听端口 {port}",
            "proxy": f"{scheme}://127.0.0.1:{port}",
            "proxy_username": "",
            "proxy_password": "",
        }
    return {
        "ok": True,
        "found": False,
        "proxy": None,
        "error": note or "未检测到 Windows 系统代理、代理环境变量或常见本地代理端口",
    }


def detect_proxy(ctx):
    return ctx._detect_local_proxy()
