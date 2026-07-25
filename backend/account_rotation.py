"""Durable account inventory and recurring upstream health checks."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


APP_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = APP_DIR / "runtime" / "data" / "account_rotation.json"
AUTO_POLL_INTERVAL_SECONDS = 30 * 60
MAX_PROBE_WORKERS = 10
HISTORY_TIMEZONE = timezone(timedelta(hours=8))

_lock = threading.RLock()
_poll_lock = threading.Lock()
_loaded = False
_records: dict[str, dict[str, Any]] = {}
_meta: dict[str, Any] = {}
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()
_poll_running = False
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2)


def _now() -> float:
    return time.time()


def _as_timestamp(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _record_id(account_type: str, email: str, session_id: str) -> str:
    key = email.lower() or session_id
    return f"{account_type}:{key}".lower()


def _atomic_write(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(
        f".tmp.{os.getpid()}.{uuid.uuid4().hex}"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        for attempt in range(len(_REPLACE_RETRY_DELAYS) + 1):
            try:
                os.replace(temporary, STATE_PATH)
                break
            except PermissionError:
                if attempt >= len(_REPLACE_RETRY_DELAYS):
                    raise
                time.sleep(_REPLACE_RETRY_DELAYS[attempt])
    finally:
        temporary.unlink(missing_ok=True)


def _load_locked() -> None:
    global _loaded, _records, _meta
    if _loaded:
        return
    _loaded = True
    if not STATE_PATH.exists():
        _records = {}
        _meta = {"version": 1, "interval_seconds": AUTO_POLL_INTERVAL_SECONDS}
        return
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    raw_records = payload.get("records") if isinstance(payload, dict) else {}
    _records = {
        str(key): dict(value)
        for key, value in (raw_records.items() if isinstance(raw_records, dict) else [])
        if isinstance(value, dict)
    }
    raw_meta = payload.get("meta") if isinstance(payload, dict) else {}
    _meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
    _meta["version"] = 1
    _meta["interval_seconds"] = AUTO_POLL_INTERVAL_SECONDS


def _save_locked() -> None:
    _atomic_write({"version": 1, "records": _records, "meta": _meta})


def _event_time(session: dict[str, Any], matcher: Callable[[dict[str, Any]], bool], fallback: float) -> float:
    events = session.get("events") if isinstance(session.get("events"), list) else []
    for event in events:
        if isinstance(event, dict) and matcher(event):
            return _as_timestamp(event.get("at"), fallback)
    return fallback


def _registration_finished_at(session: dict[str, Any], account_type: str) -> float:
    created = _as_timestamp(session.get("created_at"), _now())
    if account_type == "chatgpt":
        statuses = {"completion", "fetching_sso", "converting", "converted"}
        markers = ("注册完成", "registration complete", "agent identity 注册完成")
    else:
        statuses = {"fetching_sso", "importing"}
        markers = ("create_account http", "创建 xai 账号", "账号创建")
    return _event_time(
        session,
        lambda event: str(event.get("status") or "").lower() in statuses
        or any(marker in str(event.get("message") or "").lower() for marker in markers),
        created,
    )


def _imported_at(session: dict[str, Any], fallback: float) -> float:
    return _event_time(
        session,
        lambda event: str(event.get("status") or "").lower() == "imported"
        or "导入成功" in str(event.get("message") or ""),
        fallback,
    )


def _import_succeeded(session: dict[str, Any]) -> bool:
    auto_import = session.get("auto_import") if isinstance(session.get("auto_import"), dict) else {}
    return bool(auto_import.get("enabled")) and auto_import.get("ok") is True


def record_imported_session(account_type: str, session: dict[str, Any]) -> dict[str, Any] | None:
    """Persist a registration session only after its configured site import succeeds."""
    normalized_type = "chatgpt" if str(account_type).lower() == "chatgpt" else "xai"
    if not isinstance(session, dict) or not _import_succeeded(session):
        return None
    email = _text(session.get("email"), 320).lower()
    session_id = _text(session.get("id"), 160)
    if not email and not session_id:
        return None
    imported_accounts = session.get("imported_accounts") if isinstance(session.get("imported_accounts"), list) else []
    imported_ids = session.get("imported_account_ids") if isinstance(session.get("imported_account_ids"), list) else []
    account_id = ""
    for item in imported_accounts:
        if isinstance(item, dict):
            if not email:
                email = _text(item.get("email"), 320).lower()
            account_id = _text(item.get("id"), 180)
            if account_id:
                break
    if not account_id:
        account_id = next((_text(item, 180) for item in imported_ids if _text(item, 180)), "")
    auto_import = session.get("auto_import") if isinstance(session.get("auto_import"), dict) else {}
    imported_at = _imported_at(session, _as_timestamp(session.get("updated_at"), _now()))
    registered_at = _registration_finished_at(session, normalized_type)
    probe_summary = session.get("probe") if isinstance(session.get("probe"), dict) else {}
    probe_results = probe_summary.get("results") if isinstance(probe_summary.get("results"), list) else []
    probe_result = next(
        (
            item
            for item in probe_results
            if isinstance(item, dict)
            and (
                not account_id
                or not item.get("account_id")
                or str(item.get("account_id")) == account_id
            )
        ),
        None,
    )
    probe_succeeded = bool(
        isinstance(probe_result, dict)
        and (probe_result.get("ok") or probe_result.get("available") is True)
    )
    probe_retryable = bool(
        isinstance(probe_result, dict)
        and (probe_result.get("retryable") or probe_result.get("status_code") == 429)
    )
    probe_error = _request_error(probe_result) if isinstance(probe_result, dict) else ""
    probe_reply = _text(
        (probe_result or {}).get("reply") or (probe_result or {}).get("message"), 420
    )
    probe_at = _as_timestamp(session.get("updated_at"), imported_at) if probe_result else None
    record_id = _record_id(normalized_type, email, session_id)
    with _lock:
        _load_locked()
        existing = dict(_records.get(record_id) or {})
        source_session_file = _text(
            session.get("session_file") or existing.get("source_session_file"), 600
        )
        site_target = _text(auto_import.get("target") or existing.get("site_target"), 60)
        oauth = session.get("oauth") if isinstance(session.get("oauth"), dict) else {}
        credential_source = _text(
            oauth.get("path") or existing.get("credential_source"), 80
        )
        if existing and not probe_result and all(
            existing.get(key) == value
            for key, value in {
                "email": email or existing.get("email") or "未记录邮箱",
                "account_type": normalized_type,
                "account_id": account_id or _text(existing.get("account_id"), 180),
                "source_session_id": session_id or _text(existing.get("source_session_id"), 160),
                "source_session_file": source_session_file,
                "site_target": site_target,
                "credential_source": credential_source,
                "imported_to_site": True,
            }.items()
        ):
            return _public_record(existing)
        record = {
            "id": record_id,
            "email": email or existing.get("email") or "未记录邮箱",
            "account_type": normalized_type,
            "account_id": account_id or _text(existing.get("account_id"), 180),
            "source_session_id": session_id or _text(existing.get("source_session_id"), 160),
            "source_session_file": source_session_file,
            "site_target": site_target,
            "credential_source": credential_source,
            "imported_to_site": True,
            "registered_at": _as_timestamp(existing.get("registered_at"), registered_at),
            "imported_at": _as_timestamp(existing.get("imported_at"), imported_at),
            "status": "normal" if probe_succeeded else _text(existing.get("status") or ("normal" if probe_retryable else "error" if probe_result else "normal"), 20),
            "request_status": "正常" if probe_succeeded else probe_error if probe_result else _text(existing.get("request_status") or "等待首次探活", 500),
            "last_probe_at": probe_at or existing.get("last_probe_at"),
            "last_probe_result": probe_reply or ("正常" if probe_succeeded else probe_error) if probe_result else _text(existing.get("last_probe_result"), 500),
            "probe_state": "uncertain" if probe_retryable else "complete" if probe_result else _text(existing.get("probe_state") or "idle", 20),
            "updated_at": _now(),
        }
        _records[record_id] = record
        _save_locked()
        return _public_record(record)


def sync_imported_sessions(items: list[tuple[str, dict[str, Any]]]) -> int:
    added = 0
    for account_type, session in items:
        if record_imported_session(account_type, session) is not None:
            added += 1
    return added


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in (
            "id",
            "email",
            "account_type",
            "status",
            "site_target",
            "imported_to_site",
            "registered_at",
            "imported_at",
            "request_status",
            "last_probe_at",
            "last_probe_result",
            "probe_state",
            "updated_at",
        )
    }


def list_records(
    *,
    status: str = "",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    normalized_status = str(status or "").lower()
    normalized_keyword = str(keyword or "").strip().lower()
    selected_page_size = page_size if page_size in {20, 50, 80} else 20
    with _lock:
        _load_locked()
        records = list(_records.values())
        if normalized_status in {"normal", "error"}:
            records = [item for item in records if str(item.get("status") or "").lower() == normalized_status]
        if normalized_keyword:
            records = [
                item
                for item in records
                if normalized_keyword
                in " ".join(
                    str(item.get(key) or "")
                    for key in ("email", "account_type", "site_target", "request_status", "last_probe_result")
                ).lower()
            ]
        records.sort(
            key=lambda item: _as_timestamp(item.get("registered_at"), 0), reverse=True
        )
        total = len(records)
        pages = max(1, (total + selected_page_size - 1) // selected_page_size)
        selected_page = max(1, min(int(page or 1), pages))
        start = (selected_page - 1) * selected_page_size
        visible = [_public_record(item) for item in records[start : start + selected_page_size]]
        normal = sum(1 for item in _records.values() if item.get("status") == "normal")
        error = sum(1 for item in _records.values() if item.get("status") == "error")
        next_auto_run_at = _as_timestamp(_meta.get("next_auto_run_at"), 0) or None
        return {
            "items": visible,
            "total": total,
            "page": selected_page,
            "page_size": selected_page_size,
            "pages": pages,
            "summary": {"total": len(_records), "normal": normal, "error": error},
            "poll": {
                "running": _poll_running,
                "interval_seconds": AUTO_POLL_INTERVAL_SECONDS,
                "last_auto_run_at": _meta.get("last_auto_run_at"),
                "next_auto_run_at": next_auto_run_at,
            },
        }


def _registration_date(record: dict[str, Any]) -> str:
    timestamp = _as_timestamp(record.get("registered_at"), 0)
    if not timestamp:
        return ""
    return datetime.fromtimestamp(timestamp, HISTORY_TIMEZONE).date().isoformat()


def list_registration_history() -> dict[str, Any]:
    """Summarize durable imported registrations by local registration date."""
    with _lock:
        _load_locked()
        grouped: dict[str, dict[str, Any]] = {}
        for record in _records.values():
            date = _registration_date(record)
            email = _text(record.get("email"), 320).lower()
            if not date or not email or email == "未记录邮箱":
                continue
            item = grouped.setdefault(
                date,
                {"date": date, "count": 0, "xai": 0, "chatgpt": 0},
            )
            item["count"] += 1
            account_type = str(record.get("account_type") or "xai").lower()
            item["chatgpt" if account_type == "chatgpt" else "xai"] += 1
        return {
            "ok": True,
            "items": [grouped[date] for date in sorted(grouped, reverse=True)],
        }


def registration_history_emails(history_date: str) -> set[str]:
    """Return unique account emails registered on one YYYY-MM-DD date."""
    try:
        normalized_date = datetime.strptime(
            str(history_date or "").strip(), "%Y-%m-%d"
        ).date().isoformat()
    except ValueError as exc:
        raise ValueError("历史日期格式无效，应为 YYYY-MM-DD") from exc
    with _lock:
        _load_locked()
        return {
            _text(record.get("email"), 320).lower()
            for record in _records.values()
            if _registration_date(record) == normalized_date
            and _text(record.get("email"), 320)
            and _text(record.get("email"), 320) != "未记录邮箱"
        }


def delete_records(ids: list[str]) -> dict[str, Any]:
    selected = {str(item or "").strip() for item in ids if str(item or "").strip()}
    with _lock:
        _load_locked()
        deleted = 0
        for record_id in selected:
            if _records.pop(record_id, None) is not None:
                deleted += 1
        if deleted:
            _save_locked()
    return {"ok": True, "deleted": deleted}


def _read_chatgpt_session(record: dict[str, Any]) -> dict[str, Any] | None:
    raw_path = _text(record.get("source_session_file"), 600)
    if not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = APP_DIR / path
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_chatgpt_identity(record: dict[str, Any]) -> dict[str, Any]:
    from accounts import load_chatgpt_agent_identity

    runtime_id = _text(record.get("account_id"), 180)
    email = _text(record.get("email"), 320).lower()
    if runtime_id:
        try:
            return load_chatgpt_agent_identity(runtime_id=runtime_id)
        except RuntimeError:
            pass
    return load_chatgpt_agent_identity(email=email)


def _load_xai_record(record: dict[str, Any]) -> dict[str, Any]:
    account_id = _text(record.get("account_id"), 180)
    if account_id:
        try:
            from model_health import _load_record

            return _load_record(account_id)
        except Exception:
            pass
    target_email = _text(record.get("email"), 320).lower()
    auth_path = APP_DIR / "runtime" / "data" / "auth.json"
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        for item in payload.values():
            if isinstance(item, dict) and _text(item.get("email"), 320).lower() == target_email:
                return item
    raise RuntimeError("未找到本地 xAI 认证文件")


def _probe_record(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    account_type = str(record.get("account_type") or "xai").lower()
    if account_type == "chatgpt":
        model = _text(config.get("chatgpt_probe_model") or "gpt-5.5", 100)
        try:
            identity = _load_chatgpt_identity(record)
        except RuntimeError:
            identity = None
        if identity is not None:
            from chatgpt_build_adapter import probe_chatgpt_agent_identity

            return probe_chatgpt_agent_identity(identity, model)

        session = _read_chatgpt_session(record)
        if not session:
            return {
                "ok": False,
                "available": False,
                "classification": "missing_credential",
                "error": "未找到本地 ChatGPT Agent Identity 或原始 Session，无法发送上游请求",
            }
        from chatgpt_build_adapter import probe_chatgpt_session

        return probe_chatgpt_session(session, model)

    from account_pipeline import probe_account

    return probe_account(
        _load_xai_record(record),
        _text(config.get("probe_model") or "grok-4.5", 100),
    )


def probe_registration_account(
    account_type: str,
    *,
    email: str,
    account_id: str,
    session_id: str,
    session_file: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Probe a newly registered account through the same path as rotation."""
    normalized_type = "chatgpt" if str(account_type).lower() == "chatgpt" else "xai"
    probe_config = dict(config)
    if normalized_type == "xai" and not probe_config.get("probe_model"):
        probe_config["probe_model"] = probe_config.get("model")
    if normalized_type == "chatgpt" and not probe_config.get("chatgpt_probe_model"):
        probe_config["chatgpt_probe_model"] = probe_config.get("model")
    return _probe_record(
        {
            "account_type": normalized_type,
            "email": _text(email, 320).lower(),
            "account_id": _text(account_id, 180),
            "source_session_id": _text(session_id, 160),
            "source_session_file": _text(session_file, 600),
        },
        probe_config,
    )


