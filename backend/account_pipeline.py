"""Stable facade for post-registration account probes and remote imports."""
from __future__ import annotations

from typing import Any

import httpx

from account_pipeline_parts import cpa as _cpa
from account_pipeline_parts import grok_probe as _grok_probe
from account_pipeline_parts import sub2api as _sub2api
from account_pipeline_parts.common import (
    CHATGPT_SUB2API_MODELS,
    DEFAULT_PROBE_MODEL,
    PROBE_MODELS_TIMEOUT,
    PROBE_PERMISSION_RETRY_DELAYS,
    PROBE_RESPONSE_TIMEOUT,
    PROBE_TRANSIENT_STATUS_CODES,
)
from export_formats import CPA_BASE_URL


# Private aliases remain available for existing callers while their owning
# protocol modules stay independently testable.
_cpa_api_base_url = _cpa._cpa_api_base_url
_error_text = _cpa._error_text
_is_transient_permission_error = _grok_probe._is_transient_permission_error
_models_from_payload = _grok_probe._models_from_payload
_lightweight_probe = _grok_probe._lightweight_probe
_sub2api_auth_headers = _sub2api._sub2api_auth_headers
_chatgpt_agent_identity = _sub2api._chatgpt_agent_identity
_find_sub2api_account = _sub2api._find_sub2api_account
create_grok_oauth_in_sub2api = _sub2api.create_grok_oauth_in_sub2api
import_grok_sso_to_sub2api = _sub2api.import_grok_sso_to_sub2api
probe_grok_account_in_sub2api = _sub2api.probe_grok_account_in_sub2api


def _call_with_client(
    operation: Any,
    *,
    client: httpx.Client | None,
    timeout: httpx.Timeout | float,
    **kwargs: Any,
) -> dict[str, Any]:
    """Preserve the facade's injectable ``httpx.Client`` construction point."""
    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        return operation(client=http, **kwargs)
    finally:
        if owned:
            http.close()


def probe_account(
    record: dict[str, Any],
    model: str = DEFAULT_PROBE_MODEL,
    *,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _call_with_client(
        _grok_probe.probe_account,
        client=client,
        timeout=PROBE_RESPONSE_TIMEOUT,
        record=record,
        model=model,
    )


def import_to_cpa(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _call_with_client(
        _cpa.import_to_cpa,
        client=client,
        timeout=30.0,
        record=record,
        base_url=base_url,
        api_key=api_key,
    )


def list_sub2api_groups(
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _call_with_client(
        _sub2api.list_sub2api_groups,
        client=client,
        timeout=30.0,
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        admin_email=admin_email,
        admin_password=admin_password,
    )


def import_chatgpt_to_sub2api(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    group_id: int = 0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _call_with_client(
        _sub2api.import_chatgpt_to_sub2api,
        client=client,
        timeout=30.0,
        record=record,
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        admin_email=admin_email,
        admin_password=admin_password,
        group_id=group_id,
    )


def import_to_sub2api(
    record: dict[str, Any],
    *,
    base_url: str,
    api_key: str = "",
    auth_mode: str = "password",
    admin_email: str = "",
    admin_password: str = "",
    group_id: int = 0,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    return _call_with_client(
        _sub2api.import_to_sub2api,
        client=client,
        timeout=30.0,
        record=record,
        base_url=base_url,
        api_key=api_key,
        auth_mode=auth_mode,
        admin_email=admin_email,
        admin_password=admin_password,
        group_id=group_id,
    )


def import_account(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Route an account to the configured CPA or Sub2API import protocol."""
    target = str(config.get("target") or "sub2api").strip().lower()
    if target == "cpa":
        return import_to_cpa(
            record,
            base_url=str(config.get("cpa_base_url") or ""),
            api_key=str(config.get("cpa_management_key") or ""),
        )
    if target == "sub2api":
        common = {
            "base_url": str(config.get("sub2api_base_url") or ""),
            "api_key": str(config.get("sub2api_api_key") or ""),
            "auth_mode": str(config.get("sub2api_auth_mode") or "password"),
            "admin_email": str(config.get("sub2api_admin_email") or ""),
            "admin_password": str(config.get("sub2api_admin_password") or ""),
        }
        if _chatgpt_agent_identity(record) is not None:
            return import_chatgpt_to_sub2api(
                record,
                group_id=int(config.get("sub2api_group_id") or 0),
                **common,
            )
        return import_to_sub2api(
            record,
            group_id=int(config.get("sub2api_xai_group_id") or 0),
            **common,
        )
    return {"ok": False, "target": target, "error": "不支持的自动导入目标"}
