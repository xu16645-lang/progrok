"""Grok registration creation, batch scheduling, and worker workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class RegistrationContext:
    """Resolve the adapter services needed by long-running registration workflows."""

    def __init__(self, values: Mapping[str, Any]):
        self.values = values

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _prepare_registration_session(
    ctx: RegistrationContext,
    *,
    yescaptcha_key: str,
    proxy: str,
    moemail_api_key: str | None = None,
    moemail_base_url: str | None = None,
    prefix: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
    batch_total: int | None = None,
    start_delay: float = 0.0,
    post_registration: dict[str, Any] | None = None,
    headless: bool = True,
    should_cancel: Any | None = None,
) -> dict[str, Any]:
    """Create mailbox + session record. Does NOT start the registration worker."""
    if start_delay > 0:
        deadline = ctx.time.monotonic() + start_delay
        while True:
            if callable(should_cancel):
                should_cancel()
            remaining = deadline - ctx.time.monotonic()
            if remaining <= 0:
                break
            ctx.time.sleep(min(0.2, remaining))
    elif callable(should_cancel):
        should_cancel()

    try:
        email, receiver = ctx._make_email_receiver(
            api_key=moemail_api_key,
            base_url=moemail_base_url,
            prefix=prefix,
            domain=domain,
            expiry_ms=expiry_ms,
            mail_provider=mail_provider,
            hotmail_local_base_url=hotmail_local_base_url,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

        # xAI password rules: mix upper/lower/digit/symbol.
    password = f"Aa{ctx.os.urandom(5).hex()}9!xZ"
    sid = f"gba_{ctx.uuid.uuid4().hex[:16]}"

    created_at = ctx._now()
    initial_message = f"queued; email={email}"
    sess = {
        "id": sid,
        "status": "queued",
        "created_at": created_at,
        "updated_at": created_at,
        "email": email,
        "password": password,
        "message": initial_message,
        "events": [{"at": created_at, "status": "queued", "message": initial_message}],
        "sso": None,
        "oauth": None,
        "auth_json": None,
        "error": None,
        "yescaptcha_key": yescaptcha_key,
        "proxy": proxy or None,
        "adapter_build": ctx.ADAPTER_BUILD,
        "batch_id": batch_id,
        "batch_index": batch_index,
        "batch_total": batch_total,
        "pre_import_probe_enabled": bool(
            (post_registration or {}).get("pre_import_probe_enabled", True)
        ),
        # Keep receiver process-local only (not mirrored to Redis).
        "_receiver": receiver,
        "_hotmail_account_id": getattr(receiver, "account_id", None),
        "_hotmail_alias_index": getattr(receiver, "alias_index", None),
        "_post_registration": dict(post_registration or {}),
        "_headless": bool(headless),
    }
    with ctx._lock:
        ctx._sessions[sid] = sess
        if batch_id and batch_id in ctx._batches:
            ctx._batches[batch_id]["session_ids"].append(sid)
            ctx._batches[batch_id]["updated_at"] = ctx._now()
            ctx._mirror_reg_batch(batch_id, dict(ctx._batches[batch_id]))
    ctx._mirror_reg_sess(sid, sess)
    return {"ok": True, **ctx._compact_session(sess)}


def _start_one_registration(
    ctx: RegistrationContext,
    *,
    yescaptcha_key: str,
    proxy: str,
    moemail_api_key: str | None = None,
    moemail_base_url: str | None = None,
    prefix: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
    batch_total: int | None = None,
    start_delay: float = 0.0,
    post_registration: dict[str, Any] | None = None,
    headless: bool = True,
) -> dict[str, Any]:
    """Create one session and spawn its worker thread (single-job path)."""
    prepared = ctx._prepare_registration_session(
        yescaptcha_key=yescaptcha_key,
        proxy=proxy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        prefix=prefix,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        batch_id=batch_id,
        batch_index=batch_index,
        batch_total=batch_total,
        start_delay=start_delay,
        post_registration=post_registration,
        headless=headless,
    )
    if not prepared.get("ok"):
        return prepared
    sid = str(prepared.get("id") or "")
    with ctx._lock:
        sess = ctx._sessions.get(sid) or {}
        receiver = sess.get("_receiver")
    if not sid or receiver is None:
        return {"ok": False, "error": "registration session prepare failed"}
    with ctx._lock:
        if sid in ctx._sessions:
            ctx._sessions[sid]["status"] = "started"
            ctx._sessions[sid]["message"] = (
                f"started; email={ctx._sessions[sid].get('email') or ''}"
            )
            ctx._sessions[sid]["updated_at"] = ctx._now()
            ctx._mirror_reg_sess(sid, ctx._sessions[sid])
            # Single-job starts are not covered by the batch finalizer — log "running"
            # immediately so 任务日志 shows the registration task right away.
    if not batch_id:
        with ctx._lock:
            started_sess = dict(ctx._sessions.get(sid) or {})
        if started_sess:
            payload = ctx._session_task_log_payload(started_sess)
            ctx._record_register_task(
                task_id=payload["task_id"],
                summary=payload["summary"]
                or f"浏览器注册启动 {started_sess.get('email') or sid}",
                status="running",
                ok=None,
                progress_done=0,
                progress_total=1,
                finished=False,
                detail={**payload["detail"], "phase": "started"},
            )
    ctx.threading.Thread(
        target=ctx._run_registration,
        args=(sid, yescaptcha_key, proxy or "", receiver),
        daemon=True,
        name=f"gba-reg-{sid[-8:]}",
    ).start()
    with ctx._lock:
        sess = ctx._sessions.get(sid)
        if sess is None:
            return prepared
        return {"ok": True, **ctx._compact_session(sess)}


def start_registration(
    ctx: RegistrationContext,
    *,
    registration_mode: str | None = None,
    captcha_provider: str | None = None,
    local_solver_url: str | None = None,
    yescaptcha_key: str | None = None,
    proxy: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    proxy_strategy: str | None = None,
    moemail_api_key: str | None = None,
    moemail_base_url: str | None = None,
    prefix: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    count: int | None = None,
    concurrency: int | None = None,
    stagger_ms: int | None = None,
    probe_delay_sec: float | int | None = None,
    post_registration: dict[str, Any] | None = None,
    start_paused: bool = False,
    auto_tune_enabled: bool = False,
    headless: bool = True,
) -> dict[str, Any]:
    """Start one or many registration sessions (multi-thread).

    ``count`` > 1 enables batch mode. ``concurrency`` is the real in-flight
    limit: e.g. concurrency=3 means only 3 accounts register at the same time;
    when one finishes, the next queued account starts.

    ``proxy`` may be a single URL or a multi-line proxy pool. Each registration
    job picks one entry via ``proxy_strategy`` (round_robin / random / sticky).
    """
    try:
        from xai_browser import _ensure_camoufox

        _ensure_camoufox()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

    ctx._clean_old_sessions()

    # Allow admin form / env to override settle window for post-import probe.
    if probe_delay_sec is not None:
        try:
            ctx.values["REGISTER_PROBE_DELAY_SEC"] = max(
                0.0, min(600.0, float(probe_delay_sec))
            )
            ctx.os.environ["GROK2API_REG_PROBE_DELAY_SEC"] = str(
                int(ctx.values["REGISTER_PROBE_DELAY_SEC"])
            )
        except (TypeError, ValueError):
            pass

    mode = str(registration_mode or "browser").strip().lower()
    if mode not in {"protocol", "browser"}:
        mode = "browser"
    post_registration = dict(post_registration or {})
    post_registration["registration_mode"] = mode

    if mode == "browser":
        provider = "browser"
        key = ""
    else:
        provider = (
            (
                captcha_provider
                or ctx.CAPTCHA_PROVIDER
                or ctx.os.environ.get("GROK2API_CAPTCHA_PROVIDER")
                or ctx.os.environ.get("CAPTCHA_PROVIDER")
                or "local"
            )
            .strip()
            .lower()
        )
        if provider not in {"local", "yescaptcha"}:
            provider = "local"
        try:
            ctx.values["CAPTCHA_PROVIDER"] = provider
        except Exception:
            pass

        if provider == "local":
            solver_url = ctx._local_solver_base_url(local_solver_url)
            try:
                ctx.values["LOCAL_SOLVER_URL"] = solver_url
            except Exception:
                pass
            ctx.os.environ["GROK2API_LOCAL_SOLVER_URL"] = solver_url
            ctx.os.environ["LOCAL_SOLVER_URL"] = solver_url
            ctx.os.environ["GROK2API_YESCAPTCHA_ENDPOINT"] = solver_url
            ctx.os.environ["YESCAPTCHA_ENDPOINT"] = solver_url
            key = "local"
            solver_wait = ctx.wait_for_local_solver(solver_url)
            if not solver_wait.get("ready"):
                return {
                    "ok": False,
                    "error": solver_wait.get("error") or f"本地过盾未就绪: {solver_url}",
                    "local_solver": solver_wait,
                }
        else:
            try:
                ctx.values["LOCAL_SOLVER_URL"] = ""
            except Exception:
                pass
            for env_key in (
                "GROK2API_LOCAL_SOLVER_URL",
                "LOCAL_SOLVER_URL",
                "GROK2API_YESCAPTCHA_ENDPOINT",
                "YESCAPTCHA_ENDPOINT",
                "YESCAPTCHA_API_BASE",
            ):
                ctx.os.environ.pop(env_key, None)
            key = (
                yescaptcha_key
                or ctx.YESCAPTCHA_KEY
                or ctx.os.environ.get("GROK2API_YESCAPTCHA_KEY")
                or ctx.os.environ.get("YESCAPTCHA_API_KEY")
                or ""
            ).strip()
            if key == "local":
                key = ""
            if not key:
                return {"ok": False, "error": "YesCaptcha 模式需要有效的 API Key"}

        if key and key != ctx.YESCAPTCHA_KEY:
            try:
                ctx.values["YESCAPTCHA_KEY"] = key
            except Exception:
                pass

    try:
        n = int(count if count is not None else 1)
    except (TypeError, ValueError):
        n = 1
    n = max(1, n)

    try:
        workers = int(
            concurrency if concurrency is not None else ctx.DEFAULT_CONCURRENCY
        )
    except (TypeError, ValueError):
        workers = ctx.DEFAULT_CONCURRENCY
    requested_workers = max(1, min(workers, ctx.MAX_CONCURRENCY, n))
    performance_profile = ctx.get_registration_performance_profile(
        provider, local_solver_url
    )
    workers = requested_workers

    try:
        stagger = int(stagger_ms if stagger_ms is not None else 400)
    except (TypeError, ValueError):
        stagger = 400
    stagger = max(0, min(stagger, 60_000))

    # Build proxy pool once; each job picks one URL (rotation / random / sticky).
    proxy_pool = ctx._proxy_pool(
        proxy,
        username=proxy_username,
        password=proxy_password,
    )
    try:
        from config import XAI_PROXY_STRATEGY as _default_strat
    except Exception:
        _default_strat = "round_robin"
    proxy_strat = (proxy_strategy or _default_strat or "round_robin").strip().lower()
    proxy_val = ctx._pick_proxy_from_pool(proxy_pool, strategy=proxy_strat, index=0)
    requested_mail_provider = (mail_provider or "moemail").strip().lower() or "moemail"
    if requested_mail_provider == "hotmail_local":
        from hotmail_local import list_accounts

        available = int(list_accounts().get("available") or 0)
        if available < n:
            return {
                "ok": False,
                "error": f"微软邮箱账户池可用账号不足：需要 {n} 个，当前可用 {available} 个",
                "available": available,
                "required": n,
            }
        mail_prov = "hotmail_local"
    else:
        try:
            from moemail import normalize_mail_provider as _norm_mail

            mail_prov = _norm_mail(mail_provider, base_url=moemail_base_url)
        except Exception:
            mail_prov = requested_mail_provider

            # Single job — keep original response shape for UI compatibility.
    if n == 1:
        return ctx._start_one_registration(
            yescaptcha_key=key,
            proxy=proxy_val,
            moemail_api_key=moemail_api_key,
            moemail_base_url=moemail_base_url,
            prefix=prefix,
            domain=domain,
            expiry_ms=expiry_ms,
            mail_provider=mail_prov,
            hotmail_local_base_url=hotmail_local_base_url,
            post_registration=post_registration,
            headless=headless,
        )

    batch_id = f"batch_{ctx.uuid.uuid4().hex[:12]}"
    # Snapshot keeps the full multi-line text so resume / UI can re-parse the pool.
    proxy_snapshot = (proxy or "\n".join(proxy_pool) or proxy_val or "").strip()
    reg_cfg = ctx._snapshot_reg_config(
        captcha_provider=provider,
        yescaptcha_key=key,
        proxy=proxy_snapshot,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        prefix=prefix,
        domain=domain,
        expiry_ms=expiry_ms,
        concurrency=workers,
        stagger_ms=stagger,
        mail_provider=mail_prov,
        hotmail_local_base_url=hotmail_local_base_url,
        post_registration=post_registration,
        auto_tune_enabled=auto_tune_enabled,
        headless=headless,
    )
    reg_cfg["proxy_strategy"] = proxy_strat
    reg_cfg["proxy_pool_count"] = len(proxy_pool)
    batch = {
        "id": batch_id,
        "status": "running",
        "created_at": ctx._now(),
        "updated_at": ctx._now(),
        "count": n,
        "concurrency": workers,
        "configured_concurrency": requested_workers,
        "stagger_ms": stagger,
        "scheduling_mode": "slot_fill",
        "auto_tune_enabled": bool(auto_tune_enabled),
        "performance_profile": performance_profile,
        "session_ids": [],
        "adapter_build": ctx.ADAPTER_BUILD,
        "message": f"batch started count={n} concurrency={workers}",
        "error": None,
        "finished": 0,
        "ok_count": 0,
        "fail_count": 0,
        "spawned": 0,
        "reg_config": reg_cfg,
        "owner_pid": ctx.os.getpid(),
        "runner_alive": True,
        "cancel_requested": False,
        "pause_requested": False,
    }
    with ctx._lock:
        ctx._batches[batch_id] = batch
    ctx._mirror_reg_batch(batch_id, batch)
    # Log batch start so 任务日志 has a running row even before the first
    # session finishes (previously only the terminal row was written).
    ctx._record_register_task(
        task_id=batch_id,
        summary=f"浏览器注册批次启动 count={n} concurrency={workers}",
        status="running",
        ok=None,
        progress_done=0,
        progress_total=n,
        finished=False,
        detail={
            "batch_id": batch_id,
            "count": n,
            "concurrency": workers,
            "stagger_ms": stagger,
            "phase": "started",
            "adapter_build": ctx.ADAPTER_BUILD,
        },
    )

    if start_paused:
        with ctx._lock:
            paused_batch = ctx._batches.get(batch_id) or batch
            paused_batch["status"] = "paused"
            paused_batch["pause_requested"] = True
            paused_batch["runner_alive"] = False
            paused_batch["message"] = f"paused 0/{n} (waiting for manual resume)"
            paused_batch["updated_at"] = ctx._now()
            ctx._batches[batch_id] = paused_batch
            ctx._mirror_reg_batch(batch_id, dict(paused_batch))
        return {
            "ok": True,
            "batch": True,
            "batch_id": batch_id,
            "count": n,
            "concurrency": workers,
            "stagger_ms": stagger,
            "session_ids": [],
            "sessions": [],
            "status": "paused",
            "adapter_build": ctx.ADAPTER_BUILD,
            "message": f"paused batch created: count={n}, threads={workers}",
        }

    started = ctx._spawn_batch_runner(
        batch_id,
        remaining=n,
        concurrency=workers,
        stagger_ms=stagger,
        captcha_provider=provider,
        yescaptcha_key=key,
        proxy=proxy_snapshot,
        proxy_strategy=proxy_strat,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        prefix=prefix,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_prov,
        hotmail_local_base_url=hotmail_local_base_url,
        post_registration=post_registration,
        auto_tune_enabled=auto_tune_enabled,
        headless=headless,
    )
    if not started.get("ok"):
        return started

        # Brief wait so the first wave (up to `workers`) is usually visible to UI.
    ctx.time.sleep(min(0.45, 0.08 * workers + 0.08))
    with ctx._lock:
        b = dict(ctx._batches.get(batch_id) or batch)
        sids = list(b.get("session_ids") or [])
        sessions = [
            ctx._compact_session(ctx._sessions[s]) for s in sids if s in ctx._sessions
        ]

    return {
        "ok": True,
        "batch": True,
        "batch_id": batch_id,
        "count": n,
        "concurrency": workers,
        "configured_concurrency": requested_workers,
        "stagger_ms": stagger,
        "session_ids": sids,
        "sessions": sessions,
        "adapter_build": ctx.ADAPTER_BUILD,
        "message": (
            f"batch started: count={n}, threads={workers} "
            f"(in-flight cap), queued/started={len(sids)}"
        ),
        # Back-compat: first session fields for old UI single-session path.
        **(sessions[0] if sessions else {"id": None, "status": "starting"}),
    }
