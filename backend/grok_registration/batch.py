"""Batch scheduling for Grok registrations."""

from __future__ import annotations

from typing import Any

from .batch_lease import maintain_batch_runner_lease
from .flow import RegistrationContext


def _spawn_batch_runner(
    ctx: RegistrationContext,
    batch_id: str,
    *,
    remaining: int,
    concurrency: int,
    stagger_ms: int,
    captcha_provider: str,
    yescaptcha_key: str,
    proxy: str,
    proxy_strategy: str | None = None,
    moemail_api_key: str | None,
    moemail_base_url: str | None,
    prefix: str | None,
    domain: str | None,
    expiry_ms: int | None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
    post_registration: dict[str, Any] | None = None,
    auto_tune_enabled: bool = False,
    headless: bool = True,
) -> dict[str, Any]:
    """Start the ThreadPool spawner for a batch (also used by resume/reclaim)."""
    bid = str(batch_id or "").strip()
    if not bid:
        return {"ok": False, "error": "missing batch id"}
    batch = ctx._load_reg_batch(bid)
    if not batch:
        return {"ok": False, "error": "registration batch not found"}
    with ctx._lock:
        current_batch = ctx._batches.get(bid) or batch
        current_batch["probe_status"] = "running"
        current_batch["probe_pause_requested"] = False
        current_batch["updated_at"] = ctx._now()
        ctx._batches[bid] = current_batch
        ctx._mirror_reg_batch(bid, dict(current_batch))

    if remaining <= 0:
        with ctx._lock:
            b = ctx._batches.get(bid) or dict(batch)
            b["runner_alive"] = False
            b["status"] = "done"
            b["updated_at"] = ctx._now()
            b["message"] = "nothing to spawn"
            ctx._batches[bid] = b
            ctx._mirror_reg_batch(bid, dict(b))
        return {
            "ok": True,
            "batch_id": bid,
            "already_complete": True,
            "remaining": 0,
            "batch": ctx.get_registration_batch(bid),
        }

    acquired, lock_token = ctx._try_acquire_batch_runner(bid)
    if not acquired:
        return {
            "ok": False,
            "error": "batch runner already active on another worker",
            "batch_id": bid,
            "already_running": True,
        }

    # Persisted batches may still say local/yescaptcha. New and resumed xAI
    # registrations always let the real browser handle the upstream challenge.
    provider = "browser"
    key = ""

            # `proxy` may be multi-line pool text; expand once for this runner.
    proxy_pool = ctx._proxy_pool(proxy)
    try:
        from config import XAI_PROXY_STRATEGY as _default_strat
    except Exception:
        _default_strat = "round_robin"
    proxy_strat = (proxy_strategy or _default_strat or "round_robin").strip().lower()
    proxy_snapshot = (proxy or "\n".join(proxy_pool) or "").strip()
    workers = max(
        1,
        min(
            int(concurrency or ctx.DEFAULT_CONCURRENCY), ctx.MAX_CONCURRENCY, remaining
        ),
    )
    stagger = max(0, min(int(400 if stagger_ms is None else stagger_ms), 60_000))
    tuner = ctx.AdaptiveRegistrationTuner(bool(auto_tune_enabled), workers, stagger)
    pipeline_config = dict(post_registration or {})
    pipeline_config["pipeline_concurrency"] = workers

    with ctx._lock:
        b = ctx._batches.get(bid) or dict(batch)
        b["status"] = "running"
        b["cancel_requested"] = False
        b["pause_requested"] = False
        b["concurrency"] = workers
        b["stagger_ms"] = stagger
        b["scheduling_mode"] = "slot_fill"
        b["auto_tune_enabled"] = bool(auto_tune_enabled)
        b["auto_tune"] = tuner.snapshot(
            successes=int(b.get("ok_count") or 0),
            started_at=float(b.get("created_at") or ctx._now()),
        )
        b["runner_alive"] = True
        b["owner_pid"] = ctx.os.getpid()
        b["adapter_build"] = ctx.ADAPTER_BUILD
        b["reg_config"] = ctx._snapshot_reg_config(
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
            mail_provider=mail_provider,
            hotmail_local_base_url=hotmail_local_base_url,
            post_registration=pipeline_config,
            auto_tune_enabled=auto_tune_enabled,
            headless=headless,
        )
        b["reg_config"]["proxy_strategy"] = proxy_strat
        b["reg_config"]["proxy_pool_count"] = len(proxy_pool)
        b["updated_at"] = ctx._now()
        # Preserve historical counters when reclaiming after process restart;
        # only reset if this is a brand-new spawn with no prior progress.
        prior_finished = int(b.get("finished") or 0)
        prior_ok = int(b.get("ok_count") or 0)
        prior_fail = int(b.get("fail_count") or 0)
        b["message"] = (
            f"starting remaining={remaining} threads={workers}"
            + (f" already_done={prior_finished}" if prior_finished else "")
            + (f" proxies={len(proxy_pool)}" if proxy_pool else "")
        )
        if prior_finished <= 0 and not (b.get("session_ids") or []):
            b["finished"] = 0
            b["ok_count"] = 0
            b["fail_count"] = 0
        else:
            b["finished"] = prior_finished
            b["ok_count"] = prior_ok
            b["fail_count"] = prior_fail
        ctx._batches[bid] = b
        ctx._mirror_reg_batch(bid, dict(b))

    def _run_batch() -> None:
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        errors: list[str] = []
        with ctx._lock:
            _seed = dict(ctx._batches.get(bid) or batch or {})
        finished = int(_seed.get("finished") or 0)
        ok_n = int(_seed.get("ok_count") or 0)
        fail_n = int(_seed.get("fail_count") or 0)
        stop_renew = False
        # Strict slot-fill scheduling: keep exactly the configured number of
        # jobs in flight and submit the next one as soon as any slot completes.
        next_i = 1
        in_flight: dict[Any, int] = {}
        worker_state = ctx.threading.local()

        def _max_inflight() -> int:
            if tuner.enabled:
                return max(1, int(tuner.current_concurrency))
            return max(1, workers)

        def _batch_cancel_requested() -> bool:
            with ctx._lock:
                local = ctx._batches.get(bid) or {}
            if local.get("cancel_requested") or str(
                local.get("status") or ""
            ).lower() in (
                "stopping",
                "cancelled",
                "stopped",
            ):
                return True
            if not ctx._reg_redis():
                return False
            try:
                from store import sessions_redis

                remote = sessions_redis.reg_batch_get(bid)
                if not isinstance(remote, dict):
                    return False
                if remote.get("cancel_requested") or str(
                    remote.get("status") or ""
                ).lower() in (
                    "stopping",
                    "cancelled",
                    "stopped",
                ):
                    with ctx._lock:
                        cur = ctx._batches.get(bid) or dict(remote)
                        cur["cancel_requested"] = True
                        if str(cur.get("status") or "").lower() not in (
                            "cancelled",
                            "stopped",
                            "done",
                            "partial",
                            "error",
                        ):
                            cur["status"] = remote.get("status") or "stopping"
                            if remote.get("message"):
                                cur["message"] = remote.get("message")
                        cur["updated_at"] = ctx._now()
                        ctx._batches[bid] = cur
                    return True
            except Exception:
                pass
            return False

        def _batch_pause_requested() -> bool:
            with ctx._lock:
                local = ctx._batches.get(bid) or {}
            if local.get("pause_requested") or str(
                local.get("status") or ""
            ).lower() in (
                "pausing",
                "paused",
            ):
                return True
            if not ctx._reg_redis():
                return False
            try:
                from store import sessions_redis

                remote = sessions_redis.reg_batch_get(bid)
                if not isinstance(remote, dict):
                    return False
                if remote.get("pause_requested") or str(
                    remote.get("status") or ""
                ).lower() in (
                    "pausing",
                    "paused",
                ):
                    with ctx._lock:
                        cur = ctx._batches.get(bid) or dict(remote)
                        cur["pause_requested"] = True
                        cur["status"] = "pausing"
                        cur["updated_at"] = ctx._now()
                        ctx._batches[bid] = cur
                    return True
            except Exception:
                pass
            return False

        renew_t = ctx.threading.Thread(
            target=maintain_batch_runner_lease,
            args=(ctx, bid, lock_token),
            kwargs={
                "stop_requested": lambda: stop_renew,
                "cancel_requested": _batch_cancel_requested,
            },
            daemon=True,
            name=f"gba-batch-lock-{bid[-8:]}",
        )
        renew_t.start()

        def _job(i: int) -> dict[str, Any]:
            # Honour batch-level stop before creating more mailboxes.
            if _batch_cancel_requested():
                return {
                    "ok": False,
                    "id": None,
                    "status": "cancelled",
                    "error": "cancelled before start",
                }
            if _batch_pause_requested():
                return {
                    "ok": False,
                    "id": None,
                    "status": "paused",
                    "paused": True,
                    "error": "paused before start",
                }
                # One proxy per registration job (pool rotation / random / sticky).
            job_proxy = ctx._pick_proxy_from_pool(
                proxy_pool, strategy=proxy_strat, index=max(0, int(i) - 1)
            )
            # Small per-slot stagger only (not cumulative across the whole batch).
            active_slots = max(1, int(tuner.current_concurrency))
            delay = (tuner.stagger_ms / 1000.0) * ((i - 1) % active_slots)

            def _check_prepare_pause() -> None:
                if _batch_pause_requested():
                    raise ctx._RegPaused("paused before mailbox preparation")

            try:
                prepared = ctx._prepare_registration_session(
                    yescaptcha_key=key,
                    proxy=job_proxy,
                    moemail_api_key=moemail_api_key,
                    moemail_base_url=moemail_base_url,
                    prefix=prefix,
                    domain=domain,
                    expiry_ms=expiry_ms,
                    mail_provider=mail_provider,
                    hotmail_local_base_url=hotmail_local_base_url,
                    batch_id=bid,
                    batch_index=i,
                    batch_total=int(
                        (ctx._load_reg_batch(bid) or {}).get("count") or remaining
                    ),
                    start_delay=delay,
                    post_registration=pipeline_config,
                    headless=headless,
                    should_cancel=_check_prepare_pause,
                )
            except ctx._RegPaused as exc:
                return {
                    "ok": False,
                    "id": None,
                    "status": "paused",
                    "paused": True,
                    "error": str(exc),
                }
            if not prepared.get("ok"):
                return prepared
            sid = str(prepared.get("id") or "")
            with ctx._lock:
                # Re-check cancel after prepare (user may stop mid-queue).
                b1 = ctx._batches.get(bid) or {}
                sess = ctx._sessions.get(sid) or {}
                if (
                    b1.get("cancel_requested")
                    or str(b1.get("status") or "").lower()
                    in ("stopping", "cancelled", "stopped")
                    or sess.get("cancel_requested")
                ):
                    if sid in ctx._sessions:
                        ctx._sessions[sid]["status"] = "cancelled"
                        ctx._sessions[sid]["message"] = "cancelled before worker start"
                        ctx._sessions[sid]["error"] = "cancelled"
                        ctx._sessions[sid]["cancel_requested"] = True
                        ctx._sessions[sid]["updated_at"] = ctx._now()
                        ctx._sessions[sid].pop("_receiver", None)
                        ctx._mirror_reg_sess(sid, ctx._sessions[sid])
                    return {
                        "ok": False,
                        "id": sid,
                        "status": "cancelled",
                        "error": "cancelled",
                        "email": sess.get("email"),
                    }
                if b1.get("pause_requested") or str(b1.get("status") or "").lower() in (
                    "pausing",
                    "paused",
                ):
                    if sid in ctx._sessions:
                        ctx._sessions[sid]["status"] = "paused"
                        ctx._sessions[sid]["message"] = "paused before worker start"
                        ctx._sessions[sid]["pause_requested"] = True
                        ctx._sessions[sid]["cancel_requested"] = False
                        ctx._sessions[sid]["updated_at"] = ctx._now()
                        ctx._sessions[sid].pop("_receiver", None)
                        ctx._mirror_reg_sess(sid, ctx._sessions[sid])
                    return {
                        "ok": False,
                        "id": sid,
                        "status": "paused",
                        "paused": True,
                        "error": "paused",
                        "email": sess.get("email"),
                    }
                receiver = sess.get("_receiver")
                if sid in ctx._sessions:
                    ctx._sessions[sid]["status"] = "started"
                    ctx._sessions[sid]["message"] = (
                        f"started; email={ctx._sessions[sid].get('email') or ''}"
                    )
                    ctx._sessions[sid]["updated_at"] = ctx._now()
                    ctx._mirror_reg_sess(sid, ctx._sessions[sid])
            if not sid or receiver is None:
                return {
                    "ok": False,
                    "error": "registration session prepare failed",
                    "id": sid,
                }
                # Cross-batch admission control: many resumed batches otherwise all
                # run concurrency=8 and overwhelm local captcha + device-flow.
            admitted = False
            try:

                def _job_cancel() -> None:
                    with ctx._lock:
                        sess2 = ctx._sessions.get(sid) or {}
                        b2 = ctx._batches.get(bid) or {}
                        if sess2.get("cancel_requested") or b2.get("cancel_requested"):
                            raise ctx._RegCancelled(
                                "cancelled while waiting for admission"
                            )
                        if (
                            ctx._session_pause_requested(sess2)
                            or b2.get("pause_requested")
                            or str(b2.get("status") or "").lower()
                            in ("pausing", "paused")
                        ):
                            raise ctx._RegPaused("paused while waiting for admission")

                ctx._wait_reg_admission(check_cancel=_job_cancel)
                admitted = True
                runtime = getattr(worker_state, "browser_runtime", None)
                if runtime is None:
                    from xai_browser import XaiBrowserRuntime

                    runtime = XaiBrowserRuntime()
                    worker_state.browser_runtime = runtime
                ctx._run_registration(
                    sid,
                    key,
                    job_proxy or "",
                    receiver,
                    runtime,
                )
            except ctx._RegPaused as e:
                with ctx._lock:
                    if sid in ctx._sessions:
                        ctx._sessions[sid]["status"] = "paused"
                        ctx._sessions[sid]["error"] = None
                        ctx._sessions[sid]["message"] = str(e)
                        ctx._sessions[sid]["pause_requested"] = True
                        ctx._sessions[sid]["cancel_requested"] = False
                        ctx._sessions[sid]["updated_at"] = ctx._now()
                        ctx._mirror_reg_sess(sid, ctx._sessions[sid])
                return {
                    "ok": False,
                    "id": sid,
                    "status": "paused",
                    "paused": True,
                    "error": str(e),
                }
            except ctx._RegCancelled as e:
                with ctx._lock:
                    if sid in ctx._sessions:
                        ctx._sessions[sid]["status"] = "cancelled"
                        ctx._sessions[sid]["error"] = str(e)
                        ctx._sessions[sid]["message"] = str(e)
                        ctx._sessions[sid]["updated_at"] = ctx._now()
                        ctx._mirror_reg_sess(sid, ctx._sessions[sid])
                return {
                    "ok": False,
                    "id": sid,
                    "status": "cancelled",
                    "error": str(e),
                }
            finally:
                if admitted:
                    ctx._release_reg_admission()
                with ctx._lock:
                    if sid in ctx._sessions:
                        ctx._sessions[sid].pop("_receiver", None)
            with ctx._lock:
                final = ctx._sessions.get(sid) or {}
            st = str(final.get("status") or "")
            if st == "paused":
                return {
                    "ok": False,
                    "id": sid,
                    "status": st,
                    "paused": True,
                    "error": final.get("error"),
                    "email": final.get("email"),
                }
            ok = bool(final.get("imported_account_ids")) and st not in (
                "error",
                "failed",
                "cancelled",
                "stopped",
            )
            return {
                "ok": ok,
                "id": sid,
                "status": st,
                "error": final.get("error"),
                "email": final.get("email"),
            }

        def _note_result(
            idx: int, r: dict[str, Any] | None = None, exc: Exception | None = None
        ) -> None:
            nonlocal finished, ok_n, fail_n
            paused_result = bool(
                isinstance(r, dict)
                and (r.get("paused") or str(r.get("status") or "").lower() == "paused")
            )
            if paused_result:
                # An interrupted job is deliberately not a failed completion:
                # resume will create a replacement session for this batch slot.
                with ctx._lock:
                    b = ctx._batches.get(bid)
                    if b is not None:
                        b["updated_at"] = ctx._now()
                        b["inflight"] = len(in_flight)
                        b["runner_alive"] = True
                        b["message"] = (
                            f"pausing {finished}/{target_total} done "
                            f"(ok={ok_n} fail={fail_n}, interrupting active jobs)"
                        )
                        ctx._mirror_reg_batch(bid, dict(b))
                return
            finished += 1
            result_ok = bool(isinstance(r, dict) and r.get("ok") and exc is None)
            result_error = str(
                exc
                or ((r or {}).get("error") if isinstance(r, dict) else "")
                or ((r or {}).get("status") if isinstance(r, dict) else "")
                or ""
            )
            if exc is not None:
                fail_n += 1
                errors.append(f"#{idx}: {exc}")
            elif not isinstance(r, dict):
                fail_n += 1
                errors.append(f"#{idx}: empty result")
            elif r.get("ok"):
                ok_n += 1
            else:
                fail_n += 1
                errors.append(
                    f"#{idx}: {r.get('error') or r.get('status') or 'failed'}"
                )
            tuner.record(result_ok, result_error)
            if tuner.ready:
                solver_sample = (
                    ctx.probe_local_solver(None, timeout=0.75)
                    if provider == "local"
                    else {}
                )
                tuner.evaluate(ctx.system_sample(), solver_sample)
            with ctx._lock:
                b = ctx._batches.get(bid)
                if b is not None:
                    b["updated_at"] = ctx._now()
                    # Don't clobber explicit stop marker.
                    if not b.get("cancel_requested") and not b.get("pause_requested"):
                        b["status"] = "running"
                    b["finished"] = finished
                    b["ok_count"] = ok_n
                    b["fail_count"] = fail_n
                    b["spawned"] = len(b.get("session_ids") or [])
                    b["spawn_errors"] = errors[-20:]
                    b["runner_alive"] = True
                    b["inflight"] = len(in_flight)
                    b["auto_tune"] = tuner.snapshot(
                        successes=ok_n,
                        started_at=float(b.get("created_at") or ctx._now()),
                    )
                    b["message"] = (
                        f"running {finished}/{target_total} done "
                        f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency}, "
                        f"inflight={len(in_flight)})"
                    )
                    ctx._mirror_reg_batch(bid, dict(b))

        try:
            target_total = int(
                (ctx._load_reg_batch(bid) or {}).get("count") or remaining
            )
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix=f"gba-batch-{bid[-6:]}"
            ) as pool:
                while True:
                    # Fill every free slot immediately; no wave/batch barrier.
                    while (
                        next_i <= remaining
                        and len(in_flight) < _max_inflight()
                        and not _batch_cancel_requested()
                        and not _batch_pause_requested()
                    ):
                        fut = pool.submit(_job, next_i)
                        in_flight[fut] = next_i
                        next_i += 1
                        with ctx._lock:
                            bb = ctx._batches.get(bid)
                            if bb is not None:
                                bb["inflight"] = len(in_flight)
                                bb["updated_at"] = ctx._now()
                                if not bb.get("cancel_requested") and not bb.get(
                                    "pause_requested"
                                ):
                                    bb["status"] = "running"
                                bb["message"] = (
                                    f"running {finished}/{target_total} done "
                                    f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency}, "
                                    f"inflight={len(in_flight)})"
                                )
                                ctx._mirror_reg_batch(bid, dict(bb))

                    if not in_flight:
                        break

                    done, _pending = wait(
                        set(in_flight.keys()),
                        return_when=FIRST_COMPLETED,
                        timeout=0.5,
                    )
                    if not done:
                        # Timeout tick: re-check cancel and refresh progress.
                        if _batch_cancel_requested():
                            # Stop feeding new jobs; still drain in-flight workers.
                            pass
                        if _batch_pause_requested():
                            # Cancel any executor work that has not started yet;
                            # active jobs receive the pause signal cooperatively.
                            for future in list(in_flight):
                                future.cancel()
                        continue
                    for fut in done:
                        idx = in_flight.pop(fut, 0)
                        if fut.cancelled():
                            _note_result(
                                idx,
                                r={
                                    "ok": False,
                                    "status": "paused",
                                    "paused": True,
                                    "error": "paused before worker start",
                                },
                            )
                            continue
                        try:
                            r = fut.result()
                            _note_result(idx, r=r)
                        except Exception as e:  # noqa: BLE001
                            _note_result(idx, exc=e)

                            # If cancelled and no more work in flight, exit promptly.
                    if _batch_cancel_requested() and not in_flight:
                        break
                    if _batch_pause_requested() and not in_flight:
                        break
                        # If cancelled, do not submit more jobs even if capacity frees.
                    if _batch_cancel_requested():
                        continue

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

                cleanup_futures = [
                    pool.submit(_cleanup_worker_browser) for _ in range(workers)
                ]
                for cleanup_future in cleanup_futures:
                    try:
                        cleanup_future.result(timeout=20)
                    except Exception:
                        pass

        finally:
            stop_renew = True
            # Best-effort cancel of any leftover futures (usually empty now).
            for fut in list(in_flight.keys()):
                try:
                    fut.cancel()
                except Exception:
                    pass
            with ctx._lock:
                b = ctx._batches.get(bid)
                if b is not None:
                    b["updated_at"] = ctx._now()
                    b["finished"] = finished
                    b["ok_count"] = ok_n
                    b["fail_count"] = fail_n
                    b["spawned"] = len(b.get("session_ids") or [])
                    b["spawn_errors"] = errors[-20:]
                    b["runner_alive"] = False
                    b["inflight"] = 0
                    b["auto_tune"] = tuner.snapshot(
                        successes=ok_n,
                        started_at=float(b.get("created_at") or ctx._now()),
                    )
                    target_total = int(b.get("count") or finished or 0)
                    cancelled = bool(b.get("cancel_requested")) or str(
                        b.get("status") or ""
                    ).lower() in (
                        "stopping",
                        "cancelled",
                        "stopped",
                    )
                    paused = bool(b.get("pause_requested")) or str(
                        b.get("status") or ""
                    ).lower() in (
                        "pausing",
                        "paused",
                    )
                    if paused and finished < target_total:
                        b["status"] = "paused"
                        b["message"] = (
                            f"paused {finished}/{target_total} "
                            f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency})"
                        )
                    elif cancelled and finished < target_total:
                        b["status"] = "cancelled"
                        b["message"] = (
                            f"stopped {finished}/{target_total} "
                            f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency})"
                        )
                    elif fail_n and not ok_n:
                        b["status"] = "error"
                        b["error"] = "; ".join(errors[:5]) or "all failed"
                        b["message"] = (
                            f"finished {finished}/{target_total} "
                            f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency})"
                            + (f"; errors={len(errors)}" if errors else "")
                        )
                    elif fail_n:
                        b["status"] = "partial"
                        b["message"] = (
                            f"finished {finished}/{target_total} "
                            f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency})"
                            + (f"; errors={len(errors)}" if errors else "")
                        )
                    else:
                        b["status"] = "done"
                        b["message"] = (
                            f"finished {finished}/{target_total} "
                            f"(ok={ok_n} fail={fail_n}, threads={tuner.current_concurrency})"
                        )
                    ctx._mirror_reg_batch(bid, dict(b))
                    st = str(b.get("status") or "done")
                    ctx._record_register_task(
                        task_id=str(bid),
                        summary=str(b.get("message") or f"浏览器注册批次 {bid}"),
                        status=st,
                        ok=st in {"done", "partial"} and ok_n > 0,
                        progress_done=int(finished or 0),
                        progress_total=int(target_total or finished or 0),
                        finished=True,
                        detail={
                            "batch_id": bid,
                            "ok_count": ok_n,
                            "fail_count": fail_n,
                            "threads": tuner.current_concurrency,
                            "status": st,
                            "errors": (errors or [])[:10],
                            "phase": "finished",
                            "adapter_build": ctx.ADAPTER_BUILD,
                        },
                    )
            ctx._close_pipeline_phase(bid, "probe")
            ctx._close_pipeline_phase(bid, "import")
            ctx._release_batch_runner(bid, lock_token)

    ctx.threading.Thread(
        target=_run_batch,
        daemon=True,
        name=f"gba-batch-{bid[-8:]}",
    ).start()

    return {
        "ok": True,
        "batch_id": bid,
        "remaining": remaining,
        "concurrency": workers,
        "auto_tune": tuner.snapshot(
            successes=int(batch.get("ok_count") or 0),
            started_at=float(batch.get("created_at") or ctx._now()),
        ),
        "message": f"started batch {bid}: remaining={remaining} threads={workers}",
    }
