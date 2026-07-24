from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Any


def system_sample() -> dict[str, Any]:
    logical = max(1, int(os.cpu_count() or 1))
    physical = max(1, logical // 2 if logical >= 4 else logical)
    sample: dict[str, Any] = {
        "logical_cores": logical,
        "physical_cores": physical,
        "cpu_percent": None,
        "memory_total_gb": None,
        "memory_available_gb": None,
        "memory_available_percent": None,
    }
    try:
        import psutil

        physical = int(psutil.cpu_count(logical=False) or physical)
        memory = psutil.virtual_memory()
        sample.update(
            {
                "physical_cores": max(1, physical),
                "cpu_percent": round(float(psutil.cpu_percent(interval=None)), 1),
                "memory_total_gb": round(float(memory.total) / (1024**3), 2),
                "memory_available_gb": round(float(memory.available) / (1024**3), 2),
                "memory_available_percent": round(float(memory.available) * 100.0 / max(1, memory.total), 1),
            }
        )
    except Exception:
        pass
    return sample


def machine_profile(
    *,
    provider: str = "local",
    solver_threads: int | None = None,
    local_slots: int | None = None,
    global_limit: int = 6,
) -> dict[str, Any]:
    sample = system_sample()
    physical = max(1, int(sample.get("physical_cores") or 1))
    cpu_cap = max(1, min(8, math.floor(physical * 0.75)))
    available_gb = sample.get("memory_available_gb")
    memory_cap = 8 if available_gb is None else max(1, min(8, math.floor(float(available_gb) / 0.6)))
    recommended = max(1, min(cpu_cap, memory_cap, 8))
    limits = [recommended, max(1, int(global_limit or 1))]
    if str(provider or "local").lower() == "local":
        if solver_threads:
            limits.append(max(1, int(solver_threads)))
        if local_slots:
            limits.append(max(1, int(local_slots)))
    effective = max(1, min(limits))
    return {
        **sample,
        "provider": str(provider or "local").lower(),
        "cpu_cap": cpu_cap,
        "memory_cap": memory_cap,
        "recommended_concurrency": recommended,
        "solver_threads": int(solver_threads or 0),
        "local_slots": int(local_slots or 0),
        "global_limit": max(1, int(global_limit or 1)),
        "effective_cap": effective,
    }


_PRESSURE_WORDS = (
    "rate_limited",
    "rate limited",
    "slow_down",
    "timeout",
    "timed out",
    "turnstile",
    "captcha",
    "queue",
    "busy",
    "crashed",
)


@dataclass
class AdaptiveRegistrationTuner:
    enabled: bool
    max_concurrency: int
    configured_stagger_ms: int
    window_size: int = 20
    current_concurrency: int = field(init=False)
    stagger_ms: int = field(init=False)
    last_reason: str = field(default="等待首个统计窗口", init=False)
    last_window: dict[str, Any] = field(default_factory=dict, init=False)
    changes: list[dict[str, Any]] = field(default_factory=list, init=False)
    _window: list[tuple[bool, str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.max_concurrency = max(1, int(self.max_concurrency or 1))
        configured = max(0, int(self.configured_stagger_ms or 0))
        self.current_concurrency = self.max_concurrency
        if self.enabled:
            self.stagger_ms = max(500, min(800, configured or 800))
            self.last_reason = "并发采用用户配置，等待首个错峰统计窗口"
        else:
            self.stagger_ms = configured
            self.last_reason = "手动配置"

    def record(self, ok: bool, error: str | None = None) -> None:
        self._window.append((bool(ok), str(error or "").lower()))

    @property
    def ready(self) -> bool:
        return self.enabled and len(self._window) >= self.window_size

    def evaluate(self, sample: dict[str, Any], solver: dict[str, Any] | None = None) -> bool:
        if not self.ready:
            return False
        window = self._window[: self.window_size]
        del self._window[: self.window_size]
        successes = sum(1 for ok, _error in window if ok)
        success_rate = successes / max(1, len(window))
        pressure_count = sum(
            1 for _ok, error in window if error and any(word in error for word in _PRESSURE_WORDS)
        )
        cpu = sample.get("cpu_percent")
        memory = sample.get("memory_available_percent")
        solver = solver or {}
        old_concurrency = self.current_concurrency
        old_stagger = self.stagger_ms

        overloaded = (
            success_rate < 0.88
            or pressure_count >= max(2, len(window) // 10)
            or (cpu is not None and float(cpu) >= 90.0)
            or (memory is not None and float(memory) < 15.0)
        )
        healthy = (
            success_rate >= 0.95
            and (cpu is None or float(cpu) < 80.0)
            and (memory is None or float(memory) >= 25.0)
            and pressure_count == 0
        )
        if overloaded:
            self.stagger_ms = min(2000, self.stagger_ms + 400)
            self.last_reason = "并发保持用户配置；检测到链路压力，增加错峰"
        elif healthy:
            self.stagger_ms = max(300, self.stagger_ms - 100)
            self.last_reason = "并发保持用户配置；链路状态稳定，缩短错峰"
        else:
            self.last_reason = "并发保持用户配置；当前窗口维持错峰"

        self.last_window = {
            "size": len(window),
            "success_rate": round(success_rate, 4),
            "pressure_count": pressure_count,
            "cpu_percent": cpu,
            "memory_available_percent": memory,
            "solver_in_flight": solver.get("in_flight"),
            "solver_threads": solver.get("thread"),
        }
        changed = old_concurrency != self.current_concurrency or old_stagger != self.stagger_ms
        if changed:
            self.changes.append(
                {
                    "at": time.time(),
                    "from_concurrency": old_concurrency,
                    "to_concurrency": self.current_concurrency,
                    "from_stagger_ms": old_stagger,
                    "to_stagger_ms": self.stagger_ms,
                    "reason": self.last_reason,
                }
            )
            self.changes = self.changes[-10:]
        return changed

    def snapshot(self, *, successes: int = 0, started_at: float | None = None) -> dict[str, Any]:
        elapsed = max(0.0, time.time() - float(started_at or time.time()))
        success_per_minute = float(successes or 0) * 60.0 / elapsed if elapsed > 0 else 0.0
        return {
            "enabled": bool(self.enabled),
            "current_concurrency": self.current_concurrency,
            "max_concurrency": self.max_concurrency,
            "stagger_ms": self.stagger_ms,
            "window_progress": len(self._window),
            "window_size": self.window_size,
            "success_per_minute": round(success_per_minute, 2),
            "last_reason": self.last_reason,
            "last_window": dict(self.last_window),
            "changes": list(self.changes),
        }
