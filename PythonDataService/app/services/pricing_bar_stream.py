"""The traded instrument's one-minute bars, for a bot that trades something else (#2912).

A bot's Signal stream decides. This second stream only prices: it keeps the
traded instrument's bars as they arrive, so a decision can be handed the
**pricing bar** -- the traded instrument's own bar that closes with the
decision bar.

The two streams deliver independently and in either order. An ENTER whose
pricing bar has not arrived waits for it, for a short fixed time and never
past the decision's own time limit; a bar that turns up later is kept and
never used for that decision. An EXIT sent to a broker never waits: it is a
market order, and takes the bar only if it is already there. An EXIT a Dry
Run or a Live Shadow fills itself does need a price: it waits the same short
time for its own minute's bar, and failing that takes the newest earlier bar
with a trade in it. With no such bar at all, that sale is refused.

Nothing that happens here ends the run, pauses the Signal Session or
quarantines a decision bar. A feed error of any kind, a minute the feed
cannot prove complete, or a stream that ends only leaves entries unpriceable
until the stream is back, and the stream subscribes again on its own: once
the feed is connected, after a pause that grows while it keeps failing.

A minute the feed cannot prove complete costs two entries, not one: the
stream that replaces it joins the next minute partway, and the feed delivers
no bar for a minute it joined partway.

Each bar is retained in the instance's own source-bar ledger, as its own
provider-and-symbol stream beside the signal's, and read back from there by
close time. That is where a Dry Run and a Live Shadow verify the bar a fill
is priced from.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from app.marketdata.feed import MarketDataBar, MarketDataFeed, MarketDataFeedError
from app.services.source_bar_ledger import RetainedSourceBar, SourceBarConflictError, SourceBarLedger

logger = logging.getLogger(__name__)

RESUBSCRIBE_DELAY_S: Final = 2.0
"""The first pause before the stream subscribes again after it ended or failed."""

RESUBSCRIBE_MAX_DELAY_S: Final = 60.0
"""The longest pause between two attempts while the stream keeps failing.

