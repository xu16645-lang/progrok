"""ChatGPT registration flow operations."""

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


def _prepare_registration_session(
    ctx,
    *,
    proxy,
    moemail_api_key=None,
    moemail_base_url=None,
    domain=None,
    expiry_ms=None,
    mail_provider=None,
    hotmail_local_base_url=None,
    batch_id=None,
    batch_index=None,
    batch_total=None,
    start_delay=0.0,
    post_registration=None,
    headless=True,
):
    """Create mailbox + session record. Does NOT start the registration worker."""
    if start_delay > 0:
        ctx.time.sleep(start_delay)
    try:
        email, receiver = ctx._make_email_receiver(
            api_key=moemail_api_key,
            base_url=moemail_base_url,
            domain=domain,
            expiry_ms=expiry_ms,
            mail_provider=mail_provider,
            hotmail_local_base_url=hotmail_local_base_url,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    password = f"Aa{ctx.os.urandom(5).hex()}9!cG"
    sid = f"cgpt_{ctx.uuid.uuid4().hex[:16]}"
    created_at = ctx._now()
    msg = f"queued; email={email}"
    sess = {
        "id": sid,
        "status": "queued",
        "created_at": created_at,
        "updated_at": created_at,
        "email": email,
        "password": password,
        "message": msg,
        "events": [{"at": created_at, "status": "queued", "message": msg}],
        "sso": None,
        "auth_json": None,
        "error": None,
        "proxy": proxy or None,
        "adapter_build": ctx.CHATGPT_ADAPTER_BUILD,
        "batch_id": batch_id,
        "batch_index": batch_index,
        "batch_total": batch_total,
        "_receiver": receiver,
        "_hotmail_account_id": getattr(receiver, "account_id", None),
        "_hotmail_alias_index": getattr(receiver, "alias_index", None),
        "_post_registration": dict(post_registration or {}),
        "_headless": headless,
    }
    with ctx._lock:
        ctx._sessions[sid] = sess
        if batch_id and batch_id in ctx._batches:
            ctx._batches[batch_id]["session_ids"].append(sid)
            ctx._batches[batch_id]["updated_at"] = ctx._now()
    return {"ok": True, **ctx._compact_session(sess)}


def _start_one_registration(
    ctx,
    *,
    proxy,
    moemail_api_key=None,
    moemail_base_url=None,
    domain=None,
    expiry_ms=None,
    mail_provider=None,
    hotmail_local_base_url=None,
    batch_id=None,
    batch_index=None,
    batch_total=None,
    start_delay=0.0,
    post_registration=None,
    headless=True,
):
    """Create one session and spawn its worker thread (single-job path)."""
    prepared = ctx._prepare_registration_session(
        proxy=proxy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
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
    ctx.threading.Thread(
        target=ctx._run_registration,
        args=(sid, proxy, receiver),
        daemon=True,
        name=f"cgpt-reg-{sid[-8:]}",
    ).start()
    with ctx._lock:
        sess = ctx._sessions.get(sid)
        if sess is None:
            return prepared
        return {"ok": True, **ctx._compact_session(sess)}


def _snapshot_reg_config(
    ctx,
    *,
    proxy,
    moemail_api_key,
    moemail_base_url,
    domain,
    expiry_ms,
    mail_provider,
    hotmail_local_base_url,
    concurrency,
    stagger_ms,
    post_registration=None,
    headless=True,
):
    return {
        "proxy": proxy or "",
        "moemail_api_key": moemail_api_key or "",
        "moemail_base_url": moemail_base_url or "",
        "domain": domain or "",
        "expiry_ms": expiry_ms,
        "mail_provider": (mail_provider or "moemail").strip().lower(),
        "hotmail_local_base_url": hotmail_local_base_url or "http://127.0.0.1:17373",
        "concurrency": concurrency,
        "stagger_ms": stagger_ms,
        "post_registration": dict(post_registration or {}),
        "headless": headless,
    }


def start_registration(
    ctx,
    *,
    proxy=None,
    proxy_username=None,
    proxy_password=None,
    proxy_strategy=None,
    moemail_api_key=None,
    moemail_base_url=None,
    domain=None,
    expiry_ms=None,
    mail_provider=None,
    hotmail_local_base_url=None,
    count=None,
    concurrency=None,
    stagger_ms=None,
    post_registration=None,
    start_paused=False,
    headless=True,
    **kwargs,
):
    """Start one or many ChatGPT registration sessions (multi-thread)."""
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
    workers = max(1, min(workers, ctx.MAX_CONCURRENCY, n))
    try:
        stagger = int(stagger_ms if stagger_ms is not None else 2000)
    except (TypeError, ValueError):
        stagger = 2000
    stagger = max(0, min(stagger, 60000))
    requested_provider = (mail_provider or "moemail").strip().lower() or "moemail"
    if requested_provider == "hotmail_local":
        from hotmail_local import list_accounts

        available = int(list_accounts().get("available") or 0)
        if available < n:
            return {
                "ok": False,
                "error": f"微软邮箱账户池可用账号不足：需要 {n} 个，当前可用 {available} 个",
                "available": available,
                "required": n,
            }
    proxy_pool = ctx._proxy_pool(
        proxy, username=proxy_username, password=proxy_password
    )
    proxy_strat = (proxy_strategy or "round_robin").strip().lower()
    proxy_val = ctx._pick_proxy_from_pool(proxy_pool, strategy=proxy_strat, index=0)
    if requested_provider == "hotmail_local":
        mail_prov = requested_provider
    else:
        try:
            from moemail import normalize_mail_provider

            mail_prov = normalize_mail_provider(
                mail_provider, base_url=moemail_base_url
            )
        except Exception:
            mail_prov = requested_provider
    if n == 1:
        return ctx._start_one_registration(
            proxy=proxy_val,
            moemail_api_key=moemail_api_key,
            moemail_base_url=moemail_base_url,
            domain=domain,
            expiry_ms=expiry_ms,
            mail_provider=mail_prov,
            hotmail_local_base_url=hotmail_local_base_url,
            post_registration=post_registration,
            headless=headless,
        )
    batch_id = f"batch_cgpt_{ctx.uuid.uuid4().hex[:12]}"
    proxy_snapshot = (proxy or "\n".join(proxy_pool) or proxy_val or "").strip()
    batch = {
        "id": batch_id,
        "status": "running",
        "created_at": ctx._now(),
        "updated_at": ctx._now(),
        "count": n,
        "concurrency": workers,
        "stagger_ms": stagger,
        "session_ids": [],
        "adapter_build": ctx.CHATGPT_ADAPTER_BUILD,
        "message": f"batch started count={n} concurrency={workers}",
        "error": None,
        "finished": 0,
        "ok_count": 0,
        "fail_count": 0,
        "spawned": 0,
        "cancel_requested": False,
        "pause_requested": False,
        "reg_config": ctx._snapshot_reg_config(
            proxy=proxy_snapshot,
            moemail_api_key=moemail_api_key,
            moemail_base_url=moemail_base_url,
            domain=domain,
            expiry_ms=expiry_ms,
            mail_provider=mail_prov,
            hotmail_local_base_url=hotmail_local_base_url,
            concurrency=workers,
            stagger_ms=stagger,
            post_registration=post_registration,
            headless=headless,
        ),
    }
    with ctx._lock:
        ctx._batches[batch_id] = batch
    if start_paused:
        with ctx._lock:
            b = ctx._batches.get(batch_id) or batch
            b["status"] = "paused"
            b["pause_requested"] = True
            b["message"] = f"paused 0/{n}"
            b["updated_at"] = ctx._now()
        return {
            "ok": True,
            "batch": True,
            "batch_id": batch_id,
            "count": n,
            "concurrency": workers,
            "status": "paused",
        }
    ctx.threading.Thread(
        target=ctx._batch_spawner,
        args=(
            batch_id,
            n,
            workers,
            stagger,
            proxy_pool,
            proxy_strat,
            moemail_api_key,
            moemail_base_url,
            domain,
            expiry_ms,
            mail_prov,
            hotmail_local_base_url,
            post_registration,
            headless,
        ),
        daemon=True,
        name=f"cgpt-batch-{batch_id[-8:]}",
    ).start()
    ctx.time.sleep(0.5)
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
        "stagger_ms": stagger,
        "session_ids": sids,
        "sessions": sessions,
        "adapter_build": ctx.CHATGPT_ADAPTER_BUILD,
        **(sessions[0] if sessions else {"id": None, "status": "starting"}),
    }


def _batch_spawner(
    ctx,
    batch_id,
    total,
    workers,
    stagger_ms,
    proxy_pool,
    proxy_strat,
    moemail_api_key,
    moemail_base_url,
    domain,
    expiry_ms,
    mail_provider,
    hotmail_local_base_url,
    post_registration,
    headless,
):
    """Spawn registration jobs for a batch, respecting concurrency limits."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ctx._lock:
        b = ctx._batches.get(batch_id) or {}
    b["status"] = "running"
    b["runner_alive"] = True
    ok_n, fail_n, finished = (0, 0, 0)
    worker_state = ctx.threading.local()

    def _batch_cancel() -> bool:
        with ctx._lock:
            bb = ctx._batches.get(batch_id) or {}
        return bool(bb.get("cancel_requested")) or ctx._is_cancel_status(
            bb.get("status")
        )

    def _batch_pause() -> bool:
        with ctx._lock:
            bb = ctx._batches.get(batch_id) or {}
        return (
            bool(bb.get("pause_requested"))
            or str(bb.get("status") or "").lower() == "paused"
        )

    def _job(i: int) -> dict[str, Any]:
        if _batch_cancel():
            return {"ok": False, "status": "cancelled", "error": "batch cancelled"}
        job_proxy = ctx._pick_proxy_from_pool(
            proxy_pool, strategy=proxy_strat, index=i - 1
        )
        delay = stagger_ms / 1000.0 * ((i - 1) % max(1, workers))
        prepared = ctx._prepare_registration_session(
            proxy=job_proxy,
            moemail_api_key=moemail_api_key,
            moemail_base_url=moemail_base_url,
            domain=domain,
            expiry_ms=expiry_ms,
            mail_provider=mail_provider,
            hotmail_local_base_url=hotmail_local_base_url,
            batch_id=batch_id,
            batch_index=i,
            batch_total=total,
            start_delay=delay,
            post_registration=post_registration,
            headless=headless,
        )
        if not prepared.get("ok"):
            return prepared
        sid = str(prepared.get("id") or "")
        with ctx._lock:
            sess = ctx._sessions.get(sid) or {}
            if _batch_cancel():
                if sid in ctx._sessions:
                    ctx._sessions[sid]["status"] = "cancelled"
                    ctx._sessions[sid]["message"] = "batch cancelled"
                return {"ok": False, "id": sid, "status": "cancelled"}
            receiver = sess.get("_receiver")
        if not sid or receiver is None:
            return {"ok": False, "error": "prepare failed"}
        try:
            runtime = getattr(worker_state, "browser_runtime", None)
            if runtime is None:
                from chatgpt_browser import ChatGPTBrowserRuntime

                runtime = ChatGPTBrowserRuntime()
                worker_state.browser_runtime = runtime
            ctx._run_registration(sid, job_proxy or "", receiver, runtime)
        except Exception:
            pass
        finally:
            with ctx._lock:
                if sid in ctx._sessions:
                    ctx._sessions[sid].pop("_receiver", None)
        with ctx._lock:
            final = ctx._sessions.get(sid) or {}
        st = str(final.get("status") or "")
        ok = bool(final.get("imported_account_ids")) and st not in (
            "error",
            "failed",
            "cancelled",
        )
        return {
            "ok": ok,
            "id": sid,
            "status": st,
            "error": final.get("error"),
            "email": final.get("email"),
        }

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="cgpt-batch-job"
    ) as pool:
        futures = {}
        next_i = 1
        for _ in range(min(workers, total)):
            if next_i > total:
                break
            futures[pool.submit(_job, next_i)] = next_i
            next_i += 1
        while futures:
            if _batch_pause():
                for f in list(futures):
                    f.cancel()
                break
            done_futures = set()
            for f in list(futures):
                if f.done():
                    done_futures.add(f)
            if not done_futures:
                ctx.time.sleep(0.3)
                continue
            for f in done_futures:
                idx = futures.pop(f)
                finished += 1
                try:
                    r = f.result()
                except Exception as e:
                    r = {"ok": False, "error": str(e)}
                if r.get("ok"):
                    ok_n += 1
                else:
                    fail_n += 1
                if next_i <= total and (not _batch_cancel()) and (not _batch_pause()):
                    futures[pool.submit(_job, next_i)] = next_i
                    next_i += 1
            with ctx._lock:
                bb = ctx._batches.get(batch_id) or b
                bb["finished"] = finished
                bb["ok_count"] = ok_n
                bb["fail_count"] = fail_n
                bb["updated_at"] = ctx._now()
                bb["message"] = f"progress {finished}/{total} ok={ok_n} fail={fail_n}"
                ctx._batches[batch_id] = bb
        cleanup_barrier = ctx.threading.Barrier(workers)

        def _cleanup_worker_browser() -> None:
            runtime = getattr(worker_state, "browser_runtime", None)
            if runtime is not None:
                runtime.close()
                worker_state.browser_runtime = None
            try:
                cleanup_barrier.wait(timeout=15)
            except ctx.threading.BrokenBarrierError:
                pass

        cleanup_futures = [pool.submit(_cleanup_worker_browser) for _ in range(workers)]
        for cleanup_future in cleanup_futures:
            try:
                cleanup_future.result(timeout=20)
            except Exception:
                pass
    with ctx._lock:
        bb = ctx._batches.get(batch_id) or b
        bb["runner_alive"] = False
        if bb.get("cancel_requested"):
            bb["status"] = "cancelled"
        elif bb.get("pause_requested"):
            bb["status"] = "paused"
        elif finished >= total:
            bb["status"] = "done"
        else:
            bb["status"] = "partial"
        bb["finished"] = finished
        bb["ok_count"] = ok_n
        bb["fail_count"] = fail_n
        bb["updated_at"] = ctx._now()
        bb["message"] = f"batch complete: {ok_n} ok, {fail_n} fail / {total}"
        ctx._batches[batch_id] = bb
