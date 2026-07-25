"""CPA account upload protocol implementation."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from export_formats import (
    build_cpa_chatgpt_agent_identity_record,
    build_cpa_record,
    cpa_chatgpt_agent_identity_filename,
    cpa_filename,
)
from .common import cpa_api_base_url as _cpa_api_base_url, error_text as _error_text
from .sub2api import _chatgpt_agent_identity


def import_to_cpa(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    base = _cpa_api_base_url(base_url)
    key = str(api_key or "").strip()
    if not base or not key:
        return {"ok": False, "target": "cpa", "error": "CPA 地址或管理密钥未填写"}
    try:
        if _chatgpt_agent_identity(record) is not None:
            cpa = build_cpa_chatgpt_agent_identity_record(record)
            name = cpa_chatgpt_agent_identity_filename(cpa)
        else:
            access_token = str(
                record.get("access_token") or record.get("key") or ""
            ).strip()
            refresh_token = str(record.get("refresh_token") or "").strip()
            missing_fields = [
                field
                for field, value in (
                    ("access_token", access_token),
                    ("refresh_token", refresh_token),
                )
                if not value
            ]
            if missing_fields:
                return {
                    "ok": False,
                    "target": "cpa",
                    "error": (
                        "xAI OAuth 账号缺少 "
                        + "、".join(missing_fields)
                        + "，已阻止上传 CPA"
                    ),
                }
            cpa = build_cpa_record(record)
            name = cpa_filename(cpa)
    except ValueError as exc:
        return {"ok": False, "target": "cpa", "error": str(exc)}
    owned = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.post(
            f"{base}/v0/management/auth-files?name={quote(name)}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=cpa,
        )
        if response.status_code >= 300:
            return {"ok": False, "target": "cpa", "status_code": response.status_code, "error": _error_text(response)}
        return {"ok": True, "target": "cpa", "status_code": response.status_code, "filename": name}
    except httpx.HTTPError as exc:
        return {"ok": False, "target": "cpa", "error": f"网络错误：{str(exc)[:220]}"}
    finally:
        if owned:
            http.close()
