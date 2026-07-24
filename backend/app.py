from __future__ import annotations

import json
import os
import socket
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import unquote, urlparse
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app_context import AppContext
from export_formats import (
    build_chatgpt_auth_payload,
    build_cpa_record,
    build_sub2api_payload,
    cpa_filename,
)
from account_rotation import (
    delete_records as delete_rotation_records,
    list_records as list_rotation_records,
    schedule_probe as schedule_rotation_probe,
    start_scheduler as start_rotation_scheduler,
    stop_scheduler as stop_rotation_scheduler,
    sync_imported_sessions,
)
import app_core as _app_core
import app_exports as _app_exports
import app_network as _app_network
import app_routes as _app_routes

BACKEND_DIR = Path(__file__).resolve().parent
APP_DIR = BACKEND_DIR.parent
CONFIG_DIR = APP_DIR / "config"
WEB_DIR = APP_DIR / "web"
RUNTIME_DIR = APP_DIR / "runtime"
DATA_DIR = RUNTIME_DIR / "data"
VENDOR_DIR = APP_DIR / "vendor"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATIC_DIR = WEB_DIR / "static"
SOLVER_PROXY_FILE = VENDOR_DIR / "turnstile-solver" / "proxies.txt"
_config_lock = RLock()
CHATGPT_SUB2API_MODELS = [
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "registration_target": "grok",
    "mail_provider": "yyds",
    "mail_api_key": "",
    "mail_base_url": "https://maliapi.215.im",
    "mail_domain": "",
    "mail_prefix": "",
    "mail_expiry_ms": 86400000,
    "mail_provider_configs": {
        "yyds": {
            "mail_base_url": "https://maliapi.215.im",
            "mail_api_key": "",
            "mail_domain": "",
        },
        "custom": {
            "mail_base_url": "",
            "mail_api_key": "",
            "mail_domain": "",
        },
        "stalwart": {
            "mail_base_url": "http://mail.gptfree.jo3.org",
            "mail_api_key": "",
            "mail_domain": "gptfree.jo3.org",
        },
    },
    "hotmail_local_base_url": "http://127.0.0.1:17373",
    "captcha_provider": "local",
    "local_solver_url": "http://127.0.0.1:5072",
    "yescaptcha_key": "",
    "proxy": "",
    "proxy_username": "",
    "proxy_password": "",
    "proxy_strategy": "round_robin",
    "count": 1,
    "concurrency": 1,
    "stagger_ms": 1200,
    "chatgpt_step_delay_ms": 3000,
    "auto_tune_enabled": False,
    "probe_delay_sec": 0,
    "probe_model": "grok-4.5",
    "chatgpt_probe_model": "gpt-5.5",
    "probe_concurrency": 1,
    "probe_stagger_ms": 10000,
    "import_concurrency": 1,
    "import_stagger_ms": 10000,
    "auto_import_enabled": False,
    "auto_import_target": "sub2api",
    "registration_json_format": "cpa",
    "cpa_base_url": "",
    "cpa_management_key": "",
    "sub2api_base_url": "",
    "sub2api_auth_mode": "password",
    "sub2api_admin_email": "",
    "sub2api_admin_password": "",
    "sub2api_api_key": "",
    "sub2api_group_id": 0,
    "sub2api_group_name": "",
    "sub2api_xai_group_id": 0,
    "sub2api_xai_group_name": "",
    "sub2api_chatgpt_models": CHATGPT_SUB2API_MODELS,
    "grok_headless": True,
    "chatgpt_headless": True,
}


def _app_context() -> AppContext:
    """Expose live application services to extracted domains."""
    return AppContext(globals())


def load_config() -> dict[str, Any]:
    return _app_core.load_config(_app_context())


def _sync_solver_proxy_file(cfg: dict[str, Any]) -> int:
    return _app_core._sync_solver_proxy_file(_app_context(), cfg)


def save_config(data: dict[str, Any]) -> dict[str, Any]:
    return _app_core.save_config(_app_context(), data)


def _normalize_mail_provider_configs(value: Any) -> dict[str, dict[str, str]]:
    return _app_core._normalize_mail_provider_configs(_app_context(), value)


def apply_environment(cfg: dict[str, Any]) -> None:
    return _app_core.apply_environment(_app_context(), cfg)


_initial_config = load_config()
apply_environment(_initial_config)
_sync_solver_proxy_file(_initial_config)

# Lazy-load adapters based on registration target
_registration_adapters: dict[str, Any] = {}


def _get_registration_adapter(target: str | None = None) -> Any:
    return _app_core._get_registration_adapter(_app_context(), target)


