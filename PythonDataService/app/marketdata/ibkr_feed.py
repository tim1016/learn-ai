"""IBKR-backed implementation of the broker-neutral MarketDataFeed port.

This module backs ``MarketDataFeed`` with the existing, proven IBKR bar path
(``app/broker/ibkr/bars.stream_minute_bars``).  It is the **only** file in
``app/marketdata/`` that imports IBKR types; all other consumers depend only on
the neutral port in ``feed.py``.

Architecture (phase-3 design §4 + #1258 L2 "one shared feed, in-process fan-out"):

* One ``IbkrMarketDataFeed`` instance lives in the data plane for the lifetime
  of the process.  All bots and consumers in the same container call
  ``stream_bars()`` on this shared instance.
* Fan-out is reference-counted per symbol via the existing
  ``_RealtimeBarSubscriptionRegistry`` in ``bars.py``.  N concurrent callers of
  ``stream_bars("SPY")`` share one ``reqRealTimeBars`` subscription; each gets
  every closed minute bar; the last caller's ``async for`` exit releases the
  underlying IBKR subscription.
* ``IBKRBarStreamError`` is caught at the boundary and re-raised as
  ``MarketDataFeedError`` so no IBKR type escapes the port.
* ``health()`` reads the IBKR client's connection signals synchronously.  A
  stale feed is detected by comparing the last-bar wall-clock against a
  configurable threshold (default: 3 minutes, chosen to tolerate normal
  pre-market and intra-day bar gaps without crying wolf, while surfacing a
  truly dead feed within a reasonable window).  Bar gaps within the threshold
  are non-fatal.
* ``IbkrMinuteBar → MarketDataBar`` translation happens here, at the boundary.
  The translated bar carries ``feed_id="ibkr"`` as provenance.

Temporal contract: all ``int64 ms UTC``; no ISO strings, no naive datetimes.
Temporal ban-list clean: no utcnow, no tz-naive now(), no pd.to_datetime
without utc.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from app.broker.ibkr.bars import IBKRBarStreamError, stream_minute_bars
from app.broker.ibkr.client import IbkrClient, NotConnectedError
from app.broker.ibkr.models import IbkrMinuteBar
from app.marketdata.feed import FeedHealth, MarketDataBar, MarketDataFeedError
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_MS: int = 3 * 60 * 1_000  # 3 minutes in ms


class IbkrMarketDataFeed:
    """IBKR-backed MarketDataFeed.

    Satisfies the ``MarketDataFeed`` Protocol structurally.

    Parameters
    ----------
    client:
        The data-plane's existing in-container ``IbkrClient``.  This feed
        uses it *read-only* — no order submission, no account queries.
    stale_threshold_ms:
        How long without a bar before ``health()`` reports ``stale=True``.
        Default is 3 minutes (180 000 ms).  Bar gaps shorter than this are
        not reported as stale.
    """

    feed_id: str = "ibkr"

    def __init__(
        self,
        client: IbkrClient,
        *,
        stale_threshold_ms: int = _STALE_THRESHOLD_MS,
    ) -> None:
        self._client = client
        self._stale_threshold_ms = stale_threshold_ms
        # Watermarks updated by every active stream_bars coroutine.
        self._last_bar_ms: int | None = None
        self._last_bar_wall_ms: int | None = None  # wall-clock when last bar arrived
        # Count of currently active symbol subscriptions (one per active stream_bars call).
        self._active_count: int = 0

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncGenerator[MarketDataBar, None]:
        """Yield closed 1-minute bars for ``symbol``.

        Reference-counted: N concurrent callers of the same symbol share one
        underlying ``reqRealTimeBars`` subscription.  The last caller's exit
        releases it.

        Raises ``MarketDataFeedError`` when the IBKR connection dies.  Bar gaps
        are non-fatal: the iterator resumes when the next bar arrives.
        """
        self._active_count += 1
        logger.info(
            "MarketDataFeed consumer attached",
            extra={
                "action": "marketdata_consumer_attached",
                "feed_id": self.feed_id,
                "symbol": symbol,
                "use_rth": use_rth,
                "active_count": self._active_count,
            },
        )
        try:
            async for ibkr_bar in stream_minute_bars(self._client, symbol, use_rth=use_rth):
                bar = self._translate(ibkr_bar)
                self._last_bar_ms = bar.start_ms
                self._last_bar_wall_ms = now_ms_utc()
                yield bar
        except (IBKRBarStreamError, NotConnectedError) as exc:
            raise MarketDataFeedError(str(exc)) from exc
        finally:
            self._active_count = max(0, self._active_count - 1)
            logger.info(
                "MarketDataFeed consumer detached",
                extra={
                    "action": "marketdata_consumer_detached",
                    "feed_id": self.feed_id,
                    "symbol": symbol,
                    "active_count": self._active_count,
                },
            )

    def health(self) -> FeedHealth:
        """Return a synchronous point-in-time health snapshot."""
        connected = self._client.is_connected() and not self._client.connection_lost
        now = now_ms_utc()
        stale = False
        reason = ""

        if not connected:
            reason = "IBKR connection lost"
        elif self._last_bar_wall_ms is not None:
            age_ms = now - self._last_bar_wall_ms
            if age_ms > self._stale_threshold_ms:
                stale = True
                reason = (
                    f"No bar in {age_ms // 1000}s "
                    f"(threshold {self._stale_threshold_ms // 1000}s)"
                )

        return FeedHealth(
            connected=connected,
            stale=stale,
            last_bar_ms=self._last_bar_ms,
            reason=reason,
            active_subscription_count=self._active_count,
            observed_at_ms=now,
        )

    @staticmethod
    def _translate(ibkr_bar: IbkrMinuteBar) -> MarketDataBar:
        """Map an IbkrMinuteBar to the neutral MarketDataBar at the boundary."""
        return MarketDataBar(
            symbol=ibkr_bar.symbol,
            start_ms=ibkr_bar.start_ms,
            end_ms=ibkr_bar.end_ms,
            open=ibkr_bar.open,
            high=ibkr_bar.high,
            low=ibkr_bar.low,
            close=ibkr_bar.close,
            volume=ibkr_bar.volume,
            fetched_at_ms=ibkr_bar.fetched_at_ms,
            feed_id="ibkr",
            session_phase=ibkr_bar.session_phase,
        )


# ---------------------------------------------------------------------------
# Process-level singleton — installed at startup in main.py.
# ---------------------------------------------------------------------------

_FEED: IbkrMarketDataFeed | None = None


def get_market_data_feed() -> IbkrMarketDataFeed | None:
    """Return the process-level shared feed, or ``None`` when not installed."""
    return _FEED


def set_market_data_feed(feed: IbkrMarketDataFeed | None) -> None:
    """Install (or clear) the process-level shared feed.

    Called once at startup in ``main.py`` after the IBKR client is
    connected, and once at shutdown.
    """
    global _FEED
    _FEED = feed
