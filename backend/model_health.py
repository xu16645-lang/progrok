"""Standalone per-account model probe."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from account_pipeline import DEFAULT_PROBE_MODEL, probe_account


APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "runtime" / "data"


def _load_record(account_id: str) -> dict[str, Any]:
    for path in (DATA_DIR / "accounts").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "xai" and str(data.get("sub") or "") == account_id:
            data["_local_account_path"] = str(path)
            return data
    merged_path = DATA_DIR / "auth.json"
    try:
        merged = json.loads(merged_path.read_text(encoding="utf-8"))
    except Exception:
        merged = {}
    if isinstance(merged, dict):
        for auth_key, item in merged.items():
            if isinstance(item, dict) and str(item.get("user_id") or item.get("principal_id") or "") == account_id:
                item["_local_auth_key"] = str(auth_key)
                return item
    raise RuntimeError("未找到待测活账号文件")


def probe_single_account(account_id: str, model: str | None = None, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    result = probe_account(_load_record(account_id), model or DEFAULT_PROBE_MODEL)
    return {
        "ok": bool(result.get("available")),
        "account_id": account_id,
        "result": {"account_id": account_id, **result},
    }
