"""Account-run snapshot validation, summary, and persistence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RecordsContext:
    """Resolve record services from the live Hotmail Helper facade."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def normalize_account_run_snapshot_record(ctx, record):
    if not isinstance(record, dict):
        return None

    email_addr = str(record.get("email") or "").strip()
    password = str(record.get("password") or "").strip()
    final_status = str(record.get("finalStatus") or "").strip().lower()
    if (
        not email_addr
        or not password
        or final_status not in {"success", "failed", "stopped", "running"}
    ):
        return None

    finished_at = str(record.get("finishedAt") or "").strip() or ctx.datetime.now(
        ctx.timezone.utc
    ).isoformat().replace("+00:00", "Z")
    retry_count = max(0, int(record.get("retryCount") or 0))
    failed_step_raw = record.get("failedStep")
    try:
        failed_step = int(failed_step_raw)
    except (TypeError, ValueError):
        failed_step = None
    if failed_step is not None and failed_step <= 0:
        failed_step = None

    auto_run_context = (
        record.get("autoRunContext")
        if isinstance(record.get("autoRunContext"), dict)
        else None
    )
    normalized_auto_run_context = None
    if auto_run_context:
        normalized_auto_run_context = {
            "currentRun": max(0, int(auto_run_context.get("currentRun") or 0)),
            "totalRuns": max(0, int(auto_run_context.get("totalRuns") or 0)),
            "attemptRun": max(0, int(auto_run_context.get("attemptRun") or 0)),
        }
        if not any(normalized_auto_run_context.values()):
            normalized_auto_run_context = None

    source = (
        "auto"
        if str(record.get("source") or "").strip().lower() == "auto"
        else "manual"
    )

    return {
        "recordId": str(record.get("recordId") or email_addr).strip() or email_addr,
        "email": email_addr,
        "password": password,
        "finalStatus": final_status,
        "finishedAt": finished_at,
        "retryCount": retry_count,
        "failureLabel": str(record.get("failureLabel") or "").strip(),
        "failureDetail": str(record.get("failureDetail") or "").strip(),
        "failedStep": failed_step,
        "source": source,
        "autoRunContext": normalized_auto_run_context,
    }


def summarize_account_run_snapshot(records):
    summary = {
        "total": 0,
        "success": 0,
        "running": 0,
        "failed": 0,
        "stopped": 0,
        "retryTotal": 0,
    }
    for item in records:
        summary["total"] += 1
        if item.get("finalStatus") == "success":
            summary["success"] += 1
        elif item.get("finalStatus") == "running":
            summary["running"] += 1
        elif item.get("finalStatus") == "failed":
            summary["failed"] += 1
        elif item.get("finalStatus") == "stopped":
            summary["stopped"] += 1
        summary["retryTotal"] += max(0, int(item.get("retryCount") or 0))
    return summary


def normalize_account_run_snapshot_payload(ctx, payload):
    if not isinstance(payload, dict):
        raise RuntimeError("Invalid account run snapshot payload")

    normalized_records = []
    for item in (
        payload.get("records") if isinstance(payload.get("records"), list) else []
    ):
        normalized = ctx.normalize_account_run_snapshot_record(item)
        if normalized:
            normalized_records.append(normalized)

    return {
        "generatedAt": str(payload.get("generatedAt") or "").strip()
        or ctx.datetime.now(ctx.timezone.utc).isoformat().replace("+00:00", "Z"),
        "summary": ctx.summarize_account_run_snapshot(normalized_records),
        "records": normalized_records,
    }


def sync_account_run_records(ctx, payload):
    normalized_payload = ctx.normalize_account_run_snapshot_payload(payload)
    ctx.os.makedirs(
        ctx.os.path.dirname(ctx.ACCOUNT_RECORDS_SNAPSHOT_PATH), exist_ok=True
    )
    with ctx.ACCOUNT_RECORDS_LOCK:
        with open(ctx.ACCOUNT_RECORDS_SNAPSHOT_PATH, "w", encoding="utf-8") as handle:
            ctx.json.dump(normalized_payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return ctx.ACCOUNT_RECORDS_SNAPSHOT_PATH
