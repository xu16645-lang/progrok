"""ChatGPT registration operations operations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RegistrationContext:
    """Resolve adapter services from the live compatibility facade."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _enqueue_import(ctx, sid):
    """Trigger auto-import in a background thread (simple version)."""

    def _import_one() -> None:
        with ctx._lock:
            sess = ctx._sessions.get(sid) or {}
        config = dict(sess.get("_post_registration") or {})
        if not config.get("auto_import_enabled"):
            return
        ctx._retry_import_now(sid, config)

    ctx.threading.Thread(target=_import_one, daemon=True).start()


def _retry_import_now(ctx, sid, config):
    """Execute auto-import for a session."""
    from account_pipeline import import_account
    from accounts import MERGED_AUTH, import_auth_payload

    target = str(config.get("target") or "sub2api").strip().lower()
    target_name = "CPA" if target == "cpa" else "Sub2API"
    output_format = "cpa" if target == "cpa" else "sub2api"
    with ctx._lock:
        sess = ctx._sessions.get(sid) or {}
    if not sess:
        return {"ok": False, "error": "session not found"}

    def set_result(result: dict[str, Any]) -> dict[str, Any]:
        ok = bool(result.get("ok"))
        error = str(result.get("error") or "")[:300]
        auto_import = {
            "enabled": True,
            "target": config.get("target", "sub2api"),
            "ok": ok,
            "imported": int(
                result.get("created") or result.get("updated") or (1 if ok else 0)
            ),
            "failed": 0 if ok else 1,
            "result": result,
            "manual_retry": True,
        }
        with ctx._lock:
            cur = ctx._sessions.get(sid) or sess
            cur["status"] = "imported" if ok else "error"
            success_message = (
                "Sub2API 导入成功，分组与可用模型已绑定"
                if target == "sub2api"
                else "CPA 导入成功"
            )
            cur["message"] = (
                success_message
                if ok
                else f"{target_name} 导入失败：{error or '未知错误'}"
            )
            cur["error"] = None if ok else error or f"{target_name} 导入失败"
            cur["auto_import"] = auto_import
            cur["updated_at"] = ctx._now()
            ctx._append_session_event(
                cur, cur["status"], cur["message"], at=cur["updated_at"]
            )
            ctx._sessions[sid] = cur
        if ok:
            try:
                from account_rotation import record_imported_session

                record_imported_session("chatgpt", dict(cur))
            except Exception:
                pass
        return result

    with ctx._lock:
        cur = ctx._sessions.get(sid) or sess
        cur["status"] = "auto_importing"
        cur["message"] = f"正在重新转换 Agent Identity JSON 并导入 {target_name}"
        cur["updated_at"] = ctx._now()
        ctx._append_session_event(
            cur, "auto_importing", cur["message"], at=cur["updated_at"]
        )
        ctx._sessions[sid] = cur
    try:
        if MERGED_AUTH.exists():
            merged = ctx.json.loads(MERGED_AUTH.read_text(encoding="utf-8"))
            if isinstance(merged, dict):
                for entry in merged.values():
                    identity = (
                        entry.get("agent_identity")
                        if isinstance(entry, dict)
                        and isinstance(entry.get("agent_identity"), dict)
                        else {}
                    )
                    if isinstance(entry, dict) and (
                        entry.get("email") == sess.get("email")
                        or identity.get("email") == sess.get("email")
                    ):
                        result = import_account(entry, config)
                        if result.get("ok"):
                            return set_result(result)
                        retryable_conversion_error = str(
                            result.get("error") or ""
                        ).lower()
                        if (
                            "private key" not in retryable_conversion_error
                            and "task_id" not in retryable_conversion_error
                        ):
                            return set_result(result)
                        break
        session_data = ctx._session_data_for(sess)
        if session_data.get("accessToken"):
            import chatgpt_session_to_auth as cs2a

            auth_dict = cs2a.session_to_auth_entry(
                session_data, proxy=str(sess.get("proxy") or ""), verify_task=True
            )
            entry = next(iter(auth_dict.values()), {})
            identity = (
                entry.get("agent_identity")
                if isinstance(entry.get("agent_identity"), dict)
                else {}
            )
            if not str(identity.get("task_id") or "").strip():
                return set_result(
                    {
                        "ok": False,
                        "error": "Agent Identity 任务注册未返回 task_id，已阻止保存和导入",
                    }
                )
            payload = {
                "key": identity.get("agent_runtime_id", ""),
                "auth_mode": "agent_identity",
                "email": identity.get("email") or sess.get("email"),
                "access_token": session_data.get("accessToken", ""),
                "agent_identity": identity,
            }
            local_result = import_auth_payload(
                payload, merge=True, output_format=output_format
            )
            if not local_result.get("ok"):
                return set_result(
                    {
                        "ok": False,
                        "error": local_result.get("error")
                        or "Agent Identity JSON 保存失败",
                    }
                )
            with ctx._lock:
                cur = ctx._sessions.get(sid) or sess
                rows = local_result.get("imported") or []
                cur["imported_account_ids"] = [
                    str(row.get("id") or "") for row in rows if row.get("id")
                ]
                cur["imported_accounts"] = [
                    {"id": row.get("id"), "email": row.get("email")} for row in rows
                ]
                cur["auth_json"] = local_result
                ctx._sessions[sid] = cur
            return set_result(import_account(entry, config))
    except Exception as exc:
        return set_result({"ok": False, "error": str(exc)[:300]})
    return set_result({"ok": False, "error": "未找到可导入的 Agent Identity JSON"})


