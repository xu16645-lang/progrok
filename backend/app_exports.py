"""Extracted application exports services."""

from __future__ import annotations

from typing import Any

def _stored_auth_by_email(ctx):
    result: dict[str, dict[str, Any]] = {}
    scores: dict[str, int] = {}
    for path in sorted((ctx.DATA_DIR / "accounts").glob("*.json")):
        try:
            data = ctx.json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("type") == "xai":
            candidates = [data]
        elif data.get("type") == "chatgpt":
            email = str(data.get("email") or "").strip().lower()
            if email:
                result[email] = {**data, "_source_file": path.stem}
                scores[email] = 99
            continue
        elif data.get("type") == "sub2api-data":
            candidates = []
            for account in data.get("accounts") or []:
                if not isinstance(account, dict):
                    continue
                credentials = account.get("credentials") or {}
                extra = account.get("extra") or {}
                if isinstance(credentials, dict) and isinstance(extra, dict):
                    candidates.append({**credentials, **extra})
        else:
            candidates = list(data.values())
        for item in candidates:
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            if not email:
                continue
            normalized = {
                **item,
                "access_token": item.get("access_token") or item.get("key") or "",
                "client_id": item.get("client_id") or item.get("oidc_client_id") or "",
                "_source_file": item.get("_source_file") or path.stem,
            }
            score = sum(
                (
                    bool(normalized.get(key))
                    for key in (
                        "access_token",
                        "refresh_token",
                        "id_token",
                        "sso",
                        "password",
                    )
                )
            )
            if score >= scores.get(email, -1):
                result[email] = normalized
                scores[email] = score
    return result


def _session_has_downloadable_result(session: dict[str, Any]) -> bool:
    auth_json = session.get("auth_json")
    if isinstance(auth_json, dict) and any(
        isinstance(item, dict) for item in (auth_json.get("imported") or [])
    ):
        return True
    if int(session.get("auth_json_count") or 0) > 0:
        return True
    if session.get("imported_account_ids"):
        return True
    return str(session.get("status") or "").strip().lower() in {
        "imported",
        "success",
        "completed",
    }


def _adapter_sessions(ctx, target: str) -> tuple[list[dict[str, Any]], Any]:
    try:
        adapter = ctx._get_registration_adapter(target)
    except Exception:
        return [], None
    try:
        payload = adapter.list_registration_sessions()
        sessions = payload.get("sessions") if isinstance(payload, dict) else []
    except Exception:
        sessions = []
    return [item for item in (sessions or []) if isinstance(item, dict)], adapter


def _session_scope(ctx, batch_id: str | None = None) -> dict[str, Any]:
    all_sessions: list[dict[str, Any]] = []
    selected_sessions: list[dict[str, Any]] | None = None
    selected_adapter = None
    if batch_id:
        for target in ("grok", "chatgpt"):
            sessions, adapter = _adapter_sessions(ctx, target)
            all_sessions.extend(sessions)
            if adapter is None:
                continue
            try:
                batch = adapter.get_registration_batch(batch_id)
            except Exception:
                batch = None
            if batch is not None:
                selected_sessions = [
                    item
                    for item in (batch.get("sessions") or [])
                    if isinstance(item, dict)
                ]
                selected_adapter = adapter
                break
        if selected_sessions is None:
            raise ctx.HTTPException(
                status_code=404, detail="批次不存在或服务重启后记录已失效"
            )
    else:
        for target in ("grok", "chatgpt"):
            sessions, _adapter = _adapter_sessions(ctx, target)
            all_sessions.extend(sessions)

    source = selected_sessions if selected_sessions is not None else all_sessions
    successful = {
        str(item.get("email") or "").strip().lower()
        for item in source
        if _session_has_downloadable_result(item)
        and str(item.get("email") or "").strip()
    }
    known: dict[str, bool] = {}
    for item in all_sessions:
        email = str(item.get("email") or "").strip().lower()
        if email:
            known[email] = known.get(email, False) or _session_has_downloadable_result(
                item
            )

    raw_by_id: dict[str, dict[str, Any]] = {}
    adapters = [selected_adapter] if selected_adapter is not None else []
    if not adapters:
        for target in ("grok", "chatgpt"):
            try:
                adapters.append(ctx._get_registration_adapter(target))
            except Exception:
                pass
    for adapter in adapters:
        rows = getattr(adapter, "_sessions", None)
        if not isinstance(rows, dict):
            continue
        for session_id, item in rows.items():
            if isinstance(item, dict):
                raw_by_id[str(session_id)] = item

    enriched: list[dict[str, Any]] = []
    for item in source:
        session_id = str(item.get("id") or "")
        raw = raw_by_id.get(session_id) or {}
        enriched.append({**item, **raw})
    return {
        "batch": bool(batch_id),
        "successful_emails": successful,
        "failed_only_emails": {email for email, ok in known.items() if not ok},
        "sessions": enriched,
    }


