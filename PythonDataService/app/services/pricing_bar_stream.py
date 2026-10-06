"""The traded instrument's one-minute bars, for a bot that trades something else (#2912).

A bot's Signal stream decides. This second stream only prices: it keeps the
traded instrument's bars as they arrive, keyed by close time, so a decision
can be handed the **pricing bar** -- the traded instrument's own bar that
closes with the decision bar.

The two streams deliver independently and in either order. An ENTER whose
pricing bar has not arrived waits for it, for a short fixed time and never
past the decision's own time limit; a bar that turns up later is kept and
never used for that decision. An EXIT never waits: it takes the bar if it is
already there.

Nothing that happens here ends the run, pauses the Signal Session or
quarantines a decision bar. A feed error, a minute the feed cannot prove
complete, or a stream that ends only leaves entries unpriceable until the
stream is back, and the stream subscribes again on its own.

Each bar is retained in the instance's own source-bar ledger, as its own
provider-and-symbol stream beside the signal's. That is where a Dry Run and a
Live Shadow verify the bar a fill is priced from.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

from app.marketdata.feed import MarketDataBar, MarketDataFeed, MarketDataFeedError
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarConflictError, SourceBarLedger

logger = logging.getLogger(__name__)

PRICING_BAR_WAIT_MS: Final = 5_000
"""How long an ENTER waits for its pricing bar once its decision bar is in hand.

