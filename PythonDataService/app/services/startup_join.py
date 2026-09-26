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
    WARMUP_REFUSAL_REASONS,
    FeedContinuityEvent,
    MarketDataBar,
    MarketDataFeed,
    MarketDataFeedError,
    WarmupMinutesMissing,
)
from app.services.decision_session import RunDecisionSession
from app.services.retained_tail_join import (
    join_fresh_warmup,
    join_retained_tail,
    record_refused_resume_join,
    retain_resume_join,
)
from app.services.source_bar_ledger import SourceBarLedger
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

    async def run(
        self,
        attempt: Callable[[], Awaitable[T]],
        *,
        symbol: str,
        abort: Callable[[], None] = lambda: None,
    ) -> T:
        """Run ``attempt`` after the settle time, retrying until the deadline.

        A retryable refusal (history not there yet) is asked again every
        ``_RETRY_MS``; any other refusal propagates at once. An attempt still in
        flight at the deadline is cancelled rather than allowed to extend it.
        The refusal that ends the budget is the last attempt's, so the run's
        record names what was still missing: when the budget runs out between
        attempts — a retry sleep that wakes past the deadline — the next
        ``wait_for`` times out before its attempt can run, and the last
        *answering* attempt's refusal is re-raised in place of a synthetic
        timeout one that names no interval (#2486). Only a first attempt that
        never answered leaves nothing to name, and the timeout refusal stands.
        ``abort`` raises when the join is moot -- the live stream it would
        meet has died -- and is asked before every attempt and after every
        failure, so that failure is the run's outcome, not a history refusal
        waited out to the deadline.
        """
        await _sleep_until(self.not_before_ms)
        attempts = 0
        last_answer: MarketDataFeedError | None = None
        while True:
            abort()
            attempts += 1
            remaining_ms = self.deadline_ms - now_ms_utc()
            try:
                return await asyncio.wait_for(attempt(), timeout=max(remaining_ms, 0) / 1000)
            except TimeoutError as exc:
                if last_answer is not None:
                    raise last_answer from exc
                raise MarketDataFeedError(
                    f"warmup history for {symbol} was still being fetched at the startup-join deadline",
                    reason=WARMUP_HISTORY_UNAVAILABLE,
                ) from exc
            except MarketDataFeedError as exc:
                abort()
                last_answer = exc
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

    Once released, the pump also asks the stream for the next bar only when
    the run asks for one, so exactly one bar is in flight -- the same pull the
    run's live loop had before it held anything. Reading ahead would tell the
    feed a bar was consumed while the strategy had not yet decided on it.
    """

    def __init__(
        self,
        bars: AsyncIterator[MarketDataBar],
        *,
        symbol: str,
        retain: Callable[[MarketDataBar, int | None], Awaitable[None]],
    ) -> None:
        self.symbol = symbol
        self._source = bars
        self._retain = retain
        # Held bars keep the instant the stream delivered them, which is what
        # their delivery admission is judged against.
        self._unretained: deque[tuple[MarketDataBar, int]] = deque()
        # Each retained bar says whether it was held through preparation.
        self._retained: deque[tuple[MarketDataBar, bool]] = deque()
        self._released = False
        self._demand = asyncio.Event()
        self._changed = asyncio.Event()
        self._joined_minute_start_ms: int | None = None
        self._failure: BaseException | None = None
        self._ended = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._pump(), name="live-start-buffer")

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
                return StreamSeam(live_from_ms=self._unretained[0][0].start_ms)
            if self._ended:
                self.raise_if_failed()
                raise MarketDataFeedError("the live stream ended before it said where it takes over")
            await self._wait()

    @property
    def released(self) -> bool:
        """Whether the run has taken this stream; it is read once."""
        return self._released

    async def release(self) -> None:
        """Retain every held bar in delivery order; the pump retains the rest as they come."""
        if self._released:
            raise RuntimeError("a run's live stream is released once")
        # A stream that died while the run prepared is the run's outcome; it
        # must not be released, stamped ready, and only then fail.
        self.raise_if_failed()
        while self._unretained:
            bar, delivered_at_ms = self._unretained.popleft()
            await self._retain(bar, delivered_at_ms)
            self._retained.append((bar, True))
        # No await between the loop's last check and this flip, so a bar the
        # pump takes after it is retained by the pump, never held.
        self._released = True

    async def bars(self) -> AsyncIterator[tuple[MarketDataBar, bool]]:
        """Every retained bar in delivery order, and whether it was held. Call after :meth:`release`."""
        while True:
            while self._retained:
                yield self._retained.popleft()
            if self._ended:
                self.raise_if_failed()
                return
            self._changed.clear()
            self._demand.set()
            await self._changed.wait()

    async def aclose(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # The pump's own cancellation is the point; the caller's -- a Stop
            # landing while the stream shuts down -- must still propagate.
            current = asyncio.current_task()
            if not task.cancelled() or (current is not None and current.cancelling()):
                raise

    async def _wait(self) -> None:
        self._changed.clear()
        await self._changed.wait()

    def raise_if_failed(self) -> None:
        """Raise the stream's own failure, if it has failed."""
        if self._failure is not None:
            raise self._failure

    async def _pump(self) -> None:
        try:
            async with aclosing(self._source) as source:
                while True:
                    if self._released:
                        await self._demand.wait()
                        self._demand.clear()
                    try:
                        bar = await anext(source)
                    except StopAsyncIteration:
                        break
                    if self._released:
                        # Retained as it is delivered: admitted against now.
                        await self._retain(bar, None)
                        self._retained.append((bar, False))
                        # This bar answers the run's outstanding ask, even one
                        # made while it was already in flight.
                        self._demand.clear()
                    else:
                        self._unretained.append((bar, now_ms_utc()))
                    self._changed.set()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            # Handed to the waiter, which raises it in the run's own task.
            self._failure = exc
        finally:
            self._ended = True
            self._changed.set()