# Keep the default adapter for backward compatibility
registration = _get_registration_adapter()


class Settings(BaseModel):
    registration_target: Literal["grok", "chatgpt"] = "grok"
    mail_provider: Literal["yyds", "custom", "stalwart", "hotmail_local"] = "yyds"
    mail_api_key: str = ""
    mail_base_url: str = "https://maliapi.215.im"
    mail_domain: str = ""
    mail_prefix: str = ""
    mail_expiry_ms: int = Field(86400000, ge=60000, le=604800000)
    mail_provider_configs: dict[str, dict[str, str]] = Field(default_factory=dict)
    hotmail_local_base_url: str = "http://127.0.0.1:17373"
    captcha_provider: Literal["local", "yescaptcha"] = "local"
    local_solver_url: str = "http://127.0.0.1:5072"
    yescaptcha_key: str = ""
    proxy: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_strategy: Literal["round_robin", "random", "sticky"] = "round_robin"
    count: int = Field(1, ge=0, le=10000)
    concurrency: int = Field(1, ge=0, le=10)
    stagger_ms: int = Field(1200, ge=0, le=60000)
    chatgpt_step_delay_ms: int = Field(3000, ge=0, le=30000)
    auto_tune_enabled: bool = False
    probe_delay_sec: int = Field(0, ge=0, le=600)
    probe_model: str = "grok-4.5"
    chatgpt_probe_model: str = "gpt-5.5"
    probe_concurrency: int = Field(1, ge=1, le=10)
    probe_stagger_ms: int = Field(10000, ge=0, le=60000)
    import_concurrency: int = Field(1, ge=1, le=10)
    import_stagger_ms: int = Field(10000, ge=0, le=60000)
    auto_import_enabled: bool = False
    auto_import_target: Literal["cpa", "sub2api"] = "sub2api"
    registration_json_format: Literal["cpa", "sub2api"] = "cpa"
    cpa_base_url: str = ""
    cpa_management_key: str = ""
    sub2api_base_url: str = ""
    sub2api_auth_mode: Literal["password", "api_key"] = "password"
    sub2api_admin_email: str = ""
    sub2api_admin_password: str = ""
    sub2api_api_key: str = ""
    sub2api_group_id: int = Field(0, ge=0)
    sub2api_group_name: str = ""
    sub2api_xai_group_id: int = Field(0, ge=0)
    sub2api_xai_group_name: str = ""
    sub2api_chatgpt_models: list[str] = Field(
        default_factory=lambda: list(CHATGPT_SUB2API_MODELS)
    )
    grok_headless: bool = True
    chatgpt_headless: bool = True


class Sub2APIConnectionRequest(BaseModel):
    sub2api_base_url: str = ""
    sub2api_auth_mode: Literal["password", "api_key"] = "password"
    sub2api_admin_email: str = ""
    sub2api_admin_password: str = ""
    sub2api_api_key: str = ""


class ManualJsonImportRequest(BaseModel):
    payload: Any = None
    payloads: list[Any] = Field(default_factory=list)
    settings: Settings


class HotmailImportRequest(BaseModel):
    text: str
    base_url: str = ""


class HotmailProbeRequest(BaseModel):
    base_url: str = ""


class HotmailStatusRequest(BaseModel):
    used: bool


class AccountRotationProbeRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)
    all_accounts: bool = False


class AccountRotationDeleteRequest(BaseModel):
    ids: list[str] = Field(default_factory=list)


def _post_registration_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return _app_core._post_registration_config(_app_context(), cfg)


def _manual_import_records(payload: Any) -> list[dict[str, Any]]:
    return _app_core._manual_import_records(_app_context(), payload)


def _normalize_import_record_for_registration_target(
    record: dict[str, Any], registration_target: str
) -> dict[str, Any]:
    return _app_core._normalize_import_record_for_registration_target(
        _app_context(), record, registration_target
    )


app = FastAPI(title="ProGrok 协议注册", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _rotation_session_items() -> list[tuple[str, dict[str, Any]]]:
    return _app_core._rotation_session_items(_app_context())


def _sync_account_rotation() -> int:
    return _app_core._sync_account_rotation(_app_context())


@app.on_event("startup")
def start_account_rotation() -> None:
    return _app_core.start_account_rotation(_app_context())


@app.on_event("shutdown")
def stop_account_rotation() -> None:
    return _app_core.stop_account_rotation(_app_context())


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, Any]:
    cfg = load_config()
    target = str(cfg.get("registration_target") or "grok")
    adapter = _get_registration_adapter(target)
    available = adapter.registration_available()

    # Also probe the other adapter for status display
    other_target = "chatgpt" if target == "grok" else "grok"
    other_available = None
    try:
        other_adapter = _get_registration_adapter(other_target)
        other_available = other_adapter.registration_available()
    except Exception:
        other_available = {
            "ok": False,
            "available": False,
            "error": "adapter not loaded",
        }

    return {
        "ok": bool(available.get("available")),
        "service": "progrok-registration",
        "registration_target": target,
        "registration": available,
        "registration_alt": {other_target: other_available},
        "accounts_dir": str(DATA_DIR / "accounts"),
    }


