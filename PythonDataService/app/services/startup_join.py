"""Start a run's live stream before its warmup, so the two meet without a hole (#2410).

A run used to warm on history through the last closed minute and only then
subscribe. The minute the subscription joined partway through was omitted as
short (#2364), and a minute boundary passing between the history fetch and
the subscription lost a whole minute with no record at all. Either way the
first live bucket decided on indicators that skipped a minute.

The owner's rule is "reconstruct the missing state, then trade the next
timely signal":

1. Subscribe first. Every live bar is held in a :class:`LiveStartBuffer`
   until warmup is done; none is retained or decided on before then.
2. The stream itself says where it takes over (:class:`StreamSeam`): the
   first minute it delivers, or the minute after the one it joined partway
   through and omitted. Nothing is guessed from the wall clock.
3. Warmup history runs exactly through that seam, fetched after the joined
   minute closes plus a settle time, and retried until one fixed
   :class:`StartupDeadline`; nothing extends it.
4. The held bars are then delivered as ordinary live bars. The run's existing
   gates judge each on its own close, so a decision that fell due during
   preparation is refused as late -- never submitted as a catch-up entry.

Waiting for the stream to join is not bounded here: before its first print
(overnight, or a thin symbol outside regular hours) there is nothing to
repair, and the feed's own liveness rules govern the line (owner decision
2026-09-24: the budget starts when the joined minute closes).
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TypeVar

from app.config import settings
from app.marketdata.feed import (
    RESUME_HOLE_UNFILLED,
    WARMUP_HISTORY_UNAVAILABLE,
    FeedContinuityEvent,
    MarketDataBar,
    MarketDataFeedError,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_MINUTE_MS = 60_000
_RETRY_MS = 2_000
"""How long a failed history attempt waits before asking again."""

RETRYABLE_REASONS: frozenset[str] = frozenset({WARMUP_HISTORY_UNAVAILABLE, RESUME_HOLE_UNFILLED})
"""History that is missing now may be published a moment later. An after-hours
hole is a known unsupported repair, refused at once rather than waited out."""

T = TypeVar("T")


@dataclass(frozen=True)
class StreamSeam:
    """Where a run's live stream takes over from its warmup.

    ``live_from_ms`` is the open of the first minute the stream observed whole;
    every minute before it comes from warmup. ``joined_minute_start_ms`` is set
    when the stream joined the minute before it partway through and omitted it
    (``cause="stream_joined"``), so warmup owes that minute from history.
    """

    live_from_ms: int
    joined_minute_start_ms: int | None = None


@dataclass(frozen=True)
class StartupDeadline:
    """The one fixed budget a run's startup join is held to.

    Read once, when the stream says where it takes over (``known_at_ms``):
    that is the moment the joined minute's omission is resolved, a few seconds
    after it closes, and the moment a repair becomes possible (owner decision,
    #2410). A settings change applies only to preparations that begin
    afterwards. History is first asked for a settle time after the seam, so
    IBKR's revision of a just-closed minute has landed.
    """

    not_before_ms: int
    deadline_ms: int

    @classmethod
    def for_seam(cls, seam: StreamSeam, *, known_at_ms: int) -> StartupDeadline:
        return cls(
            not_before_ms=seam.live_from_ms + settings.STARTUP_JOIN_SETTLE_MS,
            deadline_ms=known_at_ms + settings.STARTUP_JOIN_BUDGET_MS,
        )

    async def run(self, attempt: Callable[[], Awaitable[T]], *, symbol: str) -> T:
        """Run ``attempt`` after the settle time, retrying until the deadline.

        A retryable refusal (history not there yet) is asked again every
        ``_RETRY_MS``; any other refusal propagates at once. An attempt still in
        flight at the deadline is cancelled rather than allowed to extend it.
        The refusal that ends the budget is the last attempt's, so the run's
        record names what was still missing.
        """
        await _sleep_until(self.not_before_ms)
        attempts = 0
        while True:
            attempts += 1
            remaining_ms = self.deadline_ms - now_ms_utc()
            try:
                return await asyncio.wait_for(attempt(), timeout=max(remaining_ms, 0) / 1000)
            except TimeoutError as exc:
                raise MarketDataFeedError(
                    f"warmup history for {symbol} was still being fetched at the startup-join deadline",
                    reason=WARMUP_HISTORY_UNAVAILABLE,
                ) from exc
            except MarketDataFeedError as exc:
                if exc.reason not in RETRYABLE_REASONS or now_ms_utc() + _RETRY_MS >= self.deadline_ms:
                    raise
                logger.warning(
                    "Startup join history not complete yet; asking again",
                    extra={
                        "action": "startup_join_retry",
                        "symbol": symbol,
                        "attempt": attempts,
                        "reason_code": exc.reason,
                        "deadline_ms": self.deadline_ms,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(_RETRY_MS / 1000)


async def _sleep_until(instant_ms: int) -> None:
    delay_ms = instant_ms - now_ms_utc()
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000)


class LiveStartBuffer:
    """A run's live stream, opened before its warmup and held until warmup is done.

    A pump task drains the stream from the moment the buffer starts, so the
    feed's own liveness and continuity rules keep running during preparation;
    a feed failure is re-raised to whoever waits next. ``note_event`` is fed
    the run's continuity evidence, which is how the stream reports the minute
    it joined partway through.

    ``retain`` is the run's per-bar admission and evidence write. Held bars are
    not retained until :meth:`release` -- warmup history has to reach the
    ledger first -- and from then on the pump retains each bar the moment the
    stream delivers it, before asking the stream for the next one. That keeps
    the ledger's single causal order over bars and continuity events: an event
    the feed records while producing a bar is journaled after every bar
    delivered before it.
    """

    def __init__(
        self,
        bars: AsyncIterator[MarketDataBar],
        *,
        retain: Callable[[MarketDataBar], Awaitable[None]],
    ) -> None:
        self._source = bars
        self._retain = retain
        self._unretained: deque[MarketDataBar] = deque()
        self._retained: deque[MarketDataBar] = deque()
        self._released = False
        self._changed = asyncio.Event()
        self._joined_minute_start_ms: int | None = None
        self._failure: BaseException | None = None
        self._ended = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start pumping; the pump never outlives the task that started it.

        The run opens its stream in warmup but only reads it later, in its
        live loop. A run that ends in between -- a replay error, a Stop during
        preparation -- must not leave the subscription open, so the pump is
        cancelled when its owning task finishes, however it finishes.
        """
        task = asyncio.create_task(self._pump(), name="live-start-buffer")
        self._task = task
        owner = asyncio.current_task()
        if owner is not None:
            owner.add_done_callback(lambda _owner: task.cancel())

    def note_event(self, event: FeedContinuityEvent) -> None:
        """Remember the minute the stream joined partway through, if this is that fact."""
        if (
            event.kind == "gap"
            and event.cause == "stream_joined"
            and event.window_start_ms is not None
            and self._joined_minute_start_ms is None
        ):
            self._joined_minute_start_ms = event.window_start_ms
            self._changed.set()

    async def seam(self) -> StreamSeam:
        """Wait until the stream says where it takes over.

        The joined minute's ``gap`` is recorded before the next minute can be
        delivered, so whichever fact arrives first is the seam: a delivered
        bar opens it, or the omitted join minute's close does.
        """
        while True:
            if self._joined_minute_start_ms is not None:
                return StreamSeam(
                    live_from_ms=self._joined_minute_start_ms + _MINUTE_MS,
                    joined_minute_start_ms=self._joined_minute_start_ms,
                )
            if self._unretained:
                return StreamSeam(live_from_ms=self._unretained[0].start_ms)
            if self._ended:
                self._raise_failure()
                raise MarketDataFeedError("the live stream ended before it said where it takes over")
            await self._wait()

    async def release(self) -> None:
        """Retain every held bar in delivery order; the pump retains the rest as they come."""
        while self._unretained:
            bar = self._unretained.popleft()
            await self._retain(bar)
            self._retained.append(bar)
        # No await between the loop's last check and this flip, so a bar the
        # pump takes after it is retained by the pump, never held.
        self._released = True

    async def bars(self) -> AsyncIterator[MarketDataBar]:
        """Every retained bar, in delivery order. Call after :meth:`release`."""
        while True:
            while self._retained:
                yield self._retained.popleft()
            if self._ended:
                self._raise_failure()
                return
            await self._wait()

    async def aclose(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not task.cancelled():
                raise

    async def _wait(self) -> None:
        self._changed.clear()
        await self._changed.wait()

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def _pump(self) -> None:
        try:
            async with aclosing(self._source) as source:
                async for bar in source:
                    if self._released:
                        await self._retain(bar)
                        self._retained.append(bar)
                    else:
                        self._unretained.append(bar)
                    self._changed.set()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Handed to the waiter, which raises it in the run's own task.
            self._failure = exc
        finally:
            self._ended = True
            self._changed.set()


__all__ = [
    "RETRYABLE_REASONS",
    "LiveStartBuffer",
    "StartupDeadline",
    "StreamSeam",
]
