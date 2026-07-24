"""Private ChatGPT session storage and monitor-history recovery."""

from __future__ import annotations

from typing import Any


def session_filename_part(ctx, value: Any, fallback: str) -> str:
    """Return a stable, filesystem-safe ASCII component for a session file."""
    cleaned = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in {"-", "_", "."}) else "_"
        for ch in str(value or "").strip().lower()
    ).strip("._-")
    return cleaned[:80] or fallback


def save_original_session(
    ctx,
    session_data: dict[str, Any],
    *,
    email: str,
    session_id: str,
) -> str:
    """Atomically store a raw session outside monitor state and API responses."""
    if (
        not isinstance(session_data, dict)
        or not str(session_data.get("accessToken") or "").strip()
    ):
        raise ValueError("ChatGPT Session 缺少 accessToken")

    ctx.CHATGPT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        ctx.os.chmod(ctx.CHATGPT_SESSIONS_DIR, 0o700)
    except OSError:
        pass

    email_part = ctx._session_filename_part(email, "unknown-email")
    sid_part = ctx._session_filename_part(session_id, "session")
    filename = (
        f"session-{email_part}-{sid_part}-{int(ctx._now() * 1000)}-"
        f"{ctx.uuid.uuid4().hex[:10]}.json"
    )
    target = ctx.CHATGPT_SESSIONS_DIR / filename
    temp = target.with_name(f".{target.name}.{ctx.uuid.uuid4().hex}.tmp")
    serialized = ctx.json.dumps(session_data, ensure_ascii=False, separators=(",", ":"))

    try:
        fd = ctx.os.open(
            ctx.os.fspath(temp), ctx.os.O_WRONLY | ctx.os.O_CREAT | ctx.os.O_EXCL, 0o600
        )
        with ctx.os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            ctx.os.fsync(handle.fileno())
        ctx.os.replace(temp, target)
        try:
            ctx.os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return str(target)


def saved_session_path(ctx, session_file: Any):
    """Accept only an existing JSON file located inside the session store."""
    raw = str(session_file or "").strip()
    if not raw:
        return None
    path = ctx.Path(raw)
    if not path.is_absolute():
        return None
    try:
        root = ctx.CHATGPT_SESSIONS_DIR.resolve()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file() or resolved.suffix.lower() != ".json":
        return None
    return resolved


def load_original_session(ctx, session_file: Any) -> dict[str, Any]:
    """Load a locally retained raw session without exposing its contents."""
    path = ctx._saved_session_path(session_file)
    if path is None:
        return {}
    try:
        payload = ctx.json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ctx.json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if (
        not isinstance(payload, dict)
        or not str(payload.get("accessToken") or "").strip()
    ):
        return {}
    return payload


def session_data_for(ctx, session: dict[str, Any]) -> dict[str, Any]:
    in_memory = (
        session.get("session_data")
        if isinstance(session.get("session_data"), dict)
        else {}
    )
    if str(in_memory.get("accessToken") or "").strip():
        return in_memory
    return ctx._load_original_chatgpt_session(session.get("session_file"))


def recover_saved_history(
    ctx,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Rebuild monitor history from saved ChatGPT Agent Identity files."""
    sessions: dict[str, dict[str, Any]] = {}
    paths = sorted((ctx.RUNTIME_DATA_DIR / "accounts").glob("chatgpt-*.json"))
    if not paths:
        return {}, {}
    batch_id = "batch_cgpt_recovered_accounts"
    session_ids: list[str] = []
    timestamps: list[float] = []
    for path in paths:
        try:
            payload = ctx.json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("accounts") if isinstance(payload, dict) else []
            row = (
                rows[0]
                if isinstance(rows, list) and rows and isinstance(rows[0], dict)
                else {}
            )
            credentials = (
                row.get("credentials")
                if isinstance(row.get("credentials"), dict)
                else {}
            )
            email = (
                str(credentials.get("email") or row.get("name") or path.stem)
                .strip()
                .lower()
            )
            if not email:
                continue
            created_at = float(path.stat().st_mtime)
        except Exception:
            continue
        sid = f"cgpt_recovered_{ctx.uuid.uuid5(ctx.uuid.NAMESPACE_URL, email).hex[:20]}"
        message = "从本地 Agent Identity 认证文件恢复注册成功记录"
        sessions[sid] = {
            "id": sid,
            "email": email,
            "status": "imported",
            "message": message,
            "error": None,
            "created_at": created_at,
            "updated_at": created_at,
            "batch_id": batch_id,
            "batch_index": len(session_ids) + 1,
            "batch_total": len(paths),
            "events": [
                {
                    "at": created_at,
                    "status": "converted",
                    "message": "Agent Identity auth.json 已保存",
                },
                {"at": created_at, "status": "imported", "message": message},
            ],
            "auto_import": {
                "enabled": True,
                "target": "sub2api",
                "ok": None,
                "recovered": True,
            },
            "recovered_from": str(path),
        }
        session_ids.append(sid)
        timestamps.append(created_at)
    if not session_ids:
        return {}, {}
    for sid in session_ids:
        sessions[sid]["batch_total"] = len(session_ids)
    batch = {
        "id": batch_id,
        "batch_id": batch_id,
        "status": "done",
        "message": f"从本地认证文件恢复 {len(session_ids)} 个 ChatGPT 注册记录",
        "count": len(session_ids),
        "finished": len(session_ids),
        "ok_count": len(session_ids),
        "fail_count": 0,
        "session_ids": session_ids,
        "created_at": min(timestamps),
        "updated_at": max(timestamps),
        "runner_alive": False,
        "recovered": True,
    }
    return sessions, {batch_id: batch}