def _request_error(result: dict[str, Any]) -> str:
    error = _text(result.get("error") or result.get("message"), 420)
    status_code = result.get("status_code")
    if status_code and f"{status_code}" not in error:
        error = f"HTTP {status_code}" + (f"：{error}" if error else "")
    return error or "上游未返回可用回复"


def _finish_probe(record_id: str, result: dict[str, Any]) -> None:
    succeeded = bool(result.get("ok") or result.get("available") is True)
    status_code = result.get("status_code")
    retryable = bool(result.get("retryable")) or status_code == 429
    now = _now()
    with _lock:
        _load_locked()
        record = _records.get(record_id)
        if record is None:
            return
        error = _request_error(result)
        if succeeded:
            record["status"] = "normal"
        elif not retryable:
            record["status"] = "error"
        record["request_status"] = "正常" if succeeded else error
        record["last_probe_result"] = "正常" if succeeded else error
        record["last_probe_at"] = now
        record["probe_state"] = "uncertain" if retryable else "complete"
        record["updated_at"] = now
        _records[record_id] = record
        _save_locked()


def _run_poll(record_ids: list[str], config: dict[str, Any], source: str) -> None:
    global _poll_running
    try:
        workers = max(1, min(MAX_PROBE_WORKERS, int(config.get("probe_concurrency") or 1), len(record_ids) or 1))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="account-rotation") as executor:
            futures = {}
            for record_id in record_ids:
                with _lock:
                    record = dict(_records.get(record_id) or {})
                if record:
                    futures[executor.submit(_probe_record, record, config)] = record_id
            for future in as_completed(futures):
                record_id = futures[future]
                try:
                    result = future.result()
                    result = result if isinstance(result, dict) else {"ok": False, "error": "探活返回格式异常"}
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)[:420]}
                _finish_probe(record_id, result)
    finally:
        with _lock:
            _poll_running = False
            _meta["last_poll_finished_at"] = _now()
            _meta["last_poll_source"] = source
            _save_locked()
        _poll_lock.release()