def _merge_download_record(
    records: dict[str, dict[str, Any]], item: dict[str, Any]
) -> None:
    email = str(item.get("email") or "").strip().lower()
    sso = str(item.get("sso") or "").strip()
    if not email and not sso:
        return
    key = email or f"sso:{sso}"
    current = dict(records.get(key) or {})
    for field, value in item.items():
        if value not in (None, "", [], {}) or field not in current:
            if current.get(field) in (None, "", [], {}) or field in {
                "created_at",
                "updated_at",
            }:
                current[field] = value
    if email:
        current["email"] = email
    records[key] = current


def _session_file_records(ctx) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for directory in (
        ctx.DATA_DIR / "sso_output",
        ctx.DATA_DIR / "register_sso",
    ):
        for path in sorted(directory.glob("*.json")):
            try:
                data = ctx.json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            cookies = data.get("cookies") if isinstance(data.get("cookies"), dict) else {}
            sso = str(data.get("sso") or cookies.get("sso") or cookies.get("sso-rw") or "").strip()
            email = str(data.get("email") or "").strip().lower()
            if not email and not sso:
                continue
            records.append(
                {
                    **data,
                    "email": email,
                    "sso": sso,
                    "_session_file": str(path),
                }
            )
    return records


def _download_records(ctx, batch_id=None, history_date=None):
    scope = _session_scope(ctx, batch_id)
    stored_auth = ctx._stored_auth_by_email()
    merged: dict[str, dict[str, Any]] = {}
    for email, item in stored_auth.items():
        if not isinstance(item, dict):
            continue
        agent = item.get("agent_identity")
        if isinstance(agent, dict) or str(item.get("auth_mode") or "").lower() == "agent_identity":
            continue
        _merge_download_record(merged, {**item, "email": email})
    for item in _session_file_records(ctx):
        _merge_download_record(merged, item)
    for item in scope["sessions"]:
        if _session_has_downloadable_result(item):
            _merge_download_record(merged, item)

    if history_date:
        from account_rotation import registration_history_emails

        try:
            allowed = registration_history_emails(history_date)
        except ValueError as exc:
            raise ctx.HTTPException(status_code=400, detail=str(exc)) from exc
        if not allowed:
            raise ctx.HTTPException(
                status_code=404, detail="该日期没有可导出的历史注册账号"
            )
        merged = {email: item for email, item in merged.items() if email in allowed}
    elif scope["batch"]:
        allowed = scope["successful_emails"]
        merged = {email: item for email, item in merged.items() if email in allowed}
    else:
        blocked = scope["failed_only_emails"]
        merged = {email: item for email, item in merged.items() if email not in blocked}
    return list(merged.values())


def _download_chatgpt_records(ctx, batch_id=None):
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ctx._stored_auth_by_email().values():
        if not isinstance(item, dict):
            continue
        agent = (
            item.get("agent_identity")
            if isinstance(item.get("agent_identity"), dict)
            else item
        )
        runtime_id = str(agent.get("agent_runtime_id") or "").strip()
        if not runtime_id or runtime_id in seen:
            continue
        seen.add(runtime_id)
        records.append(item)
    if batch_id:
        batch = None
        for target in ("chatgpt", "grok"):
            try:
                batch = ctx._get_registration_adapter(target).get_registration_batch(
                    batch_id
                )
                if batch is not None:
                    break
            except Exception:
                pass
        if not batch:
            raise ctx.HTTPException(
                status_code=404, detail="批次不存在或服务重启后记录已失效"
            )
        emails = {
            str(item.get("email") or "").strip().lower()
            for item in batch.get("sessions") or []
            if str(item.get("status") or "").lower()
            in {"imported", "success", "completed"}
        }
        records = [
            item for item in records if str(item.get("email") or "").lower() in emails
        ]
    return records


