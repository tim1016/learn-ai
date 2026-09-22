"""SQLite Clerk intake liveness fence.

Fenced bodies perform bounded synchronous repository work only. They must not
perform broker I/O, yield the event loop, or yield caller code from a context
manager — with one sanctioned exception (#1993): :meth:`ReentrantAsyncLock.off_loop`
holds the fence while its fold runs on a worker thread. Awaiting that worker's
completion (and, after a cancellation, awaiting the abandoned worker's finish)
is the mechanism, not a violation; the yield detector skips exactly those
awaits via the permitted hold token and still counts every other yield. Broker
I/O stays forbidden for the whole hold, including across the hop.

The dynamic-scope marker is intentionally separate from task
ownership: child tasks inherit the marker so future broker-port guards can
reject work spawned from a fenced body, while lock-order checks can still ask
whether the current task is the fence owner.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextvars import ContextVar, Token
from types import TracebackType

logger = logging.getLogger(__name__)


class IntakeFenceYieldError(RuntimeError):
    """The SQLite Clerk intake fence yielded control while it was held."""

    def __init__(self) -> None:
        super().__init__("SQLite Clerk intake fence yielded while held")


class ReentrantAsyncLock:
    """Task-reentrant SQLite intake fence with dynamic-scope liveness checks."""

    def __init__(
        self,
        *,
        strict_yield_detection: bool = False,
        abandoned_hop_timeout_s: float = 30.0,
    ) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0
        self._scope_depth = ContextVar[int]("sqlite_clerk_intake_scope_depth", default=0)
        self._scope_tokens: list[Token[int]] = []
        self._strict_yield_detection = strict_yield_detection
        self._active_hold_token: object | None = None
        self._permitted_hold_token: object | None = None
        self._abandoned_hop_timeout_s = abandoned_hop_timeout_s
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

    async def off_loop[LocalResult](
        self,
        operation: Callable[..., LocalResult],
        *args: object,
        **kwargs: object,
    ) -> LocalResult:
        """Run one bounded repository fold on a worker thread, under intake.

        The fence's one sanctioned yield (#1993): awaiting the worker — and,
        after a cancellation, awaiting the abandoned worker's finish — is
        permitted for this hold's token while nothing else is. Cancellation
        never abandons a writing fold to the fence's next customer: the hold
        is released only after the worker completes, bounded by
        ``abandoned_hop_timeout_s`` so a wedged fold cannot brick shutdown.
        """
        async with self:
            hop = asyncio.ensure_future(asyncio.to_thread(operation, *args, **kwargs))
            self._permitted_hold_token = self._active_hold_token
            try:
                return await asyncio.shield(hop)
            except asyncio.CancelledError:
                await self._finish_abandoned_hop(hop)
                raise
            finally:
                self._permitted_hold_token = None

    async def _finish_abandoned_hop(self, hop: asyncio.Future[object]) -> None:
        """Let a fold whose caller was cancelled finish before intake releases.

        Cancelling the ``asyncio.to_thread`` await does not stop the worker
        already inside the fold, and releasing the fence while that worker
        may still write is the interleaving hazard #1993 exists to close —
        the same shape ``ClerkSqliteRepository.close`` solves by taking the
        write lock and ``ReconciliationSweep._finish_orphaned_revival`` bounds
        with one lease TTL. A second cancellation (shutdown) propagates after
        arming the outcome callback; either way the worker's eventual failure
        is logged, not dropped as an unretrieved task exception.
        """
        try:
            await asyncio.wait_for(asyncio.shield(hop), timeout=self._abandoned_hop_timeout_s)
        except asyncio.CancelledError:
            hop.add_done_callback(self._log_abandoned_hop_outcome)
            raise
        except TimeoutError:
            logger.critical(
                "SQLite Clerk intake fold still running after its caller was "
                "cancelled; releasing the fence with the worker unfinished",
                extra={
                    "intake_fence_event": "abandoned_hop_timeout",
                    "abandoned_hop_timeout_s": self._abandoned_hop_timeout_s,
                },
            )
            hop.add_done_callback(self._log_abandoned_hop_outcome)
        except Exception:
            self._log_abandoned_hop_outcome(hop)

    def _log_abandoned_hop_outcome(self, hop: asyncio.Future[object]) -> None:
        """Retrieve an abandoned fold's outcome so a failure is logged."""
        if hop.cancelled():
            return
        error = hop.exception()
        if error is None:
            return
        logger.error(
            "SQLite Clerk intake fold errored after its caller was cancelled",
            extra={"intake_fence_event": "abandoned_hop_error"},
            exc_info=error,
        )

    def _enter_dynamic_scope(self) -> Token[int]:
        return self._scope_depth.set(self._scope_depth.get() + 1)

    def _detect_event_loop_yield(
        self,
        owner: asyncio.Task[object],
        hold_token: object,
    ) -> None:
        if self._owner is not owner or self._active_hold_token is not hold_token:
            return
        if self._permitted_hold_token is hold_token:
            # The sanctioned off_loop hop (or its abandoned-hop finish) is the
            # one yield this hold is allowed; every other yield below it is
            # still a violation (#1993).
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