@app.get("/api/output-paths")
def output_paths() -> dict[str, Any]:
    paths = [
        {"key": "accounts", "label": "账号文件目录", "path": DATA_DIR / "accounts"},
        {"key": "auth", "label": "合并 auth.json", "path": DATA_DIR / "auth.json"},
        {"key": "sso", "label": "SSO 输出目录", "path": DATA_DIR / "sso_output"},
        {"key": "debug", "label": "注册诊断目录", "path": DATA_DIR / "register_sso"},
        {
            "key": "chatgpt_sessions",
            "label": "ChatGPT 原始 Session",
            "path": DATA_DIR / "chatgpt_sessions",
        },
    ]
    return {
        "ok": True,
        "items": [
            {**item, "path": str(item["path"]), "exists": item["path"].exists()}
            for item in paths
        ],
    }


def _stored_auth_by_email() -> dict[str, dict[str, Any]]:
    return _app_exports._stored_auth_by_email(_app_context())


def _download_records(batch_id: str | None = None) -> list[dict[str, Any]]:
    return _app_exports._download_records(_app_context(), batch_id)


def _download_chatgpt_records(batch_id: str | None = None) -> list[dict[str, Any]]:
    return _app_exports._download_chatgpt_records(_app_context(), batch_id)


@app.get("/api/download")
def download_accounts(
    export_format: Literal[
        "pure_sso",
        "cookie",
        "email_sso",
        "email_password_sso",
        "json",
        "cpa_json",
        "sub2api_json",
        "chatgpt_auth_json",
    ] = Query("pure_sso", alias="format"),
    batch_id: str | None = None,
) -> Response:
    return _app_exports.download_accounts(_app_context(), export_format, batch_id)


@app.get("/api/solver/detect")
def detect_solver() -> dict[str, Any]:
    return _app_network.detect_solver(_app_context())


def _normalize_detected_proxy(
    raw: str, *, scheme_hint: str = ""
) -> dict[str, str] | None:
    return _app_network._normalize_detected_proxy(
        _app_context(), raw, scheme_hint=scheme_hint
    )


def _detect_windows_system_proxy() -> tuple[dict[str, str] | None, str]:
    return _app_network._detect_windows_system_proxy(_app_context())


def _detect_local_proxy() -> dict[str, Any]:
    return _app_network._detect_local_proxy(_app_context())


@app.get("/api/proxy/detect")
def detect_proxy() -> dict[str, Any]:
    return _app_network.detect_proxy(_app_context())


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return _app_routes.get_config(_app_context())


@app.post("/api/sub2api/groups")
def sub2api_groups(request: Sub2APIConnectionRequest) -> dict[str, Any]:
    return _app_routes.sub2api_groups(_app_context(), request)


@app.get("/api/performance")
def performance_profile(
    provider: Literal["local", "yescaptcha"] | None = Query(None),
) -> dict[str, Any]:
    return _app_routes.performance_profile(_app_context(), provider)


@app.get("/api/mail/provider-presets")
def mail_provider_presets(response: Response) -> dict[str, Any]:
    return _app_routes.mail_provider_presets(_app_context(), response)


@app.get("/api/mail/hotmail/accounts")
def hotmail_accounts() -> dict[str, Any]:
    return _app_routes.hotmail_accounts(_app_context())


@app.post("/api/mail/hotmail/accounts/import")
def hotmail_import(request: HotmailImportRequest) -> dict[str, Any]:
    return _app_routes.hotmail_import(_app_context(), request)


@app.post("/api/mail/hotmail/accounts/probe")
def hotmail_probe_all(request: HotmailProbeRequest) -> dict[str, Any]:
    return _app_routes.hotmail_probe_all(_app_context(), request)


@app.post("/api/mail/hotmail/accounts/{account_id}/probe")
def hotmail_probe_one(account_id: str, request: HotmailProbeRequest) -> dict[str, Any]:
    return _app_routes.hotmail_probe_one(_app_context(), account_id, request)


