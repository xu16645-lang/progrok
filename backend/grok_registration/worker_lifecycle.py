"""Session cancellation and phase state for Grok registration workers."""

from __future__ import annotations

from typing import Any


class WorkerSessionLifecycle:
    """Synchronize registration lifecycle state independently of protocol work."""

    def __init__(self, ctx, sid, sess):
        self.ctx = ctx
        self.sid = sid
        self.sess = sess

    def refresh_cancel_from_redis(self) -> None:
        """Pull cancel_requested from Redis so multi-worker stop works.

        Also honour batch-level stop so stopping a batch reaches in-flight
        sessions even if the session mirror lags.
        """
        if not self.ctx._reg_redis():
            return
        try:
            from store import sessions_redis

            remote = sessions_redis.reg_sess_get(self.sid)
            batch_cancel = False
            batch_pause = False
            remote_batch = None
            bid = ""
            with self.ctx._lock:
                local_sess = self.ctx._sessions.get(self.sid) or self.sess or {}
                bid = str(local_sess.get("batch_id") or "")
            if not bid and isinstance(remote, dict):
                bid = str(remote.get("batch_id") or "")
            if bid:
                try:
                    remote_batch = sessions_redis.reg_batch_get(bid)
                except Exception:
                    remote_batch = None
                if isinstance(remote_batch, dict) and (
                    remote_batch.get("cancel_requested")
                    or self.ctx._is_cancel_status(remote_batch.get("status"))
                ):
                    batch_cancel = True
                    with self.ctx._lock:
                        bb = self.ctx._batches.get(bid) or dict(remote_batch)
                        bb["cancel_requested"] = True
                        if str(bb.get("status") or "").lower() not in (
                            "cancelled",
                            "stopped",
                            "done",
                            "partial",
                            "error",
                        ):
                            bb["status"] = remote_batch.get("status") or "stopping"
                        bb["updated_at"] = self.ctx._now()
                        self.ctx._batches[bid] = bb
                if isinstance(remote_batch, dict) and (
                    remote_batch.get("pause_requested")
                    or str(remote_batch.get("status") or "").lower()
                    in ("pausing", "paused")
                ):
                    batch_pause = True
                    with self.ctx._lock:
                        bb = self.ctx._batches.get(bid) or dict(remote_batch)
                        bb["pause_requested"] = True
                        if (
                            str(bb.get("status") or "").lower()
                            not in self.ctx._TERMINAL_STATUSES
                        ):
                            bb["status"] = "pausing"
                        bb["updated_at"] = self.ctx._now()
                        self.ctx._batches[bid] = bb
            sess_cancel = isinstance(remote, dict) and (
                remote.get("cancel_requested")
                or self.ctx._is_cancel_status(remote.get("status"))
            )
            sess_pause = isinstance(remote, dict) and (
                remote.get("pause_requested")
                or str(remote.get("status") or "").lower() in ("pausing", "paused")
            )
            if (
                not sess_cancel
                and (not batch_cancel)
                and (not sess_pause)
                and (not batch_pause)
            ):
                return
            with self.ctx._lock:
                cur = self.ctx._sessions.get(self.sid) or self.sess
                if sess_cancel or batch_cancel:
                    cur["cancel_requested"] = True
                    if (
                        str(cur.get("status") or "").lower()
                        not in self.ctx._TERMINAL_STATUSES
                    ):
                        if sess_cancel and str(remote.get("status") or "").lower() in (
                            "stopping",
                            "cancelled",
                            "stopped",
                        ):
                            cur["status"] = remote.get("status") or "stopping"
                            if remote.get("message"):
                                cur["message"] = remote.get("message")
                        elif batch_cancel:
                            cur["status"] = "stopping"
                            cur["message"] = "stop requested via batch"
                elif sess_pause or batch_pause:
                    cur["pause_requested"] = True
                    cur["cancel_requested"] = False
                    if (
                        str(cur.get("status") or "").lower()
                        not in self.ctx._TERMINAL_STATUSES
                    ):
                        cur["status"] = "pausing"
                        cur["message"] = "pause requested via batch"
                self.ctx._sessions[self.sid] = cur
        except Exception:
            pass

    def update(self, status: str, message: str, **kwargs: Any) -> None:
        self.refresh_cancel_from_redis()
        with self.ctx._lock:
            cur = self.ctx._sessions.get(self.sid) or self.sess
            bid = str(cur.get("batch_id") or "")
            batch_hit = False
            pause_hit = self.ctx._session_pause_requested(cur)
            if bid:
                bb = self.ctx._batches.get(bid) or {}
                if bb.get("cancel_requested") or self.ctx._is_cancel_status(
                    bb.get("status")
                ):
                    batch_hit = True
                    cur["cancel_requested"] = True
                if bb.get("pause_requested") or str(bb.get("status") or "").lower() in (
                    "pausing",
                    "paused",
                ):
                    pause_hit = True
                    cur["pause_requested"] = True
            if (
                self.ctx._session_cancel_requested(cur) or batch_hit
            ) and status not in ("cancelled", "stopped", "error", "imported"):
                raise self.ctx._RegCancelled(cur.get("message") or "cancelled by user")
            if pause_hit and status != "paused":
                raise self.ctx._RegPaused(cur.get("message") or "paused by user")
            now = self.ctx._now()
            previous_status = str(cur.get("_phase_status") or "")
            previous_started = float(cur.get("_phase_started_at") or 0.0)
            if previous_status and previous_status != status and (previous_started > 0):
                stage = self.ctx._REGISTRATION_STAGE_BY_STATUS.get(previous_status)
                if stage:
                    durations = dict(cur.get("phase_durations") or {})
                    durations[stage] = round(
                        float(durations.get(stage) or 0.0)
                        + max(0.0, now - previous_started),
                        3,
                    )
                    cur["phase_durations"] = durations
            if status in self.ctx._REGISTRATION_STAGE_BY_STATUS:
                if previous_status != status:
                    cur["_phase_status"] = status
                    cur["_phase_started_at"] = now
            else:
                cur.pop("_phase_status", None)
                cur.pop("_phase_started_at", None)
            cur["status"] = status
            cur["message"] = message
            cur["updated_at"] = now
            cur.update(kwargs)
            self.ctx._append_session_event(cur, status, message, at=now)
            self.ctx._sessions[self.sid] = cur
            self.ctx._mirror_reg_sess(self.sid, cur)

    def check_cancel(self) -> None:
        self.refresh_cancel_from_redis()
        with self.ctx._lock:
            cur = self.ctx._sessions.get(self.sid) or self.sess
            bid = str(cur.get("batch_id") or "")
            if bid:
                bb = self.ctx._batches.get(bid) or {}
                if bb.get("cancel_requested") or self.ctx._is_cancel_status(
                    bb.get("status")
                ):
                    cur["cancel_requested"] = True
                    self.ctx._sessions[self.sid] = cur
                elif bb.get("pause_requested") or str(
                    bb.get("status") or ""
                ).lower() in ("pausing", "paused"):
                    cur["pause_requested"] = True
                    self.ctx._sessions[self.sid] = cur
            if self.ctx._session_cancel_requested(cur):
                raise self.ctx._RegCancelled(cur.get("message") or "cancelled by user")
            if self.ctx._session_pause_requested(cur):
                raise self.ctx._RegPaused(cur.get("message") or "paused by user")
