"""Opt-in, bounded browser memory maintenance at safe observation boundaries."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Awaitable, Callable

ACTION_THRESHOLD = 300
MIN_ATTEMPT_INTERVAL_SECONDS = 10.0
GC_TIMEOUT_SECONDS = 1.0


class MemoryMaintenance:
    def __init__(self, *, enabled: bool | None = None, backend: str = "camoufox",
                 clock: Callable[[], float] = time.monotonic) -> None:
        configured = os.environ.get("BROWSER_MEMORY_MAINTENANCE") == "1" if enabled is None else enabled
        self.enabled = bool(configured and backend == "camoufox")
        self.state = "enabled" if self.enabled else "disabled"
        self.supported: bool | None = None
        self.completed_actions_since_attempt = 0
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.last_duration_ms: float | None = None
        self.last_error_code: str | None = None
        self._last_attempt_at: float | None = None
        self._clock = clock

    def record_completed_action(self) -> None:
        if self.enabled:
            self.completed_actions_since_attempt += 1

    def due(self) -> bool:
        if not self.enabled or self.completed_actions_since_attempt < ACTION_THRESHOLD:
            return False
        return (self._last_attempt_at is None
                or self._clock() - self._last_attempt_at >= MIN_ATTEMPT_INTERVAL_SECONDS)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "supported": self.supported, "state": self.state,
                "completed_actions_since_attempt": self.completed_actions_since_attempt,
                "attempts": self.attempts, "successes": self.successes, "failures": self.failures,
                "last_duration_ms": self.last_duration_ms, "last_error_code": self.last_error_code}

    async def attempt(self, request_gc: Callable[[], Awaitable[Any]] | None) -> bool:
        if not self.due():
            return False
        self.attempts += 1
        self.completed_actions_since_attempt = 0
        self._last_attempt_at = self._clock()
        started = self._clock()
        if not callable(request_gc):
            self.supported = False
            self._fail("gc_unsupported")
            self.last_duration_ms = round(max(0.0, self._clock() - started) * 1000, 3)
            return False
        self.supported = True
        try:
            await asyncio.wait_for(request_gc(), timeout=GC_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._fail("gc_timeout")
            ok = False
        except Exception:
            self._fail("gc_failed")
            ok = False
        else:
            self.successes += 1
            self.state = "enabled"
            self.last_error_code = None
            ok = True
        self.last_duration_ms = round(max(0.0, self._clock() - started) * 1000, 3)
        return ok

    def _fail(self, code: str) -> None:
        self.failures += 1
        self.last_error_code = code
        self.enabled = False
        self.state = "disabled_after_failure"