def schedule_probe(record_ids: list[str] | None, config: dict[str, Any], *, source: str = "manual") -> dict[str, Any]:
    global _poll_running
    if not _poll_lock.acquire(blocking=False):
        return {"ok": True, "already_running": True, "scheduled": 0}
    with _lock:
        _load_locked()
        requested = {str(item or "").strip() for item in (record_ids or []) if str(item or "").strip()}
        selected = [record_id for record_id in _records if not requested or record_id in requested]
        if not selected:
            _poll_lock.release()
            return {"ok": True, "scheduled": 0}
        now = _now()
        for record_id in selected:
            record = _records[record_id]
            record["probe_state"] = "queued"
            record["request_status"] = "正在排队探活"
            record["updated_at"] = now
        _poll_running = True
        _meta["last_poll_started_at"] = now
        _meta["last_poll_source"] = source
        if source == "auto":
            _meta["last_auto_run_at"] = now
        _save_locked()
    threading.Thread(
        target=_run_poll,
        args=(selected, dict(config), source),
        daemon=True,
        name=f"account-rotation-{source}",
    ).start()
    return {"ok": True, "scheduled": len(selected), "source": source}


def start_scheduler(
    sync_callback: Callable[[], None], config_loader: Callable[[], dict[str, Any]]
) -> None:
    """Run one full inventory poll every thirty minutes without blocking FastAPI."""
    global _scheduler_thread
    with _lock:
        _load_locked()
        if not _as_timestamp(_meta.get("next_auto_run_at"), 0):
            _meta["next_auto_run_at"] = _now() + AUTO_POLL_INTERVAL_SECONDS
            _save_locked()
    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    def loop() -> None:
        while not _scheduler_stop.is_set():
            with _lock:
                _load_locked()
                next_run_at = _as_timestamp(_meta.get("next_auto_run_at"), _now() + AUTO_POLL_INTERVAL_SECONDS)
            if _scheduler_stop.wait(max(0.0, next_run_at - _now())):
                return
            try:
                sync_callback()
                schedule_probe(None, config_loader(), source="auto")
            finally:
                with _lock:
                    _load_locked()
                    _meta["last_auto_run_at"] = _now()
                    _meta["next_auto_run_at"] = _now() + AUTO_POLL_INTERVAL_SECONDS
                    _save_locked()

    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=loop, daemon=True, name="account-rotation-scheduler"
    )
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _scheduler_stop.set()
