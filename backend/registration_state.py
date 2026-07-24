"""Durable registration monitor state shared by registration adapters."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = APP_DIR / "runtime" / "data" / "registration_state"
SENSITIVE_KEYS = {
    "password",
    "access_token",
    "refresh_token",
    "id_token",
    "session_data",
    "sso",
    "auth_json",
    "_receiver",
    "_client",
    "_post_registration",
}
ACTIVE_SESSION_STATUSES = {
    "queued",
    "starting",
    "started",
    "running",
    "registering",
    "launching",
    "navigating",
    "signup_page",
    "email_filling",
    "email_submitted",
    "verification",
    "code_received",
    "profile",
    "completion",
    "fetching_sso",
    "authorizing",
    "converting",
    "probing",
    "probe_queued",
    "auto_importing",
    "import_queued",
}
ACTIVE_BATCH_STATUSES = {"queued", "starting", "started", "running", "pausing", "resuming"}


def _safe(value: Any, key: str = "") -> Any:
    if key in SENSITIVE_KEYS or key.startswith("_"):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(k): safe
            for k, v in value.items()
            if (safe := _safe(v, str(k))) is not None
        }
    if isinstance(value, (list, tuple, set)):
        return [safe for item in value if (safe := _safe(item)) is not None]
    return str(value)


def state_path(target: str) -> Path:
    name = "chatgpt" if str(target).lower() == "chatgpt" else "grok"
    return STATE_DIR / f"{name}.json"


def save_state(target: str, sessions: dict[str, Any], batches: dict[str, Any]) -> Path:
    path = state_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "target": str(target),
        "updated_at": time.time(),
        "sessions": _safe(sessions),
        "batches": _safe(batches),
    }
    temp = path.with_suffix(f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)
    return path


def load_state(target: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    path = state_path(target)
    if not path.exists():
        return {}, {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else {}
    batches = payload.get("batches") if isinstance(payload, dict) else {}
    sessions = sessions if isinstance(sessions, dict) else {}
    batches = batches if isinstance(batches, dict) else {}
    now = time.time()
    for sid, session in sessions.items():
        if not isinstance(session, dict):
            continue
        if str(session.get("status") or "").lower() in ACTIVE_SESSION_STATUSES:
            session["status"] = "error"
            session["error"] = "service_restarted"
            session["message"] = "服务重启，未完成任务已中断；历史记录已恢复"
            session["updated_at"] = now
            events = session.get("events") if isinstance(session.get("events"), list) else []
            events.append({"at": now, "status": "error", "message": session["message"]})
            session["events"] = events[-300:]
    for bid, batch in batches.items():
        if not isinstance(batch, dict):
            continue
        if str(batch.get("status") or "").lower() in ACTIVE_BATCH_STATUSES:
            batch["status"] = "partial"
            batch["message"] = "服务重启，批次未完成；历史记录已恢复"
            batch["updated_at"] = now
            batch["runner_alive"] = False
    return sessions, batches


def clear_state(target: str) -> None:
    try:
        state_path(target).unlink(missing_ok=True)
    except OSError:
        pass
