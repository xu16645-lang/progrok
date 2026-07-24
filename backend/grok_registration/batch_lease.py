"""Durable lease maintenance for a running Grok registration batch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def maintain_batch_runner_lease(
    ctx: Any,
    batch_id: str,
    lock_token: str,
    *,
    stop_requested: Callable[[], bool],
    cancel_requested: Callable[[], bool],
) -> None:
    """Renew the distributed batch lock until the scheduler finishes."""
    while not stop_requested():
        ctx.time.sleep(max(5.0, ctx.REG_BATCH_RUNNER_LOCK_TTL / 3))
        if stop_requested():
            break
        if cancel_requested():
            with ctx._lock:
                batch = ctx._batches.get(batch_id)
                if batch is not None:
                    batch["cancel_requested"] = True
                    if str(batch.get("status") or "").lower() not in (
                        "cancelled",
                        "stopped",
                        "done",
                        "partial",
                        "error",
                    ):
                        batch["status"] = "stopping"
                    batch["updated_at"] = ctx._now()
                    batch["runner_alive"] = True
                    ctx._mirror_reg_batch(batch_id, dict(batch))
        ctx._renew_batch_runner(batch_id, lock_token)
        with ctx._lock:
            batch = ctx._batches.get(batch_id)
            if batch is not None:
                batch["updated_at"] = ctx._now()
                batch["runner_alive"] = True
                batch["owner_pid"] = ctx.os.getpid()
                ctx._mirror_reg_batch(batch_id, dict(batch))
