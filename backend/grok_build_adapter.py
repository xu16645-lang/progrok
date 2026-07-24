"""Adapter: xAI browser registration -> grokcli-2api account pool.

1. register an x.ai account in an isolated browser context
2. extract and preserve the browser SSO/session cookies
3. approve the OAuth device request in the same browser context
4. import the resulting auth entry into the multi-account pool

Browser imports are deferred so the main API can still start when optional
dependencies are missing and report a clear availability error.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from performance_tuning import AdaptiveRegistrationTuner, machine_profile, system_sample
from registration_state import clear_state
from grok_registration import batch as _batch
from grok_registration import flow as _flow
from grok_registration import import_control as _import_control
from grok_registration import mailbox as _mailbox
from grok_registration import management as _management
from grok_registration import monitor as _monitor
from grok_registration import pipeline as _pipeline
from grok_registration import probe_control as _probe_control
from grok_registration import recovery as _recovery
from grok_registration import solver as _solver
from grok_registration import worker as _worker
from grok_registration.state import (
    DEFAULT_CONCURRENCY,
    MAX_CONCURRENCY,
    PROBE_RECHECK_DELAYS,
    REG_BATCH_RUNNER_LOCK_TTL,
    _RegCancelled,
    _RegPaused,
    _REGISTRATION_STAGE_BY_STATUS,
    _TERMINAL_STATUSES,
    _GLOBAL_REG_INFLIGHT_MAX,
    _LOCAL_CAPTCHA_CONCURRENCY,
    _active_batch_runners,
    _append_session_event,
    _batch_runner_lock_key,
    _batches,
    _close_pipeline_phase,
    _finish_pipeline_without_import,
    _interrupt_pipeline_for_batch,
    _is_cancel_status,
    _load_reg_batch,
    _load_reg_sess,
    _local_captcha_slots,
    _lock,
    _mark_pipeline_paused,
    _mirror_reg_batch,
    _mirror_reg_sess,
    _note_reg_pressure,
    _now,
    _pipeline_group,
    _pipeline_queue_lock,
    _pipeline_queues,
    _probe_group_slots,
    _record_register_task,
    _reg_redis,
    _release_batch_runner,
    _release_reg_admission,
    _renew_batch_runner,
    _session_cancel_requested,
    _session_pause_requested,
    _sessions,
    _snapshot_reg_config,
    _try_acquire_batch_runner,
    _wait_reg_admission,
    wait_pipeline_stagger,
)

BACKEND_DIR = Path(__file__).resolve().parent
APP_DIR = BACKEND_DIR.parent
RUNTIME_DATA_DIR = APP_DIR / "runtime" / "data"
GBA = APP_DIR / "vendor" / "grok-build-auth"
ADAPTER_BUILD = "2026-07-24-xai-browser-registration-1"
# Newly registered accounts often need a short settle window before probe.
REGISTER_PROBE_DELAY_SEC = float(
    os.environ.get("GROK2API_REG_PROBE_DELAY_SEC", "30") or 30
)

YESCAPTCHA_KEY = (
    os.environ.get("GROK2API_YESCAPTCHA_KEY")
    or os.environ.get("YESCAPTCHA_API_KEY")
    or ""
).strip()

CAPTCHA_PROVIDER = (
    (
        os.environ.get("GROK2API_CAPTCHA_PROVIDER")
        or os.environ.get("CAPTCHA_PROVIDER")
        or "local"
    )
    .strip()
    .lower()
)
if CAPTCHA_PROVIDER not in {"local", "yescaptcha"}:
    CAPTCHA_PROVIDER = "local"

LOCAL_SOLVER_URL = (
    (
        os.environ.get("GROK2API_LOCAL_SOLVER_URL")
        or os.environ.get("LOCAL_SOLVER_URL")
        or os.environ.get("GROK2API_YESCAPTCHA_ENDPOINT")
        or os.environ.get("YESCAPTCHA_ENDPOINT")
        or "http://127.0.0.1:5072"
    )
    .strip()
    .rstrip("/")
)

# Registration workers wait for the inline solver instead of racing its startup.
LOCAL_SOLVER_WAIT_SEC = float(
    os.environ.get("GROK2API_LOCAL_SOLVER_WAIT_SEC", "120") or 120
)
LOCAL_SOLVER_POLL_SEC = float(
    os.environ.get("GROK2API_LOCAL_SOLVER_POLL_SEC", "1.0") or 1.0
)


def _session_task_log_payload(sess: dict[str, Any] | None) -> dict[str, Any]:
    s = sess if isinstance(sess, dict) else {}
    st = str(s.get("status") or "done").lower() or "done"
    ok: bool | None
    if st in ("imported", "success", "completed", "done"):
        ok = True
    elif st in ("error", "failed", "expired", "protocol_error", "protocol_blocked"):
        ok = False
    elif st in ("cancelled", "stopped"):
        ok = False
    else:
        ok = None
    email = str(s.get("email") or "").strip()
    summary = str(s.get("message") or "").strip()
    if not summary:
        summary = f"浏览器注册 {email or s.get('id') or ''}".strip()
    return {
        "task_id": str(s.get("id") or ""),
        "summary": summary,
        "status": st,
        "ok": ok,
        "progress_done": 1 if st in _TERMINAL_STATUSES else 0,
        "progress_total": 1,
        "finished": st in _TERMINAL_STATUSES,
        "detail": {
            "session_id": s.get("id"),
            "batch_id": s.get("batch_id"),
            "email": email or None,
            "status": st,
            "error": s.get("error"),
            "imported_account_ids": list(s.get("imported_account_ids") or [])[:20],
            "adapter_build": s.get("adapter_build") or ADAPTER_BUILD,
        },
    }


def _run_probe_wave(group: str, session_ids: list[str], limit: int) -> None:
    _pipeline.run_probe_wave(
        group,
        session_ids,
        limit,
        retry_probe=_retry_probe_now,
    )


def _run_import_wave(group: str, session_ids: list[str], limit: int) -> None:
    _pipeline.run_import_wave(
        group,
        session_ids,
        limit,
        retry_import=_retry_import_now,
    )


def _continuous_import_dispatch_loop(key: str) -> None:
    _pipeline.continuous_import_dispatch_loop(
        key,
        max_concurrency=MAX_CONCURRENCY,
        run_import_wave=_run_import_wave,
    )


def _pipeline_dispatch_loop(key: str) -> None:
    _pipeline.pipeline_dispatch_loop(
        key,
        run_probe_wave=_run_probe_wave,
        run_import_wave=_run_import_wave,
        continuous_import_dispatch=_continuous_import_dispatch_loop,
    )


def _enqueue_pipeline_phase(sid: str, phase: str = "probe") -> None:
    _pipeline.enqueue_pipeline_phase(
        sid,
        phase,
        max_concurrency=MAX_CONCURRENCY,
        dispatch_loop=_pipeline_dispatch_loop,
    )


def _clean_old_sessions() -> None:
    _monitor.clean_old_sessions()


def _compact_session(sess: dict[str, Any]) -> dict[str, Any]:
    return _monitor.compact_session(sess)


def ensure_xconsole() -> None:
    _solver.ensure_xconsole(GBA)


def _local_solver_base_url(url: str | None = None) -> str:
    return _solver.local_solver_base_url(url, configured_url=LOCAL_SOLVER_URL)


def probe_local_solver(
    url: str | None = None,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    return _solver.probe_local_solver(
        url,
        timeout=timeout,
        configured_url=LOCAL_SOLVER_URL,
    )


def wait_for_local_solver(
    url: str | None = None,
    *,
    timeout_sec: float | None = None,
    poll_sec: float | None = None,
    progress: Any | None = None,
) -> dict[str, Any]:
    return _solver.wait_for_local_solver(
        url,
        timeout_sec=timeout_sec,
        poll_sec=poll_sec,
        progress=progress,
        configured_url=LOCAL_SOLVER_URL,
        default_wait_sec=LOCAL_SOLVER_WAIT_SEC,
        default_poll_sec=LOCAL_SOLVER_POLL_SEC,
    )


def registration_available() -> dict[str, Any]:
    try:
        from xai_browser import _ensure_playwright

        _ensure_playwright()
        return {
            "ok": True,
            "available": True,
            "engine": "xai-browser",
            "adapter_build": ADAPTER_BUILD,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "available": False,
            "engine": "xai-browser",
            "adapter_build": ADAPTER_BUILD,
            "error": str(exc),
        }


# --------------------------------------------------------------------------- #
# mail provider: moemail / yyds (reuse grokcli-2api config)
# --------------------------------------------------------------------------- #
def _make_email_receiver(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    prefix: str | None = None,
    domain: str | None = None,
    expiry_ms: int | None = None,
    mail_provider: str | None = None,
    hotmail_local_base_url: str | None = None,
):
    return _mailbox.make_email_receiver(
        api_key=api_key,
        base_url=base_url,
        prefix=prefix,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        cancelled_error=_RegCancelled,
    )


def _proxy_url() -> str:
    return _mailbox.proxy_url()


def _proxy_pool(
    proxy_text: str | None = None,
    *,
    username: str | None = None,
    password: str | None = None,
) -> list[str]:
    return _mailbox.proxy_pool(
        proxy_text,
        username=username,
        password=password,
    )


def _pick_proxy_from_pool(
    pool: list[str],
    *,
    strategy: str | None = None,
    index: int | None = None,
) -> str:
    return _mailbox.pick_proxy_from_pool(pool, strategy=strategy, index=index)


def _proxy_for_browser(proxy_url: str | None) -> dict[str, str] | None:
    return _mailbox.proxy_for_browser(proxy_url)


# --------------------------------------------------------------------------- #
# registration flow
# --------------------------------------------------------------------------- #
def _registration_context() -> _flow.RegistrationContext:
    """Expose the live adapter namespace to the extracted registration flow."""
    return _flow.RegistrationContext(globals())


def _prepare_registration_session(
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
    return _flow._prepare_registration_session(
        _registration_context(),
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
        should_cancel=should_cancel,
    )


def _start_one_registration(
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
    return _flow._start_one_registration(
        _registration_context(),
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


def start_registration(
    *,
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
    return _flow.start_registration(
        _registration_context(),
        captcha_provider=captcha_provider,
        local_solver_url=local_solver_url,
        yescaptcha_key=yescaptcha_key,
        proxy=proxy,
        proxy_username=proxy_username,
        proxy_password=proxy_password,
        proxy_strategy=proxy_strategy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        prefix=prefix,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        count=count,
        concurrency=concurrency,
        stagger_ms=stagger_ms,
        probe_delay_sec=probe_delay_sec,
        post_registration=post_registration,
        start_paused=start_paused,
        auto_tune_enabled=auto_tune_enabled,
        headless=headless,
    )


def _spawn_batch_runner(
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
    return _batch._spawn_batch_runner(
        _registration_context(),
        batch_id,
        remaining=remaining,
        concurrency=concurrency,
        stagger_ms=stagger_ms,
        captcha_provider=captcha_provider,
        yescaptcha_key=yescaptcha_key,
        proxy=proxy,
        proxy_strategy=proxy_strategy,
        moemail_api_key=moemail_api_key,
        moemail_base_url=moemail_base_url,
        prefix=prefix,
        domain=domain,
        expiry_ms=expiry_ms,
        mail_provider=mail_provider,
        hotmail_local_base_url=hotmail_local_base_url,
        post_registration=post_registration,
        auto_tune_enabled=auto_tune_enabled,
        headless=headless,
    )


def _run_registration(
    sid: str,
    yescaptcha_key: str,
    proxy: str,
    receiver: Any,
    browser_runtime: Any | None = None,
) -> None:
    _worker._run_registration(
        _registration_context(),
        sid,
        yescaptcha_key,
        proxy,
        receiver,
        browser_runtime,
    )


def _nonterminal_session_statuses() -> frozenset[str]:
    return _recovery._nonterminal_session_statuses(_registration_context())


def reclaim_orphaned_registration_sessions(
    *, stale_sec: float | None = None, batch_id: str | None = None
) -> dict[str, Any]:
    return _recovery.reclaim_orphaned_registration_sessions(
        _registration_context(), stale_sec=stale_sec, batch_id=batch_id
    )


def resume_registration_batch(
    batch_id: str, *, force: bool = False, reclaim_stale_sec: float | None = None
) -> dict[str, Any]:
    return _recovery.resume_registration_batch(
        _registration_context(),
        batch_id,
        force=force,
        reclaim_stale_sec=reclaim_stale_sec,
    )


def get_registration_performance_profile(
    provider: str | None = None, local_solver_url: str | None = None
) -> dict[str, Any]:
    return _recovery.get_registration_performance_profile(
        _registration_context(), provider, local_solver_url
    )


def pause_registration_batch(batch_id: str) -> dict[str, Any]:
    return _recovery.pause_registration_batch(_registration_context(), batch_id)


def _schedule_probe_recheck(
    sid: str,
    config: dict[str, Any],
    *,
    current_attempt: int,
    next_attempt: int,
    delay: float,
) -> None:
    return _probe_control._schedule_probe_recheck(
        _registration_context(),
        sid,
        config,
        current_attempt=current_attempt,
        next_attempt=next_attempt,
        delay=delay,
    )


def _retry_probe_now(
    session_id: str,
    post_registration: dict[str, Any] | None = None,
    *,
    manual_retry: bool = True,
    recheck_attempt: int = 0,
) -> dict[str, Any]:
    return _probe_control._retry_probe_now(
        _registration_context(),
        session_id,
        post_registration,
        manual_retry=manual_retry,
        recheck_attempt=recheck_attempt,
    )


def retry_registration_probe(
    session_id: str, post_registration: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _probe_control.retry_registration_probe(
        _registration_context(), session_id, post_registration
    )


def retry_registration_batch_probe(
    batch_id: str, post_registration: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _probe_control.retry_registration_batch_probe(
        _registration_context(), batch_id, post_registration
    )


def pause_registration_batch_probe(batch_id: str) -> dict[str, Any]:
    return _probe_control.pause_registration_batch_probe(
        _registration_context(), batch_id
    )


def resume_registration_batch_probe(batch_id: str) -> dict[str, Any]:
    return _probe_control.resume_registration_batch_probe(
        _registration_context(), batch_id
    )


def _retry_import_now(
    session_id: str,
    post_registration: dict[str, Any] | None = None,
    *,
    manual_retry: bool = True,
) -> dict[str, Any]:
    return _import_control._retry_import_now(
        _registration_context(),
        session_id,
        post_registration,
        manual_retry=manual_retry,
    )


def retry_registration_import(
    session_id: str, post_registration: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _import_control.retry_registration_import(
        _registration_context(), session_id, post_registration
    )


def retry_registration_batch_import(
    batch_id: str, post_registration: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _import_control.retry_registration_batch_import(
        _registration_context(), batch_id, post_registration
    )


def reclaim_orphaned_registration_batches(
    *,
    stale_sec: float | None = None,
    auto_resume: bool = True,
    max_batches: int | None = None,
) -> dict[str, Any]:
    return _management.reclaim_orphaned_registration_batches(
        _registration_context(),
        stale_sec=stale_sec,
        auto_resume=auto_resume,
        max_batches=max_batches,
    )


def stop_registration_session(session_id: str) -> dict[str, Any]:
    return _management.stop_registration_session(_registration_context(), session_id)


def stop_registration_batch(batch_id: str) -> dict[str, Any]:
    return _management.stop_registration_batch(_registration_context(), batch_id)


def stop_all_active_registrations() -> dict[str, Any]:
    return _management.stop_all_active_registrations(_registration_context())


def list_registration_sessions() -> dict[str, Any]:
    return _management.list_registration_sessions(_registration_context())


def reset_registration_monitor() -> dict[str, Any]:
    return _management.reset_registration_monitor(_registration_context())


def get_registration_session(
    sid: str, *, include_auth_json: bool = False
) -> dict[str, Any] | None:
    return _management.get_registration_session(
        _registration_context(), sid, include_auth_json=include_auth_json
    )


def _batch_stats(
    session_ids: list[str], *, batch: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _management._batch_stats(_registration_context(), session_ids, batch=batch)


def get_registration_batch(batch_id: str) -> dict[str, Any] | None:
    return _management.get_registration_batch(_registration_context(), batch_id)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    print("grok-build-auth adapter for grokcli-2api")
    result = start_registration()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        return 1

    sid = result["id"]
    deadline = time.time() + 600
    while time.time() < deadline:
        sess = get_registration_session(sid, include_auth_json=True)
        if not sess:
            print("session disappeared", file=sys.stderr)
            return 1
        status = sess.get("status")
        print(f"[{time.strftime('%H:%M:%S')}] {status}: {sess.get('message')}")
        if status in ("imported", "error"):
            print(json.dumps(sess, ensure_ascii=False, indent=2))
            return 0 if status == "imported" else 1
        time.sleep(5)

    print("timeout", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