Each attempt asks the Gateway to qualify a contract and may spend one of the
connection's paced bar requests, which every bot's Signal stream also draws
on when it reconnects. A fixed short pause would spend that budget through
an outage.
"""

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


class _TwoPrices:
    """A minute the feed delivered twice with different prices: neither prices an order."""


_TWO_PRICES: Final = _TwoPrices()


@dataclass(frozen=True)
class PricingRefusal:
    """Why an ENTER has no pricing bar, in a code and in plain words."""

    reason_code: str
    why: str


class _LedgerFailure(Exception):
    """The bar ledger refused a bar for a reason that asking the feed again cannot cure."""


class PricingBarStream:
    """Keep the traded instrument's minute bars and answer for one decision minute."""

    def __init__(
        self,
        feed: MarketDataFeed,
        ledger: SourceBarLedger,
        *,
        symbol: str,
        run_id: str,
        wait_ms: int,
        resubscribe_delay_s: float = RESUBSCRIBE_DELAY_S,
        resubscribe_max_delay_s: float = RESUBSCRIBE_MAX_DELAY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._feed = feed
        self._ledger = ledger
        # The feed stamps its bars with the upper-cased symbol, and the ledger
        # is read back by that.
        self._symbol = symbol.upper()
        self._run_id = run_id
        self._wait_ms = wait_ms
        self._resubscribe_delay_s = resubscribe_delay_s
        self._resubscribe_max_delay_s = resubscribe_max_delay_s
        self._sleep = sleep
        # Minutes delivered twice with different prices: neither prices an order.
        self._conflicted: set[int] = set()
        self._arrived = asyncio.Event()
        # Why the stream stopped delivering, from the failure that ended a
        # subscription until the next bar is retained. None while it delivers.
        self._down: str | None = None
        # One outage, however many attempts it takes, is logged once as down
        # and once as back, with one stack for a failure the feed did not name
        # and one for a health probe that could not be read.
        self._outage_logged = False
        self._unnamed_failure_logged = False
        self._unreadable_health_logged = False
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
        """The bar already in hand that closes at ``close_ms``, if it is a price: one with a trade in it."""
        bar = self._retained(close_ms)
        return bar if isinstance(bar, RetainedSourceBar) and bar.volume > 0 else None

    async def bar_for_simulated_exit(self, close_ms: int) -> RetainedSourceBar | None:
        """The bar a synthesized EXIT decided at ``close_ms`` fills at: never a refusal.

        Its own minute's bar when that arrives within this stream's wait and
        has a trade in it. Otherwise the newest earlier bar that has one,
        whose own close time says how old the price is. ``None`` only when the
        traded instrument has never traded on this stream.
        """
        own = await self._arrival(close_ms, wait_ms=self._wait_ms)
        if isinstance(own, RetainedSourceBar) and own.volume > 0:
            return own
        return self._ledger.latest_for_symbol(
            self._symbol, provider=self._feed.feed_id, at_or_before_ms=close_ms - 1, traded_only=True
        )

    async def bar_for_entry(self, close_ms: int, *, time_left_ms: int) -> RetainedSourceBar | PricingRefusal:
        """The pricing bar for an ENTER decided on the bar closing at ``close_ms``, or why there is none.

        Waits for a bar not yet in hand: no longer than this stream's wait,
        and no longer than ``time_left_ms``, the time the decision itself
        stays valid.
        """
        bar = await self._arrival(close_ms, wait_ms=min(self._wait_ms, time_left_ms))
        if bar is _TWO_PRICES:
            return PricingRefusal(
                PRICING_BAR_CONFLICT,
                f"{self._symbol} was delivered with two different prices for this minute, "
                f"so neither is a price to buy at. {_NOT_RETRIED}",
            )
        if isinstance(bar, RetainedSourceBar):
            if bar.volume == 0:
                return PricingRefusal(
                    PRICING_BAR_NO_TRADE,
                    f"{self._symbol} had no trade in this minute, so there was no price to buy at. {_NOT_RETRIED}",
                )
            return bar
        not_delivering = self._not_delivering()
        if not_delivering is not None:
            return PricingRefusal(
                PRICING_FEED_UNHEALTHY,
                f"The {self._symbol} price feed is not delivering ({not_delivering}), so there was no price "
                f"to buy at. {_NOT_RETRIED}",
            )
        return PricingRefusal(
            PRICING_BAR_LATE,
            f"{self._symbol}'s price for this minute had not arrived in time. {_NOT_RETRIED}",
        )

    async def _arrival(self, close_ms: int, *, wait_ms: int) -> RetainedSourceBar | _TwoPrices | None:
        """The bar closing at ``close_ms`` once it is in hand, waiting up to ``wait_ms`` for it; None if none came."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, wait_ms) / 1000
        while True:
            bar = self._retained(close_ms)
            if bar is not None:
                return bar
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            self._arrived.clear()
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout=remaining)
            except TimeoutError:
                return None

    def _retained(self, close_ms: int) -> RetainedSourceBar | _TwoPrices | None:
        """The ledger's one bar closing at ``close_ms``; ``_TWO_PRICES`` when the minute has two."""
        if close_ms in self._conflicted:
            return _TWO_PRICES
        try:
            return self._ledger.find_by_closed_end(provider=self._feed.feed_id, symbol=self._symbol, end_ms=close_ms)
        except SourceBarConflictError:
            # Two retained bars close at this instant (they open at different
            # ones): the ledger will not choose between them.
            return _TWO_PRICES

    def _not_delivering(self) -> str | None:
        """Why the feed is not delivering this symbol's bars right now, or None when it is.

        Read when an ENTER's wait ends. A stream that failed is not delivering
        until a bar arrives again, and says what failed. One that has
        delivered since is judged by the feed's own health, so a bar that is
        merely late is not blamed on a failure already recovered from, and a
        line that stalled without raising is not called late.
        """
        if self._down is not None:
            return self._down
        try:
            health = self._feed.health(self._symbol)
        except Exception:
            # A health probe that raises proves nothing about the feed.
            logger.warning(
                "the pricing stream's feed health could not be read",
                exc_info=True,
                extra={"action": "pricing_stream_health_unreadable", "symbol": self._symbol, "run_id": self._run_id},
            )
            return "its health could not be read"
        if not health.connected:
            return health.reason or "the feed is disconnected"
        if health.stale:
            return health.reason or "the feed has stalled"
        return None

    async def _consume(self) -> None:
        delay = self._resubscribe_delay_s
        while True:
            try:
                async for bar in self._feed.stream_bars(self._symbol, use_rth=False):
                    self._retain(bar)
                    delay = self._resubscribe_delay_s
                why = "the stream ended"
            except _LedgerFailure:
                raise
            except MarketDataFeedError as exc:
                why = exc.reason or str(exc)
            except Exception as exc:
                # Any feed failure is "down, subscribe again", never the run's.
                # A socket that drops while a subscription is being set up
                # raises the transport's own error, not the feed's. The Signal
                # stream rides such a drop out; so must this one.
                why = f"{type(exc).__name__}: {exc}"
                if not self._unnamed_failure_logged:
                    self._unnamed_failure_logged = True
                    logger.warning(
                        "the pricing stream failed on an error the feed did not name",
                        exc_info=True,
                        extra={
                            "action": "pricing_stream_unnamed_failure",
                            "symbol": self._symbol,
                            "run_id": self._run_id,
                        },
                    )
            if not self._outage_logged:
                self._outage_logged = True
                logger.warning(
                    "the pricing stream stopped delivering; entries are unpriceable until it is back",
                    extra={"action": "pricing_stream_down", "symbol": self._symbol, "run_id": self._run_id, "why": why},
                )
            self._down = why
            self._arrived.set()
            await self._sleep(delay)
            while not self._connected():
                await self._sleep(delay)
            delay = min(delay * 2, self._resubscribe_max_delay_s)

    def _connected(self) -> bool:
        """Whether the feed's connection is up, so that subscribing again can succeed."""
        try:
            return self._feed.health().connected
        except Exception:
            # A health probe that raises proves nothing about the feed, so the
            # stream keeps waiting. Said once per outage: this is polled.
            if not self._unreadable_health_logged:
                self._unreadable_health_logged = True
                logger.warning(
                    "the pricing stream cannot read its feed's health; it will not subscribe again until it can",
                    exc_info=True,
                    extra={
                        "action": "pricing_stream_connection_unreadable",
                        "symbol": self._symbol,
                        "run_id": self._run_id,
                    },
                )
            return False

    def _retain(self, bar: MarketDataBar) -> None:
        try:
            self._ledger.append(bar, run_id=self._run_id)
        except SourceBarConflictError as exc:
            # The ledger refuses a second, different bar for a close time it
            # holds, and a minute older than its newest. Neither is retained.
            # Only the first leaves two prices for one minute, so only it marks
            # the minute: the other is a minute with no bar at all.
            if self._retained(bar.end_ms) is not None:
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
        except Exception as exc:
            raise _LedgerFailure(str(exc)) from exc
        self._down = None
        self._arrived.set()
        self._unnamed_failure_logged = False
        self._unreadable_health_logged = False
        if self._outage_logged:
            self._outage_logged = False
            logger.info(
                "the pricing stream is delivering again",
                extra={"action": "pricing_stream_back", "symbol": self._symbol, "run_id": self._run_id},
            )

    def _ended(self, task: asyncio.Task[None]) -> None:
        """A consumer the ledger stopped leaves the stream down for the run, loudly."""
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._down = "its bars can no longer be retained"
        self._arrived.set()
        logger.error(
            "the pricing stream stopped: its bars can no longer be retained; entries stay unpriceable for this run",
            extra={"action": "pricing_stream_failed", "symbol": self._symbol, "run_id": self._run_id},
            exc_info=(type(error), error, error.__traceback__),
        )


__all__ = [
    "PRICING_BAR_CONFLICT",
    "PRICING_BAR_LATE",
    "PRICING_BAR_NO_TRADE",
    "PRICING_FEED_UNHEALTHY",
    "PRICING_REFUSAL_REASON_CODES",
    "PricingBarStream",
    "PricingRefusal",
]