@app.patch("/api/mail/hotmail/accounts/{account_id}")
def hotmail_set_status(
    account_id: str, request: HotmailStatusRequest
) -> dict[str, Any]:
    return _app_routes.hotmail_set_status(_app_context(), account_id, request)


@app.delete("/api/mail/hotmail/accounts/used")
def hotmail_delete_used() -> dict[str, Any]:
    return _app_routes.hotmail_delete_used(_app_context())


@app.delete("/api/mail/hotmail/accounts/{account_id}")
def hotmail_delete(account_id: str) -> dict[str, Any]:
    return _app_routes.hotmail_delete(_app_context(), account_id)


@app.post("/api/mail/hotmail/test")
def hotmail_test(settings: Settings | None = None) -> dict[str, Any]:
    return _app_routes.hotmail_test(_app_context(), settings)


@app.put("/api/config")
def put_config(settings: Settings) -> dict[str, Any]:
    return _app_routes.put_config(_app_context(), settings)


@app.post("/api/register")
def start_register(
    settings: Settings | None = None, paused: bool = False
) -> dict[str, Any]:
    return _app_routes.start_register(_app_context(), settings, paused)


@app.get("/api/sessions")
def sessions() -> dict[str, Any]:
    return _app_routes.sessions(_app_context())


@app.get("/api/account-rotation")
def account_rotation_list(
    status: str = Query(""),
    keyword: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=80),
) -> dict[str, Any]:
    return _app_routes.account_rotation_list(
        _app_context(), status, keyword, page, page_size
    )


@app.post("/api/account-rotation/probe")
def account_rotation_probe(request: AccountRotationProbeRequest) -> dict[str, Any]:
    return _app_routes.account_rotation_probe(_app_context(), request)


@app.post("/api/account-rotation/{record_id}/probe")
def account_rotation_probe_one(record_id: str) -> dict[str, Any]:
    return _app_routes.account_rotation_probe_one(_app_context(), record_id)


@app.delete("/api/account-rotation")
def account_rotation_delete(request: AccountRotationDeleteRequest) -> dict[str, Any]:
    return _app_routes.account_rotation_delete(_app_context(), request)


@app.post("/api/sessions/reset")
def reset_sessions() -> dict[str, Any]:
    return _app_routes.reset_sessions(_app_context())


@app.get("/api/sessions/{session_id}")
def session(session_id: str) -> dict[str, Any]:
    return _app_routes.session(_app_context(), session_id)


@app.get("/api/batches/{batch_id}")
def batch(batch_id: str) -> dict[str, Any]:
    return _app_routes.batch(_app_context(), batch_id)


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: str) -> dict[str, Any]:
    return _app_routes.stop_session(_app_context(), session_id)


@app.post("/api/batches/{batch_id}/stop")
def stop_batch(batch_id: str) -> dict[str, Any]:
    return _app_routes.stop_batch(_app_context(), batch_id)


@app.post("/api/batches/{batch_id}/pause")
def pause_batch(batch_id: str) -> dict[str, Any]:
    return _app_routes.pause_batch(_app_context(), batch_id)


@app.post("/api/batches/{batch_id}/resume")
def resume_batch(batch_id: str) -> dict[str, Any]:
    return _app_routes.resume_batch(_app_context(), batch_id)


@app.post("/api/sessions/{session_id}/retry-probe")
def retry_session_probe(session_id: str) -> dict[str, Any]:
    return _app_routes.retry_session_probe(_app_context(), session_id)


@app.post("/api/batches/{batch_id}/retry-probe")
def retry_batch_probe(batch_id: str) -> dict[str, Any]:
    return _app_routes.retry_batch_probe(_app_context(), batch_id)


@app.post("/api/batches/{batch_id}/pause-probe")
def pause_batch_probe(batch_id: str) -> dict[str, Any]:
    return _app_routes.pause_batch_probe(_app_context(), batch_id)


@app.post("/api/batches/{batch_id}/resume-probe")
def resume_batch_probe(batch_id: str) -> dict[str, Any]:
    return _app_routes.resume_batch_probe(_app_context(), batch_id)


@app.post("/api/sessions/{session_id}/retry-import")
def retry_session_import(session_id: str) -> dict[str, Any]:
    return _app_routes.retry_session_import(_app_context(), session_id)


@app.post("/api/batches/{batch_id}/retry-import")
def retry_batch_import(batch_id: str) -> dict[str, Any]:
    return _app_routes.retry_batch_import(_app_context(), batch_id)


@app.post("/api/import/json")
def manual_json_import(request: ManualJsonImportRequest) -> dict[str, Any]:
    return _app_routes.manual_json_import(_app_context(), request)
