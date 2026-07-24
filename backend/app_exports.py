"""Extracted application exports services."""

from __future__ import annotations

from typing import Any

from app_context import AppContext


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


def _download_records(ctx, batch_id=None):
    output_dir = ctx.DATA_DIR / "sso_output"
    stored_auth = ctx._stored_auth_by_email()
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(output_dir.glob("*.json")):
        try:
            data = ctx.json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        email = str(data.get("email") or "").strip()
        password = str(data.get("password") or "")
        sso = str(data.get("sso") or "").strip()
        if not sso:
            continue
        identity = (email.lower(), sso)
        if identity in seen:
            continue
        seen.add(identity)
        records.append(
            {
                **stored_auth.get(email.lower(), {}),
                "email": email,
                "password": password,
                "sso": sso,
                "created_at": data.get("created_at"),
            }
        )
    if batch_id:
        batch = None
        for target in ("grok", "chatgpt"):
            try:
                adapter = ctx._get_registration_adapter(target)
                batch = adapter.get_registration_batch(batch_id)
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
        records = [item for item in records if item["email"].lower() in emails]
    return records


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


def download_accounts(ctx, export_format=None, batch_id=None):
    records = (
        ctx._download_chatgpt_records(batch_id)
        if export_format == "chatgpt_auth_json"
        else ctx._download_records(batch_id)
    )
    if not records:
        raise ctx.HTTPException(status_code=404, detail="没有可下载的成功注册账号")
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
        },
    )