A starting value (#2912): it should be set from how late the second stream's
minute actually arrives. The caller also caps the wait at the decision's own
time limit.
"""

RESUBSCRIBE_DELAY_S: Final = 2.0
"""The pause before the stream subscribes again after it ended or failed."""

# Minutes kept in memory. A decision is priced within seconds of its bar, so
# this only bounds a long run's memory; the ledger keeps every bar.
_KEPT_MINUTES: Final = 240

PRICING_BAR_NO_TRADE: Final = "PRICING_BAR_NO_TRADE"
PRICING_BAR_LATE: Final = "PRICING_BAR_LATE"
PRICING_BAR_CONFLICT: Final = "PRICING_BAR_CONFLICT"
PRICING_FEED_UNHEALTHY: Final = "PRICING_FEED_UNHEALTHY"

PRICING_REFUSAL_REASON_CODES: Final = frozenset(
    {PRICING_BAR_NO_TRADE, PRICING_BAR_LATE, PRICING_BAR_CONFLICT, PRICING_FEED_UNHEALTHY}
)
"""Every reason this stream refuses an ENTER for. A quiet minute, a slow feed
and a broken feed are three different facts and stay three codes."""

_NOT_RETRIED: Final = "No order was sent, and the entry is not kept for a later bar."


@dataclass(frozen=True)
class PricingRefusal:
    """Why an ENTER has no pricing bar, in a code and in plain words."""

    reason_code: str
    why: str


class PricingBarStream:
    """Keep the traded instrument's minute bars and answer for one decision minute."""

    def __init__(
        self,
        feed: MarketDataFeed,
        ledger: SourceBarLedger,
        *,
        symbol: str,
        run_id: str,
        wait_ms: int = PRICING_BAR_WAIT_MS,
        resubscribe_delay_s: float = RESUBSCRIBE_DELAY_S,
    ) -> None:
        self._feed = feed
        self._ledger = ledger
        self._symbol = symbol
        self._run_id = run_id
        self._wait_ms = wait_ms
        self._resubscribe_delay_s = resubscribe_delay_s
        self._bars: dict[int, RetainedSourceBar] = {}
        self._conflicted: set[int] = set()
        self._arrived = asyncio.Event()
        # Why the stream is not delivering, or None while it is.
        self._down: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def symbol(self) -> str:
        return self._symbol

    async def __aenter__(self) -> PricingBarStream:
        self._task = asyncio.create_task(self._consume(), name=f"pricing-bars:{self._symbol}:{self._run_id}")
        self._task.add_done_callback(self._ended)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def bar_closing_at(self, close_ms: int) -> RetainedSourceBar | None:
        """The bar already in hand that closes at ``close_ms``, if it can price an order."""
        if close_ms in self._conflicted:
            return None
        return self._bars.get(close_ms)

    async def bar_for_entry(self, close_ms: int, *, time_left_ms: int) -> RetainedSourceBar | PricingRefusal:
        """The pricing bar for an ENTER decided on the bar closing at ``close_ms``, or why there is none.

        Waits for a bar not yet in hand: no longer than this stream's wait,
        and no longer than ``time_left_ms``, the time the decision itself
        stays valid.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, min(self._wait_ms, time_left_ms)) / 1000
        while True:
            if close_ms in self._conflicted:
                return PricingRefusal(
                    PRICING_BAR_CONFLICT,
                    f"{self._symbol} was delivered with two different prices for this minute, "
                    f"so neither is a price to buy at. {_NOT_RETRIED}",
                )
            bar = self._bars.get(close_ms)
            if bar is not None:
                if bar.volume == 0:
                    return PricingRefusal(
                        PRICING_BAR_NO_TRADE,
                        f"{self._symbol} had no trade in this minute, so there was no price to buy at. "
                        f"{_NOT_RETRIED}",
                    )
                return bar
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout=remaining)
            except TimeoutError:
                break
        if self._down is not None:
            return PricingRefusal(
                PRICING_FEED_UNHEALTHY,
                f"The {self._symbol} price feed is not delivering ({self._down}), so there was no price "
                f"to buy at. {_NOT_RETRIED}",
            )
        return PricingRefusal(
            PRICING_BAR_LATE,
            f"{self._symbol}'s price for this minute had not arrived in time. {_NOT_RETRIED}",
        )

    async def _consume(self) -> None:
        while True:
            try:
                async for bar in self._feed.stream_bars(self._symbol, use_rth=False):
                    self._retain(bar)
                why = "the stream ended"
            except MarketDataFeedError as exc:
                why = exc.reason or str(exc)
            self._down = why
            self._arrived.set()
            logger.warning(
                "the pricing stream stopped delivering; entries are unpriceable until it is back",
                extra={
                    "action": "pricing_stream_down",
                    "symbol": self._symbol,
                    "run_id": self._run_id,
                    "why": why,
                },
            )
            await asyncio.sleep(self._resubscribe_delay_s)

    def _retain(self, bar: MarketDataBar) -> None:
        try:
            retained = self._ledger.append(bar, run_id=self._run_id)
        except SourceBarConflictError as exc:
            # The ledger refuses a second, different bar for a minute it holds,
            # and a minute older than its newest. Neither is retained. Only the
            # first leaves two prices for one minute, so only it marks the
            # minute: the other is a minute with no bar at all.
            if self._ledger.find_by_market_bar(bar) is not None:
                self._bars.pop(bar.end_ms, None)
                self._conflicted.add(bar.end_ms)
                self._arrived.set()
            logger.warning(
                "a pricing bar was not retained; it prices no order",
                extra={
                    "action": "pricing_bar_not_retained",
                    "symbol": self._symbol,
                    "run_id": self._run_id,
                    "bar_end_ms": bar.end_ms,
                    "why": str(exc),
                },
            )
            return
        self._down = None
        self._bars[retained.end_ms] = retained
        for old in sorted(self._bars)[:-_KEPT_MINUTES]:
            del self._bars[old]
            self._conflicted.discard(old)
        self._arrived.set()

    def _ended(self, task: asyncio.Task[None]) -> None:
        """A consumer that died of an unexpected error leaves the stream down, loudly."""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._down = "the stream stopped on an unexpected error"
        self._arrived.set()
        logger.error(
            "the pricing stream stopped on an unexpected error; entries stay unpriceable for this run",
            extra={"action": "pricing_stream_failed", "symbol": self._symbol, "run_id": self._run_id},
            exc_info=(type(error), error, error.__traceback__),
        )


__all__ = [
    "PRICING_BAR_CONFLICT",
    "PRICING_BAR_LATE",
    "PRICING_BAR_NO_TRADE",
    "PRICING_BAR_WAIT_MS",
    "PRICING_FEED_UNHEALTHY",
    "PRICING_REFUSAL_REASON_CODES",
    "PricingBarStream",
    "PricingRefusal",
]
