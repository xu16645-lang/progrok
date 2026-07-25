import argparse
import email
import html
import imaplib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from functools import partial
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlencode
from urllib.request import Request, urlopen

if __package__:
    from . import hotmail_helper_mailbox as _mailbox
    from . import hotmail_helper_records as _records
    from . import hotmail_helper_sub2api as _sub2api
else:
    import hotmail_helper_mailbox as _mailbox
    import hotmail_helper_records as _records
    import hotmail_helper_sub2api as _sub2api

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17373
LIVE_TOKEN_URL = "https://login.live.com/oauth20_token.srf"
ENTRA_COMMON_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
ENTRA_CONSUMERS_TOKEN_URL = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
)
GRAPH_API_ORIGIN = "https://graph.microsoft.com"
OUTLOOK_API_ORIGIN = "https://outlook.office.com"
GRAPH_SCOPES = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
TOKEN_ENDPOINTS = {
    "live": {
        "name": "live",
        "url": LIVE_TOKEN_URL,
        "extra_data": {},
    },
    "entra-consumers-delegated": {
        "name": "entra-consumers-delegated",
        "url": ENTRA_CONSUMERS_TOKEN_URL,
        "extra_data": {
            "scope": GRAPH_SCOPES,
        },
    },
    "entra-common-delegated": {
        "name": "entra-common-delegated",
        "url": ENTRA_COMMON_TOKEN_URL,
        "extra_data": {
            "scope": GRAPH_SCOPES,
        },
    },
    "entra-common-default": {
        "name": "entra-common-default",
        "url": ENTRA_COMMON_TOKEN_URL,
        "extra_data": {
            "scope": GRAPH_DEFAULT_SCOPE,
        },
    },
    "entra-common-outlook": {
        "name": "entra-common-outlook",
        "url": ENTRA_COMMON_TOKEN_URL,
        "extra_data": {},
    },
}
IMAP_HOST = "outlook.office365.com"
IMAP_PORT = 993
REQUEST_TIMEOUT_SECONDS = 45
IMAP_TIMEOUT_SECONDS = 20
FETCH_LIMIT_DEFAULT = 5
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ACCOUNT_LOG_PATH = os.path.join(BASE_DIR, "data", "account-run-history.txt")
ACCOUNT_RECORDS_SNAPSHOT_PATH = os.path.join(
    BASE_DIR, "data", "account-run-history.json"
)
ACCOUNT_RECORDS_LOCK = threading.Lock()
DEFAULT_CODEX_AGENT_SCRIPT_PATH = r"D:\Tencent\QQNT\QQdownloads\codex_agent.py"
CODEX_AGENT_CONVERSION_TIMEOUT_SECONDS = 180


_sub2api_context = _sub2api.Sub2APIContext(globals())
_records_context = _records.RecordsContext(globals())
normalize_sub2api_base_url = partial(
    _sub2api.normalize_sub2api_base_url, _sub2api_context
)
unwrap_sub2api_data = partial(_sub2api.unwrap_sub2api_data, _sub2api_context)
sub2api_json_request = partial(_sub2api.sub2api_json_request, _sub2api_context)
login_sub2api = partial(_sub2api.login_sub2api, _sub2api_context)
list_sub2api_groups = partial(_sub2api.list_sub2api_groups, _sub2api_context)
fetch_sub2api_groups = partial(_sub2api.fetch_sub2api_groups, _sub2api_context)
find_sub2api_group_id = partial(_sub2api.find_sub2api_group_id, _sub2api_context)
import_agent_identity_to_sub2api = partial(
    _sub2api.import_agent_identity_to_sub2api, _sub2api_context
)


def normalize_server_port(raw_value, default=DEFAULT_PORT):
    candidate = (
        default if raw_value is None or str(raw_value).strip() == "" else raw_value
    )
    try:
        port = int(str(candidate).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid helper port: {raw_value}") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Helper port out of range: {port}")
    return port