def retry_registration_import(ctx, session_id, config):
    return ctx._retry_import_now(session_id, config)


def retry_registration_batch_import(ctx, batch_id, config):
    with ctx._lock:
        b = ctx._batches.get(batch_id) or {}
    results = []
    for sid in b.get("session_ids") or []:
        results.append(ctx._retry_import_now(sid, config))
    return {"ok": True, "batch_id": batch_id, "results": results}


def retry_registration_probe(ctx, session_id, config):
    return ctx._probe_registration_session(session_id, config)


def retry_registration_batch_probe(ctx, batch_id, config):
    with ctx._lock:
        batch = dict(ctx._batches.get(batch_id) or {})
    if not batch:
        return {"ok": False, "error": "registration batch not found"}
    with ctx._lock:
        current_batch = ctx._batches.get(batch_id) or batch
        current_batch["probe_status"] = "running"
        current_batch["probe_pause_requested"] = False
        current_batch["updated_at"] = ctx._now()
        ctx._batches[batch_id] = current_batch
    candidates = []
    with ctx._lock:
        for raw_sid in batch.get("session_ids") or []:
            sid = str(raw_sid)
            sess = ctx._sessions.get(sid) or {}
            if not ctx._session_data_for(sess).get("accessToken"):
                continue
            candidates.append(sid)
            current = ctx._sessions.get(sid) or sess
            current["probe"] = {
                "state": "queued",
                "model": str(config.get("model") or ctx.CHATGPT_DEFAULT_PROBE_MODEL),
            }
            current["updated_at"] = ctx._now()
            ctx._append_session_event(
                current, "probe_queued", "批次测活已排队", at=current["updated_at"]
            )
            ctx._sessions[sid] = current

    def run() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def probe_one(sid: str) -> dict[str, Any]:
            while True:
                with ctx._lock:
                    paused = bool(
                        (ctx._batches.get(batch_id) or {}).get("probe_pause_requested")
                    )
                if not paused:
                    break
                ctx.time.sleep(0.25)
            return ctx._probe_registration_session(sid, config)

        limit = max(
            1, min(ctx.MAX_CONCURRENCY, int(config.get("probe_concurrency") or 1))
        )
        with ThreadPoolExecutor(
            max_workers=limit, thread_name_prefix="cgpt-manual-probe"
        ) as pool:
            futures = [pool.submit(probe_one, sid) for sid in candidates]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass
        with ctx._lock:
            current_batch = ctx._batches.get(batch_id) or batch
            current_batch["probe_status"] = "completed"
            current_batch["probe_pause_requested"] = False
            current_batch["updated_at"] = ctx._now()
            ctx._batches[batch_id] = current_batch

    if candidates:
        ctx.threading.Thread(
            target=run, daemon=True, name=f"cgpt-retry-probe-{batch_id[-8:]}"
        ).start()
    else:
        with ctx._lock:
            current_batch = ctx._batches.get(batch_id) or batch
            current_batch["probe_status"] = "completed"
            current_batch["updated_at"] = ctx._now()
            ctx._batches[batch_id] = current_batch
    return {"ok": True, "batch_id": batch_id, "scheduled": len(candidates)}


def pause_registration_batch_probe(ctx, batch_id):
    with ctx._lock:
        batch = ctx._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "error": "registration batch not found"}
        batch["probe_pause_requested"] = True
        batch["probe_status"] = "paused"
        batch["updated_at"] = ctx._now()
    return {"ok": True, "batch_id": batch_id, "probe_status": "paused"}