def download_accounts(ctx, export_format=None, batch_id=None, history_date=None):
    if export_format == "chatgpt_auth_json":
        raise ctx.HTTPException(status_code=400, detail="ChatGPT 相关下载暂时停用")
    records = (
        ctx._download_chatgpt_records(batch_id)
        if export_format == "chatgpt_auth_json"
        else _download_records(ctx, batch_id, history_date)
    )
    if not records:
        raise ctx.HTTPException(status_code=404, detail="没有可下载的成功注册账号")
    required_fields = {
        "cpa_json": ("email", "access_token", "refresh_token"),
        "sub2api_json": ("email", "access_token", "refresh_token"),
        "json": ("email", "password"),
        "email_sso": ("email", "sso"),
        "email_password_sso": ("email", "password", "sso"),
        "cookie": ("sso",),
        "pure_sso": ("sso",),
    }
    required = required_fields.get(str(export_format or "pure_sso"), ("sso",))
    eligible = [
        item
        for item in records
        if all(str(item.get(field) or "").strip() for field in required)
    ]
    skipped = len(records) - len(eligible)
    if not eligible:
        labels = {
            "access_token": "access_token",
            "refresh_token": "refresh_token",
            "password": "密码",
            "email": "邮箱",
            "sso": "SSO",
        }
        missing = "、".join(labels.get(field, field) for field in required)
        raise ctx.HTTPException(
            status_code=422,
            detail=f"成功账号存在，但没有账号同时具备当前格式需要的字段：{missing}",
        )
    records = eligible
    timestamp = ctx.datetime.now().strftime("%Y%m%d_%H%M%S")
    if export_format == "chatgpt_auth_json":
        try:
            content = (
                ctx.json.dumps(
                    ctx.build_chatgpt_auth_payload(records),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        except ValueError as exc:
            raise ctx.HTTPException(status_code=422, detail=str(exc)) from exc
        extension = "json"
        media_type = "application/json; charset=utf-8"
        filename = f"chatgpt_agent_identity_auth_{timestamp}.json"
    elif export_format == "json":
        ordinary = [
            {"email": item["email"], "password": item["password"]} for item in records
        ]
        content = ctx.json.dumps(ordinary, ensure_ascii=False, indent=2) + "\n"
        extension = "json"
        media_type = "application/json; charset=utf-8"
        filename = f"progrok_email_password_{timestamp}.json"
    elif export_format == "sub2api_json":
        content = (
            ctx.json.dumps(
                ctx.build_sub2api_payload(records), ensure_ascii=False, indent=2
            )
            + "\n"
        )
        extension = "json"
        media_type = "application/json; charset=utf-8"
        filename = f"progrok_sub2api_{timestamp}.json"
    elif export_format == "cpa_json":
        cpa_records = [ctx.build_cpa_record(item) for item in records]
        if len(cpa_records) == 1:
            content = (
                ctx.json.dumps(cpa_records[0], ensure_ascii=False, indent=2) + "\n"
            )
            extension = "json"
            media_type = "application/json; charset=utf-8"
            filename = ctx.cpa_filename(cpa_records[0])
        else:
            archive = ctx.BytesIO()
            with ctx.ZipFile(archive, "w", compression=ctx.ZIP_DEFLATED) as zf:
                for index, item in enumerate(cpa_records, start=1):
                    name = ctx.cpa_filename(item)
                    if name == "xai-unknown.json":
                        name = f"xai-account-{index}.json"
                    zf.writestr(
                        name, ctx.json.dumps(item, ensure_ascii=False, indent=2) + "\n"
                    )
            content = archive.getvalue()
            extension = "zip"
            media_type = "application/zip"
            filename = f"progrok_cpa_{timestamp}.zip"
    else:
        if export_format == "cookie":
            lines = [f"sso={item['sso']}" for item in records]
        elif export_format == "email_sso":
            lines = [f"{item['email']}----{item['sso']}" for item in records]
        elif export_format == "email_password_sso":
            lines = [
                f"{item['email']}:{item['password']}:{item['sso']}" for item in records
            ]
        else:
            lines = [item["sso"] for item in records]
        content = "\n".join(lines) + "\n"
        extension = "txt"
        media_type = "text/plain; charset=utf-8"
        filename = f"progrok_{export_format}_{timestamp}.{extension}"
    return ctx.Response(
        content=content if isinstance(content, bytes) else content.encode("utf-8"),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-ProGrok-Account-Count": str(len(records)),
            "X-ProGrok-Skipped-Count": str(skipped),
        },
    )