async def warm_through_seam(
    live: LiveStartBuffer,
    source: MarketDataFeed,
    ledger: SourceBarLedger,
    *,
    run_id: str,
    session: RunDecisionSession,
    symbol: str,
    lookback_days: int,
) -> list[MarketDataBar]:
    """Wait for the stream to join, then warm exactly through its seam; every step is stamped.

    A run with retained bars (a resume) joins them to the seam (#2314); one
    without warms on the sealed lookback. Either join is retried under the
    run's one deadline, and only its final answer is recorded -- history not
    published yet is not the run's verdict. A refusal is stamped with the
    interval history did not return, when it can say, before it is raised.
    Returns every warmup bar, unfiltered; the caller applies its session.
    """
    seam = await live.seam()
    deadline = StartupDeadline.for_seam(seam, known_at_ms=now_ms_utc())
    ledger.record_startup_seam(
        run_id=run_id,
        live_from_ms=seam.live_from_ms,
        joined_minute_start_ms=seam.joined_minute_start_ms,
        deadline_ms=deadline.deadline_ms,
    )
    retained = ledger.bars(provider=source.feed_id, symbol=symbol)
    try:
        if retained:
            join = await deadline.run(
                lambda: join_retained_tail(
                    source,
                    symbol=symbol,
                    session=session,
                    retained_end_ms=retained[-1].end_ms,
                    now_ms=seam.live_from_ms,
                    lookback_days=lookback_days,
                    joined_minute_start_ms=seam.joined_minute_start_ms,
                ),
                symbol=symbol,
                abort=live.raise_if_failed,
            )
            rows = retain_resume_join(ledger, run_id=run_id, retained=retained, join=join)
            warmup = [row.to_market_bar() for row in rows]
        else:
            warmup = await deadline.run(
                lambda: join_fresh_warmup(
                    source,
                    symbol=symbol,
                    session=session,
                    live_from_ms=seam.live_from_ms,
                    joined_minute_start_ms=seam.joined_minute_start_ms,
                    lookback_days=lookback_days,
                ),
                symbol=symbol,
                abort=live.raise_if_failed,
            )
            for bar in warmup:
                ledger.append_history(bar, run_id=run_id)
    except MarketDataFeedError as exc:
        if exc.reason in WARMUP_REFUSAL_REASONS:
            if retained:
                record_refused_resume_join(
                    ledger,
                    run_id=run_id,
                    retained_end_ms=retained[-1].end_ms,
                    joined_at_ms=seam.live_from_ms,
                    reason_code=exc.reason,
                )
            missing = exc if isinstance(exc, WarmupMinutesMissing) else None
            ledger.mark_startup_refused(
                run_id=run_id,
                at_ms=now_ms_utc(),
                reason_code=exc.reason,
                missing_start_ms=None if missing is None else missing.missing_start_ms,
                missing_end_ms=None if missing is None else missing.missing_end_ms,
            )
        raise
    ledger.mark_startup_history_joined(run_id=run_id, at_ms=now_ms_utc())
    return warmup


__all__ = [
    "RETRYABLE_REASONS",
    "LiveStartBuffer",
    "StartupDeadline",
    "StreamSeam",
    "warm_through_seam",
]