def resume_registration_batch_probe(ctx, batch_id):
    with ctx._lock:
        batch = ctx._batches.get(batch_id)
        if batch is None:
            return {"ok": False, "error": "registration batch not found"}
        batch["probe_pause_requested"] = False
        batch["probe_status"] = "running"
        batch["updated_at"] = ctx._now()
    return {"ok": True, "batch_id": batch_id, "probe_status": "running"}


def list_registration_sessions(ctx):
    with ctx._lock:
        sessions = {
            sid: ctx._compact_session(dict(s)) for sid, s in ctx._sessions.items()
        }
        batches = {bid: dict(b) for bid, b in ctx._batches.items()}
    ctx._persist_monitor_state()
    return {"sessions": sessions, "batches": batches}


def get_registration_session(ctx, session_id):
    with ctx._lock:
        sess = ctx._sessions.get(session_id)
        if sess is None:
            return None
        return ctx._compact_session(dict(sess))


def get_registration_batch(ctx, batch_id):
    with ctx._lock:
        b = ctx._batches.get(batch_id)
        if b is None:
            return None
        result = dict(b)
        sids = list(result.get("session_ids") or [])
        result["sessions"] = [
            ctx._compact_session(ctx._sessions[s]) for s in sids if s in ctx._sessions
        ]
        return result


def stop_registration_session(ctx, session_id):
    with ctx._lock:
        sess = ctx._sessions.get(session_id)
        if sess is None:
            return {"ok": False, "error": "session not found"}
        sess["cancel_requested"] = True
        sess["status"] = "cancelled"
        sess["message"] = "stopped by user"
        sess["updated_at"] = ctx._now()
        ctx._sessions[session_id] = sess
    return {"ok": True, "session_id": session_id}


def stop_registration_batch(ctx, batch_id):
    with ctx._lock:
        b = ctx._batches.get(batch_id)
        if b is None:
            return {"ok": False, "error": "batch not found"}
        b["cancel_requested"] = True
        b["status"] = "cancelled"
        b["updated_at"] = ctx._now()
        for sid in b.get("session_ids") or []:
            if sid in ctx._sessions:
                ctx._sessions[sid]["cancel_requested"] = True
                ctx._sessions[sid]["status"] = "cancelled"
                ctx._sessions[sid]["updated_at"] = ctx._now()
        ctx._batches[batch_id] = b
    return {"ok": True, "batch_id": batch_id}


def pause_registration_batch(ctx, batch_id):
    with ctx._lock:
        b = ctx._batches.get(batch_id)
        if b is None:
            return {"ok": False, "error": "batch not found"}
        b["pause_requested"] = True
        b["status"] = "paused"
        b["updated_at"] = ctx._now()
        ctx._batches[batch_id] = b
    return {"ok": True, "batch_id": batch_id}


def resume_registration_batch(ctx, batch_id, force=False):
    with ctx._lock:
        b = ctx._batches.get(batch_id)
        if b is None:
            return {"ok": False, "error": "batch not found"}
        b["pause_requested"] = False
        b["status"] = "running"
        b["updated_at"] = ctx._now()
        ctx._batches[batch_id] = b
    return {"ok": True, "batch_id": batch_id, "resumed": True}


def reset_registration_monitor(ctx):
    with ctx._lock:
        ctx._sessions.clear()
        ctx._batches.clear()
    ctx.clear_state("chatgpt")
    return {"ok": True}


def probe_local_solver(ctx, url=None, timeout=0.35):
    """ChatGPT mode doesn't use Turnstile solver. Always returns ready."""
    return {
        "ok": True,
        "ready": True,
        "url": url or "",
        "message": "chatgpt mode: no turnstile needed",
    }


def get_registration_performance_profile(ctx, provider="", local_solver_url=""):
    """Return machine performance profile (simplified for ChatGPT)."""
    try:
        from performance_tuning import machine_profile

        return machine_profile()
    except Exception:
        return {"cpu_cores": 4, "memory_gb": 8, "platform": "windows"}


def wait_pipeline_stagger(ctx, phase, delay_ms):
    """Stub for pipeline stagger; not heavily used in ChatGPT mode."""
    if delay_ms and float(delay_ms) > 0:
        ctx.time.sleep(min(float(delay_ms) / 1000.0, 10.0))
