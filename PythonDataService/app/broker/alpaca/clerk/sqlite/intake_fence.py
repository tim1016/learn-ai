"""SQLite Clerk intake liveness fence.

Fenced bodies perform bounded synchronous repository work only. They must not
perform broker I/O, yield the event loop, or yield caller code from a context
manager. The dynamic-scope marker is intentionally separate from task
ownership: child tasks inherit the marker so future broker-port guards can
reject work spawned from a fenced body, while lock-order checks can still ask
whether the current task is the fence owner.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar, Token
from types import TracebackType

logger = logging.getLogger(__name__)


class IntakeFenceYieldError(RuntimeError):
    """The SQLite Clerk intake fence yielded control while it was held."""

    def __init__(self) -> None:
        super().__init__("SQLite Clerk intake fence yielded while held")


class ReentrantAsyncLock:
    """Task-reentrant SQLite intake fence with dynamic-scope liveness checks."""

    def __init__(self, *, strict_yield_detection: bool = False) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0
        self._scope_depth = ContextVar[int]("sqlite_clerk_intake_scope_depth", default=0)
        self._scope_tokens: list[Token[int]] = []
        self._strict_yield_detection = strict_yield_detection
        self._active_hold_token: object | None = None
        self._hold_started_at: float | None = None
        self._current_hold_yielded = False
        self._yielded_fence_count = 0
        self._last_hold_duration_seconds: float | None = None

    @property
    def yielded_fence_count(self) -> int:
        """Return the number of outermost holds that yielded while fenced."""
        return self._yielded_fence_count

    @property
    def last_hold_duration_seconds(self) -> float | None:
        """Return the duration of the most recently released outermost hold."""
        return self._last_hold_duration_seconds

    def held_by_current_task(self) -> bool:
        """Return whether this task owns the fence for lock-order assertions."""
        return self._owner is asyncio.current_task()

    def current_scope_depth(self) -> int:
        """Return this task's inherited fence dynamic-scope depth."""
        return self._scope_depth.get()

    async def __aenter__(self) -> ReentrantAsyncLock:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("SQLite Clerk intake requires an asyncio task")
        if self._owner is current:
            self._depth += 1
            self._scope_tokens.append(self._enter_dynamic_scope())
            return self

        await self._lock.acquire()
        self._owner = current
        self._depth = 1
        self._scope_tokens.append(self._enter_dynamic_scope())
        self._hold_started_at = time.perf_counter()
        self._current_hold_yielded = False
        hold_token = object()
        self._active_hold_token = hold_token
        asyncio.get_running_loop().call_soon(self._detect_event_loop_yield, current, hold_token)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        current = asyncio.current_task()
        if self._owner is not current:
            raise RuntimeError("SQLite Clerk intake released by a non-owner task")

        self._depth -= 1
        token = self._scope_tokens.pop()
        self._scope_depth.reset(token)
        if self._depth != 0:
            return

        self._last_hold_duration_seconds = self._hold_duration_seconds()
        yielded = self._current_hold_yielded
        self._owner = None
        self._active_hold_token = None
        self._hold_started_at = None
        self._current_hold_yielded = False
        self._lock.release()
        if yielded and self._strict_yield_detection and exc_type is None:
            raise IntakeFenceYieldError()

    def _enter_dynamic_scope(self) -> Token[int]:
        return self._scope_depth.set(self._scope_depth.get() + 1)

    def _detect_event_loop_yield(
        self,
        owner: asyncio.Task[object],
        hold_token: object,
    ) -> None:
        if self._owner is not owner or self._active_hold_token is not hold_token:
            return
        if self._current_hold_yielded:
            return

        self._current_hold_yielded = True
        self._yielded_fence_count += 1
        logger.warning(
            "SQLite Clerk intake fence yielded while held",
            extra={
                "intake_fence_event": "yielded_while_held",
                "owner_task_name": owner.get_name(),
                "yielded_fence_count": self._yielded_fence_count,
                "hold_duration_seconds": self._hold_duration_seconds(),
            },
        )

    def _hold_duration_seconds(self) -> float:
        if self._hold_started_at is None:
            return 0.0
        return time.perf_counter() - self._hold_started_at


__all__ = ["IntakeFenceYieldError", "ReentrantAsyncLock"]
