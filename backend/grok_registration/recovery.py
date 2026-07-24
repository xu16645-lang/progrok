"""Grok registration recovery operations."""

from __future__ import annotations

from typing import Any

from .flow import RegistrationContext


def _nonterminal_session_statuses(ctx: RegistrationContext) -> frozenset[str]:
    return frozenset(
        {
            "pending",
            "queued",
            "starting",
            "started",
            "running",
            "probing",
            "solving_turnstile",
            "waiting_email",
            "fetching_sso",
            "converting",
            "importing",
            "stopping",
        }
    )


def reclaim_orphaned_registration_sessions(
    ctx: RegistrationContext,
    *,
    stale_sec: float | None = None,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """Mark abandoned non-terminal sessions as error so a batch can resume.

    After process restart / image upgrade, in-flight workers die but Redis still
    shows ``solving_turnstile`` etc. Those sessions never finish, and the
    batch runner is gone — registration appears "hung mid-way".
    """
    try:
        ttl = float(
            stale_sec
            if stale_sec is not None
            else ctx.os.environ.get("GROK2API_REG_STALE_SEC", "180") or 180
        )
    except (TypeError, ValueError):
        ttl = 180.0
    ttl = max(30.0, min(ttl, 3600.0))
    now = ctx._now()
    nonterm = ctx._nonterminal_session_statuses()
    want_batch = (batch_id or "").strip() or None
    reclaimed: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    if ctx._reg_redis():
        try:
            from store import sessions_redis

            listed = sessions_redis.reg_sess_list() or []
            if isinstance(listed, list):
                sessions = [s for s in listed if isinstance(s, dict)]
        except Exception:
            sessions = []
    if not sessions:
        with ctx._lock:
            sessions = [dict(v) for v in ctx._sessions.values() if isinstance(v, dict)]
    for sess in sessions:
        sid = str(sess.get("id") or "").strip()
        if not sid:
            continue
        bid = str(sess.get("batch_id") or "").strip()
        if want_batch and bid != want_batch:
            continue
        st = str(sess.get("status") or "").strip().lower()
        if st in ctx._TERMINAL_STATUSES or st not in nonterm:
            continue
        age = now - float(sess.get("updated_at") or sess.get("created_at") or 0)
        has_runner = False
        if bid:
            with ctx._lock:
                has_runner = bool(ctx._active_batch_runners.get(bid))
            if not has_runner and ctx._reg_redis():
                try:
                    from store.redis_client import get_str as redis_get

                    has_runner = bool(redis_get(ctx._batch_runner_lock_key(bid)))
                except Exception:
                    has_runner = False
        min_age = ttl if has_runner else min(30.0, ttl)
        if age < min_age:
            continue
        msg = f"reclaimed orphan session after {age:.0f}s (status was {st}; runner_alive={has_runner})"
        with ctx._lock:
            cur = ctx._sessions.get(sid) or dict(sess)
            prev = str(cur.get("status") or "").strip().lower()
            already_terminal = prev in ctx._TERMINAL_STATUSES
            cur["status"] = "error"
            cur["error"] = msg
            cur["message"] = msg
            cur["cancel_requested"] = False
            cur["updated_at"] = now
            ctx._sessions[sid] = cur
            ctx._mirror_reg_sess(sid, dict(cur))
            if bid and (not already_terminal):
                b = ctx._batches.get(bid) or ctx._load_reg_batch(bid) or {"id": bid}
                b = dict(b)
                b["fail_count"] = int(b.get("fail_count") or 0) + 1
                b["finished"] = int(b.get("finished") or 0) + 1
                b["updated_at"] = now
                if not b.get("message"):
                    b["message"] = "reclaimed orphan sessions"
                ctx._batches[bid] = b
                ctx._mirror_reg_batch(bid, dict(b))
        reclaimed.append(
            {
                "id": sid,
                "batch_id": bid,
                "prev_status": st,
                "age_sec": int(age),
                "email": sess.get("email"),
            }
        )
    return {
        "ok": True,
        "reclaimed": len(reclaimed),
        "stale_sec": ttl,
        "items": reclaimed[:100],
    }


def resume_registration_batch(
    ctx: RegistrationContext,
    batch_id: str,
    *,
    force: bool = False,
    reclaim_stale_sec: float | None = None,
) -> dict[str, Any]:
    """Reclaim orphan sessions and re-spawn the batch runner for remaining count.

    Used after process restart when Redis still shows status=running but no
    worker is actually spawning jobs.
    """
    bid = str(batch_id or "").strip()
    if not bid:
        return {"ok": False, "error": "missing batch id"}
    batch = ctx._load_reg_batch(bid)
    if not batch:
        return {"ok": False, "error": "registration batch not found"}
    st = str(batch.get("status") or "").strip().lower()
    if st in {"done", "cancelled", "stopped"} and (not force):
        return {
            "ok": False,
            "error": f"batch already terminal ({st}); pass force=true to resume",
            "batch_id": bid,
            "status": st,
        }
    if batch.get("cancel_requested") and (not force):
        return {
            "ok": False,
            "error": "batch cancel_requested; clear stop or pass force=true",
            "batch_id": bid,
        }
    if force and ctx._reg_redis():
        try:
            from store.redis_client import get_str as redis_get

            lock_k = ctx._batch_runner_lock_key(bid)
            token = redis_get(lock_k)
            with ctx._lock:
                local_alive = bool(ctx._active_batch_runners.get(bid))
            if token and (not local_alive):
                try:
                    from store.redis_client import compare_and_delete

                    compare_and_delete(lock_k, str(token))
                except Exception:
                    try:
                        from store.redis_client import set_ex

                        set_ex(lock_k, "reclaimed", 1)
                    except Exception:
                        pass
        except Exception:
            pass
    reclaimed = ctx.reclaim_orphaned_registration_sessions(
        stale_sec=reclaim_stale_sec, batch_id=bid
    )
    count = int(batch.get("count") or 0)
    finished = int(batch.get("finished") or 0)
    sids = list(batch.get("session_ids") or [])
    terminal = 0
    if sids and ctx._reg_redis():
        try:
            from store import sessions_redis

            for sid in sids:
                sess = sessions_redis.reg_sess_get(str(sid)) or {}
                st_s = str(sess.get("status") or "").lower()
                if st_s in ctx._TERMINAL_STATUSES and st_s != "paused":
                    terminal += 1
        except Exception:
            terminal = finished
    else:
        terminal = finished
    remaining = max(0, count - max(finished, terminal))
    if remaining <= 0:
        with ctx._lock:
            b = ctx._batches.get(bid) or dict(batch)
            b["status"] = "done" if int(b.get("fail_count") or 0) == 0 else "partial"
            b["runner_alive"] = False
            b["updated_at"] = ctx._now()
            b["message"] = (
                f"resume: nothing remaining (count={count} finished={finished} terminal={terminal})"
            )
            ctx._batches[bid] = b
            ctx._mirror_reg_batch(bid, dict(b))
        return {
            "ok": True,
            "batch_id": bid,
            "remaining": 0,
            "reclaimed": reclaimed.get("reclaimed") or 0,
            "message": "batch already complete",
            "status": (ctx._load_reg_batch(bid) or {}).get("status"),
        }
    cfg = batch.get("reg_config") if isinstance(batch.get("reg_config"), dict) else {}
    provider = (
        str(cfg.get("captcha_provider") or ctx.CAPTCHA_PROVIDER or "local")
        .strip()
        .lower()
    )
    key = str(cfg.get("yescaptcha_key") or "").strip()
    proxy = str(cfg.get("proxy") or "").strip()
    workers = int(
        cfg.get("concurrency") or batch.get("concurrency") or ctx.DEFAULT_CONCURRENCY
    )
    configured_stagger = cfg.get("stagger_ms")
    if configured_stagger is None:
        configured_stagger = batch.get("stagger_ms")
    stagger = max(
        0, min(int(400 if configured_stagger is None else configured_stagger), 60000)
    )
    mail_provider = str(cfg.get("mail_provider") or "moemail").strip().lower()
    proxy_strategy = str(cfg.get("proxy_strategy") or "round_robin").strip().lower()
    with ctx._lock:
        b = ctx._batches.get(bid) or dict(batch)
        b["cancel_requested"] = False
        b["pause_requested"] = False
        if str(b.get("status") or "").lower() in {
            "pausing",
            "paused",
            "stopping",
            "cancelled",
            "stopped",
            "error",
        }:
            b["status"] = "running"
        b["updated_at"] = ctx._now()
        b["message"] = (
            f"resume requested remaining={remaining} (reclaimed={reclaimed.get('reclaimed') or 0})"
        )
        ctx._batches[bid] = b
        ctx._mirror_reg_batch(bid, dict(b))
    spawned = ctx._spawn_batch_runner(
        bid,
        remaining=remaining,
        concurrency=workers,
        stagger_ms=stagger,
        captcha_provider=provider,
        yescaptcha_key=key,
        proxy=proxy,
        proxy_strategy=proxy_strategy,
        moemail_api_key=cfg.get("moemail_api_key"),
        moemail_base_url=cfg.get("moemail_base_url"),
        prefix=cfg.get("prefix"),
        domain=cfg.get("domain"),
        expiry_ms=cfg.get("expiry_ms"),
        mail_provider=mail_provider,
        hotmail_local_base_url=cfg.get("hotmail_local_base_url"),
        post_registration=cfg.get("post_registration")
        if isinstance(cfg.get("post_registration"), dict)
        else None,
        auto_tune_enabled=bool(cfg.get("auto_tune_enabled")),
        headless=bool(cfg.get("headless", True)),
    )
    out = {
        "ok": bool(spawned.get("ok")),
        "batch_id": bid,
        "remaining": remaining,
        "reclaimed": reclaimed.get("reclaimed") or 0,
        "reclaim": reclaimed,
        "spawn": spawned,
        "message": spawned.get("message") or spawned.get("error"),
    }
    if not out["ok"]:
        out["error"] = spawned.get("error") or "spawn failed"
    return out


def get_registration_performance_profile(
    ctx: RegistrationContext,
    provider: str | None = None,
    local_solver_url: str | None = None,
) -> dict[str, Any]:
    selected = str(provider or ctx.CAPTCHA_PROVIDER or "local").strip().lower()
    solver: dict[str, Any] = {}
    solver_threads = None
    if selected == "local":
        solver = ctx.probe_local_solver(local_solver_url, timeout=0.75)
        try:
            solver_threads = int(solver.get("thread") or 0) or None
        except (TypeError, ValueError):
            solver_threads = None
    profile = ctx.machine_profile(
        provider=selected,
        solver_threads=solver_threads,
        local_slots=ctx._LOCAL_CAPTCHA_CONCURRENCY if selected == "local" else None,
        global_limit=ctx._GLOBAL_REG_INFLIGHT_MAX,
    )
    profile["solver"] = solver
    return profile


def pause_registration_batch(ctx: RegistrationContext, batch_id: str) -> dict[str, Any]:
    """Interrupt active work immediately while preserving the batch for resume."""
    bid = str(batch_id or "").strip()
    if not bid:
        return {"ok": False, "error": "missing batch id"}
    batch = ctx._load_reg_batch(bid)
    if not batch:
        return {"ok": False, "error": "registration batch not found"}
    status = str(batch.get("status") or "").lower()
    if status in {"done", "partial", "error"}:
        return {
            "ok": False,
            "error": f"batch already terminal ({status})",
            "batch_id": bid,
        }
    if batch.get("cancel_requested") or status in {"stopping", "cancelled", "stopped"}:
        return {
            "ok": False,
            "error": "batch is stopping or cancelled; it cannot be paused",
            "batch_id": bid,
        }
    clients: list[Any] = []
    interrupted: list[str] = []
    with ctx._lock:
        current = ctx._batches.get(bid) or dict(batch)
        current["pause_requested"] = True
        current["cancel_requested"] = False
        current["status"] = "pausing" if current.get("runner_alive") else "paused"
        current["message"] = "已请求暂停，正在中断进行中的注册任务"
        current["updated_at"] = ctx._now()
        ctx._batches[bid] = current
        ctx._mirror_reg_batch(bid, dict(current))
        for sid in list(current.get("session_ids") or []):
            session = ctx._sessions.get(str(sid)) or ctx._load_reg_sess(str(sid))
            if not session:
                continue
            session = dict(session)
            session_status = str(session.get("status") or "").lower()
            if session_status in ctx._TERMINAL_STATUSES:
                continue
            session["pause_requested"] = True
            session["cancel_requested"] = False
            session["status"] = "pausing"
            session["message"] = "批次暂停中，正在中断当前注册流程"
            session["updated_at"] = ctx._now()
            ctx._append_session_event(
                session, session["status"], session["message"], at=session["updated_at"]
            )
            ctx._sessions[str(sid)] = session
            ctx._mirror_reg_sess(str(sid), session)
            interrupted.append(str(sid))
            client = session.get("_client")
            if client is not None:
                clients.append(client)
        out = dict(current)
    ctx._interrupt_pipeline_for_batch(bid)
    for client in clients:
        try:
            client.close()
        except Exception:
            pass
    return {
        "ok": True,
        "batch_id": bid,
        "status": out["status"],
        "message": out["message"],
        "interrupted": len(interrupted),
        "batch": out,
    }