def resolve_server_config(argv=None, environ=None):
    runtime_environ = environ if environ is not None else os.environ
    parser = argparse.ArgumentParser(
        description="Start the local Hotmail helper service."
    )
    parser.add_argument(
        "--host",
        default=str(runtime_environ.get("HOTMAIL_HELPER_HOST") or DEFAULT_HOST).strip()
        or DEFAULT_HOST,
        help="Server host. Defaults to HOTMAIL_HELPER_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        default=runtime_environ.get("HOTMAIL_HELPER_PORT"),
        help="Server port. Defaults to HOTMAIL_HELPER_PORT or 17373.",
    )
    args = parser.parse_args(argv)
    host = str(args.host or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = normalize_server_port(args.port, default=DEFAULT_PORT)
    except ValueError as exc:
        parser.error(str(exc))
    return {
        "host": host,
        "port": port,
    }


def json_response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    try:
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError) as exc:
        log_info(
            f"response aborted by client status={status} detail={compact_text(exc)}"
        )


def read_json_payload(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length) if length > 0 else b"{}"
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON payload: {exc}") from exc


def post_form(url, data):
    encoded = urlencode(data).encode("utf-8")
    request = Request(
        url, data=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def get_json(url, headers=None):
    request = Request(url, headers=headers or {})
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.getcode(), json.loads(response.read().decode("utf-8"))


def mask_secret(value, keep=6):
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= keep:
        return "*" * len(raw)
    return raw[:keep] + "..." + raw[-keep:]


def compact_text(value, limit=400):
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def log_info(message):
    print(f"[HotmailHelper] {message}", flush=True)


def get_message_body_content(message):
    body = message.get("body") or {}
    if not isinstance(body, dict):
        return ""
    return str(body.get("content") or "").strip()


def get_proxy_debug_context():
    names = [
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ]
    parts = []
    for name in names:
        value = str(os.environ.get(name) or "").strip()
        if value:
            parts.append(f"{name}={value}")
    return ",".join(parts) if parts else "direct"


def classify_token_refresh_failure(result):
    detail = str(result.get("error") or "").strip().lower()
    if "invalid_grant" in detail or "aadsts70000" in detail:
        return "invalid_grant"
    if "proxy authentication required" in detail:
        return "proxy_auth_failed"
    if "connection refused" in detail:
        return (
            "proxy_connect_failed"
            if get_proxy_debug_context() != "direct"
            else "connection_refused"
        )
    if (
        "eof occurred in violation of protocol" in detail
        or "wrong version number" in detail
    ):
        return (
            "proxy_tls_failed"
            if get_proxy_debug_context() != "direct"
            else "tls_failed"
        )
    if "timed out" in detail or "timeout" in detail:
        return "network_timeout"
    return "request_failed"


def log_token_refresh_failure_diagnosis(result):
    category = classify_token_refresh_failure(result)
    message = (
        f"token refresh diagnosis endpoint={result['endpoint']} category={category}"
    )
    if category.startswith("proxy_"):
        message += f" proxy={get_proxy_debug_context()}"
    elif category == "invalid_grant":
        message += " hint=refresh_token_or_scope_invalid"
    log_info(message)


def append_account_log(email_addr, password, status, recorded_at="", reason=""):
    normalized_email = str(email_addr or "").strip()
    normalized_password = str(password or "").strip()
    normalized_status = str(status or "").strip().lower()
    normalized_recorded_at = str(recorded_at or "").strip() or datetime.now(
        timezone.utc
    ).isoformat().replace("+00:00", "Z")
    normalized_reason = str(reason or "").strip().replace("\r", " ").replace("\n", " ")

    if not normalized_email or not normalized_password or not normalized_status:
        raise RuntimeError("Missing email/password/status for account log append")

    os.makedirs(os.path.dirname(ACCOUNT_LOG_PATH), exist_ok=True)
    line = f"{normalized_recorded_at}\t{normalized_email}\t{normalized_password}\t{normalized_status}\t{normalized_reason}\n"
    with ACCOUNT_RECORDS_LOCK:
        with open(ACCOUNT_LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(line)
    return ACCOUNT_LOG_PATH


def save_local_cpa_json(file_path, content, directory_path=""):
    target_path = Path(str(file_path or "").strip()).expanduser()
    if not str(target_path):
        raise RuntimeError("Missing filePath")

    if not target_path.is_absolute():
        raise RuntimeError("filePath must be absolute")

    if directory_path:
        Path(str(directory_path).strip()).expanduser().mkdir(
            parents=True, exist_ok=True
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(str(content or ""), encoding="utf-8")
    return str(target_path)


def resolve_codex_agent_python_command():
    candidates = []
    configured_python = str(os.environ.get("CODEX_AGENT_PYTHON") or "").strip()
    if configured_python:
        candidates.append((configured_python,))
    candidates.extend(
        [
            ("py", "-3"),
            ("python",),
            (sys.executable,),
        ]
    )

    seen = set()
    failures = []
    for candidate in candidates:
        executable = str(candidate[0] or "").strip()
        if not executable:
            continue
        resolved_executable = shutil.which(executable) or (
            executable if Path(executable).is_file() else ""
        )
        if not resolved_executable:
            continue
        command = (resolved_executable, *candidate[1:])
        signature = tuple(part.lower() for part in command)
        if signature in seen:
            continue
        seen.add(signature)
        try:
            result = subprocess.run(
                [*command, "-c", "import curl_cffi, cryptography"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            failures.append(f"{' '.join(command)}: {exc}")
            continue
        if result.returncode == 0:
            return list(command)
        detail = (result.stderr or result.stdout or "missing dependencies").strip()
        failures.append(f"{' '.join(command)}: {detail}")

    raise RuntimeError(
        "No Python runtime with curl_cffi and cryptography was found. "
        "Set CODEX_AGENT_PYTHON to a compatible Python executable. "
        + " | ".join(failures[-3:])
    )


def convert_codex_agent_session(session_file_path, email_addr, timestamp):
    configured_script = str(
        os.environ.get("CODEX_AGENT_SCRIPT_PATH") or DEFAULT_CODEX_AGENT_SCRIPT_PATH
    ).strip()
    script_path = Path(configured_script).expanduser()
    if not script_path.is_file():
        raise RuntimeError(f"Codex Agent conversion script not found: {script_path}")

    safe_email = (
        re.sub(r"[^a-z0-9@._+-]+", "_", str(email_addr or "").strip().lower())
        or "chatgpt-account"
    )
    output_dir = Path(__file__).resolve().parents[1] / "data" / "codex-agent-identities"
    output_dir.mkdir(parents=True, exist_ok=True)
    auth_file_path = output_dir / f"auth-{safe_email}-{timestamp}.json"
    python_command = resolve_codex_agent_python_command()
    command = [
        *python_command,
        str(script_path),
        "--file",
        str(session_file_path),
        "--output",
        str(auth_file_path),
    ]

    log_info(
        f"codex agent conversion start script={script_path} output={auth_file_path}"
    )
    try:
        result = subprocess.run(
            command,
            cwd=str(script_path.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CODEX_AGENT_CONVERSION_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Codex Agent conversion timed out after {CODEX_AGENT_CONVERSION_TIMEOUT_SECONDS} seconds"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to start Codex Agent conversion: {exc}") from exc

    if result.returncode != 0:
        detail = (
            result.stderr or result.stdout or f"exit code {result.returncode}"
        ).strip()
        raise RuntimeError(
            f"Codex Agent conversion failed: {compact_text(detail, 1800)}"
        )
    if not auth_file_path.is_file():
        raise RuntimeError("Codex Agent conversion finished without creating auth.json")

    try:
        auth_payload = json.loads(auth_file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Generated Codex Agent auth.json is invalid: {exc}"
        ) from exc
    if str(auth_payload.get("auth_mode") or "").lower() not in {
        "agent_identity",
        "agentidentity",
    } or not isinstance(auth_payload.get("agent_identity"), dict):
        raise RuntimeError("Generated file is not a Codex Agent Identity auth.json")

    identity = auth_payload.get("agent_identity") or {}
    log_info(
        "codex agent conversion success "
        f"runtimeId={identity.get('agent_runtime_id', '')} output={auth_file_path}"
    )
    return {
        "authFilePath": str(auth_file_path),
        "scriptPath": str(script_path),
        "agentRuntimeId": str(identity.get("agent_runtime_id") or ""),
        "authMode": str(auth_payload.get("auth_mode") or ""),
    }


def save_codex_agent_session(payload):
    access_token = str(
        payload.get("accessToken") or payload.get("access_token") or ""
    ).strip()
    if not access_token:
        raise RuntimeError("Missing accessToken")

    email_addr = str(payload.get("email") or "").strip().lower()
    safe_email = re.sub(r"[^a-z0-9@._+-]+", "_", email_addr) or "chatgpt-account"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    output_dir = Path(__file__).resolve().parents[1] / "data" / "codex-agent-sessions"
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"codex-agent-session-{safe_email}-{timestamp}.json"

    session_payload = dict(payload)
    session_payload["format"] = "codex-agent-session"
    session_payload["accessToken"] = access_token
    session_payload["access_token"] = access_token
    session_payload["exportedAt"] = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    file_path.write_text(
        json.dumps(session_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    conversion = convert_codex_agent_session(file_path, email_addr, timestamp)
    sub2api_config = (
        payload.get("sub2api") if isinstance(payload.get("sub2api"), dict) else {}
    )
    sub2api_result = import_agent_identity_to_sub2api(
        conversion["authFilePath"], sub2api_config
    )
    conversion["authMode"] = "agentIdentity"
    return {
        "sessionFilePath": str(file_path),
        "sub2api": sub2api_result,
        **conversion,
    }


def normalize_account_run_snapshot_record(record):
    return _records.normalize_account_run_snapshot_record(_records_context, record)


def summarize_account_run_snapshot(records):
    return _records.summarize_account_run_snapshot(records)


def normalize_account_run_snapshot_payload(payload):
    return _records.normalize_account_run_snapshot_payload(_records_context, payload)


def sync_account_run_records(payload):
    return _records.sync_account_run_records(_records_context, payload)


_mailbox_context = _mailbox.MailboxContext(globals())
try_refresh_access_token = partial(
    _mailbox.try_refresh_access_token, _mailbox_context
)
refresh_access_token = partial(_mailbox.refresh_access_token, _mailbox_context)
build_xoauth2 = partial(_mailbox.build_xoauth2, _mailbox_context)
open_mailbox = partial(_mailbox.open_mailbox, _mailbox_context)
decode_mime_header = partial(_mailbox.decode_mime_header, _mailbox_context)
extract_text_part = partial(_mailbox.extract_text_part, _mailbox_context)
mailbox_candidates = partial(_mailbox.mailbox_candidates, _mailbox_context)
normalize_mailbox_label = partial(_mailbox.normalize_mailbox_label, _mailbox_context)
normalize_mailbox_id = partial(_mailbox.normalize_mailbox_id, _mailbox_context)
select_mailbox = partial(_mailbox.select_mailbox, _mailbox_context)
to_timestamp_ms = partial(_mailbox.to_timestamp_ms, _mailbox_context)
to_iso_string = partial(_mailbox.to_iso_string, _mailbox_context)
normalize_message = partial(_mailbox.normalize_message, _mailbox_context)
fetch_messages = partial(_mailbox.fetch_messages, _mailbox_context)
fetch_messages_for_mailboxes = partial(
    _mailbox.fetch_messages_for_mailboxes, _mailbox_context
)
normalize_graph_message = partial(_mailbox.normalize_graph_message, _mailbox_context)
normalize_outlook_message = partial(
    _mailbox.normalize_outlook_message, _mailbox_context
)
fetch_graph_messages = partial(_mailbox.fetch_graph_messages, _mailbox_context)
fetch_outlook_api_messages = partial(
    _mailbox.fetch_outlook_api_messages, _mailbox_context
)
collect_imap_messages = partial(_mailbox.collect_imap_messages, _mailbox_context)
collect_graph_messages = partial(_mailbox.collect_graph_messages, _mailbox_context)
collect_outlook_messages = partial(_mailbox.collect_outlook_messages, _mailbox_context)
collect_messages = partial(_mailbox.collect_messages, _mailbox_context)
extract_code = partial(_mailbox.extract_code, _mailbox_context)
select_latest_code = partial(_mailbox.select_latest_code, _mailbox_context)


class HotmailHelperHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        request_path = urlparse(self.path).path
        if request_path in {"", "/", "/health"}:
            json_response(
                self,
                200,
                {
                    "ok": True,
                    "service": "hotmail-helper",
                    "version": 2,
                    "time": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            )
            return

        json_response(
            self, 404, {"ok": False, "error": f"Unsupported path: {self.path}"}
        )

    def do_POST(self):
        try:
            payload = read_json_payload(self)
            request_path = urlparse(self.path).path

            if request_path == "/sync-account-run-records":
                file_path = sync_account_run_records(payload)
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "filePath": file_path,
                    },
                )
                return

            if request_path == "/append-account-log":
                file_path = append_account_log(
                    payload.get("email"),
                    payload.get("password"),
                    payload.get("status"),
                    payload.get("recordedAt"),
                    payload.get("reason"),
                )
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "filePath": file_path,
                    },
                )
                return

            if request_path == "/save-auth-json":
                file_path = save_local_cpa_json(
                    payload.get("filePath"),
                    payload.get("content"),
                    payload.get("directoryPath"),
                )
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "filePath": file_path,
                    },
                )
                return

            if request_path == "/save-codex-agent-session":
                result = save_codex_agent_session(payload)
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "filePath": result["authFilePath"],
                        **result,
                    },
                )
                return

            if request_path == "/sub2api-groups":
                result = fetch_sub2api_groups(payload)
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        **result,
                    },
                )
                return

            email_addr = str(payload.get("email") or "").strip()
            client_id = str(payload.get("clientId") or "").strip()
            refresh_token = str(payload.get("refreshToken") or "").strip()
            if not email_addr or not client_id or not refresh_token:
                raise RuntimeError("Missing email/clientId/refreshToken")

            top = max(1, min(int(payload.get("top") or FETCH_LIMIT_DEFAULT), 30))
            mailboxes = (
                payload.get("mailboxes")
                if isinstance(payload.get("mailboxes"), list)
                else [payload.get("mailbox") or "INBOX"]
            )

            if request_path == "/messages":
                result = collect_messages(
                    email_addr, client_id, refresh_token, mailboxes, top
                )
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "messages": result["messages"],
                        "mailboxResults": result["mailboxResults"],
                        "nextRefreshToken": result["token_payload"].get(
                            "next_refresh_token"
                        )
                        or "",
                        "tokenEndpoint": result["token_payload"].get("token_endpoint")
                        or "",
                        "transport": result.get("transport") or "",
                    },
                )
                return

            if request_path == "/code":
                result = collect_messages(
                    email_addr, client_id, refresh_token, mailboxes, top
                )
                selected = select_latest_code(
                    result["messages"],
                    payload.get("senderFilters") or [],
                    payload.get("subjectFilters") or [],
                    payload.get("excludeCodes") or [],
                    int(payload.get("filterAfterTimestamp") or 0),
                    payload.get("requiredKeywords") or [],
                    payload.get("codePatterns") or [],
                    bool(payload.get("allowTimeFallback", True)),
                    payload.get("recipientFilters") or [],
                )
                json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "code": selected["code"],
                        "message": selected["message"],
                        "usedTimeFallback": selected["usedTimeFallback"],
                        "nextRefreshToken": result["token_payload"].get(
                            "next_refresh_token"
                        )
                        or "",
                        "tokenEndpoint": result["token_payload"].get("token_endpoint")
                        or "",
                        "transport": result.get("transport") or "",
                    },
                )
                return

            json_response(
                self, 404, {"ok": False, "error": f"Unsupported path: {self.path}"}
            )
        except Exception as exc:
            traceback.print_exc()
            json_response(self, 500, {"ok": False, "error": str(exc)})


def main(argv=None):
    config = resolve_server_config(argv)
    host = config["host"]
    port = config["port"]
    server = ThreadingHTTPServer((host, port), HotmailHelperHandler)
    print(f"Hotmail helper listening on http://{host}:{port}", flush=True)
    print(f"Account log file: {ACCOUNT_LOG_PATH}", flush=True)
    print(f"Account snapshot file: {ACCOUNT_RECORDS_SNAPSHOT_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
