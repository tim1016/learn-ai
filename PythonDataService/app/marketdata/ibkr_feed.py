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
* A connected but stalled ``reqRealTimeBars`` line is invalidated and
  transparently replaced. Other ``IBKRBarStreamError`` failures are re-raised
  as ``MarketDataFeedError`` so no IBKR type escapes the port.
* ``health()`` reads the IBKR client's connection signals synchronously.  A
  newly active feed fails closed until its first closed minute. Thereafter,
  raw 5-second source activity must advance within a configurable threshold
  (default: 30 seconds). Ordinary bar gaps remain non-fatal.
* ``IbkrMinuteBar → MarketDataBar`` translation happens here, at the boundary.
  The translated bar carries ``feed_id="ibkr"`` as provenance.

Temporal contract: all ``int64 ms UTC``; no ISO strings, no naive datetimes.
Temporal ban-list clean: no utcnow, no tz-naive now(), no pd.to_datetime
without utc.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from app.broker.ibkr.bars import (
    IBKRBarStreamError,
    IBKRBarSubscriptionStalled,
    fetch_historical_minute_bars,
    stream_minute_bars,
)
from app.broker.ibkr.client import IbkrClient, NotConnectedError
from app.broker.ibkr.models import IbkrMinuteBar
from app.marketdata.feed import FeedHealth, MarketDataBar, MarketDataFeedError
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_MS: int = 30_000


@dataclass
class _SymbolLiveness:
    """Mutable ingestion watermarks owned by one normalized symbol."""

    last_bar_ms: int | None = None
    last_bar_wall_ms: int | None = None
    last_source_ms: int | None = None
    last_source_wall_ms: int | None = None
    first_bar_seen: bool = False
    active_count: int = 0


class IbkrMarketDataFeed:
    """IBKR-backed MarketDataFeed.

    Satisfies the ``MarketDataFeed`` Protocol structurally.

    Parameters
    ----------
    client:
        The data-plane's existing in-container ``IbkrClient``.  This feed
        uses it *read-only* — no order submission, no account queries.
    stale_threshold_ms:
        How long without raw source activity before ``health()`` reports
        ``stale=True``. Default is 30 seconds. A newly active subscription is
        stale until it emits its first closed minute.
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
        self._symbol_liveness: dict[str, _SymbolLiveness] = {}

    @property
    def capability_account_id(self) -> str | None:
        """Expose the IBKR account that owns this feed's session evidence."""
        return self._client.connected_account

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

        Raises ``MarketDataFeedError`` when the IBKR connection dies or the
        source violates a data invariant. A stalled request is replaced
        transparently. Closed-minute output gaps are non-fatal; source-heartbeat
        silence is evaluated against IBKR's documented one-bar-per-five-seconds
        ``reqRealTimeBars`` contract.
        """
        normalized_symbol = symbol.upper()
        state = self._state_for(normalized_symbol)
        if state.active_count == 0:
            state.first_bar_seen = False
        state.active_count += 1
        logger.info(
            "MarketDataFeed consumer attached",
            extra={
                "action": "marketdata_consumer_attached",
                "feed_id": self.feed_id,
                "symbol": normalized_symbol,
                "use_rth": use_rth,
                "active_count": state.active_count,
            },
        )
        try:
            replacements = 0
            while True:
                try:
                    async for ibkr_bar in stream_minute_bars(
                        self._client,
                        normalized_symbol,
                        use_rth=use_rth,
                        on_source_bar=lambda source_ms: self._observe_source_bar(
                            normalized_symbol,
                            source_ms,
                        ),
                    ):
                        bar = self._translate(ibkr_bar)
                        state.last_bar_ms = bar.start_ms
                        state.last_bar_wall_ms = now_ms_utc()
                        state.first_bar_seen = True
                        yield bar
                    break
                except IBKRBarSubscriptionStalled as exc:
                    state.first_bar_seen = False
                    replacements += 1
                    logger.warning(
                        "Replacing stalled IBKR real-time-bar subscription",
                        extra={
                            "action": "marketdata_stalled_subscription_replaced",
                            "feed_id": self.feed_id,
                            "symbol": normalized_symbol,
                            "use_rth": use_rth,
                            "replacement_count": replacements,
                            "reason": str(exc),
                        },
                    )
        except (IBKRBarStreamError, NotConnectedError) as exc:
            raise MarketDataFeedError(str(exc)) from exc
        finally:
            state.active_count = max(0, state.active_count - 1)
            if state.active_count == 0:
                state.first_bar_seen = False
            logger.info(
                "MarketDataFeed consumer detached",
                extra={
                    "action": "marketdata_consumer_detached",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "active_count": state.active_count,
                },
            )

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        """Return closed 1-minute bars from the trailing ``lookback_days``
        calendar days, oldest first, via IBKR's read-only historical-data
        endpoint.

        Used only to warm up a strategy's indicator state before live
        decisions begin (a fresh RTH session alone can't warm
        ADX/EMA-class indicators with multi-day lookback periods) -- never
        itself treated as a decision. A fetch failure is non-fatal:
        callers fall back to a cold start, matching pre-warmup behavior.
        """
        normalized_symbol = symbol.upper()
        try:
            historical = await fetch_historical_minute_bars(
                self._client,
                normalized_symbol,
                duration=f"{lookback_days} D",
                use_rth=use_rth,
            )
        except (IBKRBarStreamError, NotConnectedError) as exc:
            logger.warning(
                "Historical warmup bars unavailable; strategy will cold-start",
                extra={
                    "action": "warmup_bars_unavailable",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "error": str(exc),
                },
            )
            return []
        bars = [self._translate(bar) for bar in historical]
        # IBKR's historical endpoint includes the still-forming minute as its
        # last row. A forming bar is not a closed observation: sealing it into
        # the source-bar ledger makes the later live close of the same window
        # a SOURCE_BAR_IDENTITY_CONFLICT (fleet run 2026-08-25 — every bot
        # deployed mid-minute crashed on its first live bar).
        now_ms = now_ms_utc()
        closed = [bar for bar in bars if bar.end_ms <= now_ms]
        if len(closed) < len(bars):
            logger.info(
                "Dropped forming bar(s) from historical warmup",
                extra={
                    "action": "warmup_forming_bars_dropped",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "dropped": len(bars) - len(closed),
                },
            )
        return closed

    def health(self, symbol: str | None = None) -> FeedHealth:
        """Return aggregate or symbol-scoped point-in-time feed health."""
        connected = self._client.is_connected() and not self._client.connection_lost
        now = now_ms_utc()
        stale = False
        reason = ""
        normalized_symbol = symbol.upper() if symbol is not None else None
        states = (
            [(normalized_symbol, self._symbol_liveness.get(normalized_symbol))]
            if normalized_symbol is not None
            else list(self._symbol_liveness.items())
        )
        present_states = [(name, state) for name, state in states if state is not None]
        active_states = [
            (name, state)
            for name, state in present_states
            if state.active_count > 0
        ]
        active_count = sum(state.active_count for _, state in present_states)
        last_bar_ms = max(
            (state.last_bar_ms for _, state in present_states if state.last_bar_ms is not None),
            default=None,
        )

        if not connected:
            reason = "IBKR connection lost"
        elif missing_first := [name for name, state in active_states if not state.first_bar_seen]:
            stale = True
            reason = (
                f"Active IBKR feed for {', '.join(missing_first)} has not produced "
                "its first closed bar"
            )
        else:
            stale_ages = [
                (name, now - freshness_wall_ms)
                for name, state in active_states
                if (freshness_wall_ms := state.last_source_wall_ms or state.last_bar_wall_ms)
                is not None
                and now - freshness_wall_ms >= self._stale_threshold_ms
            ]
            if stale_ages:
                stale = True
                stale_names = ", ".join(name for name, _ in stale_ages)
                oldest_age_ms = max(age_ms for _, age_ms in stale_ages)
                reason = (
                    f"No source bar for {stale_names} in {oldest_age_ms // 1000}s "
                    f"(threshold {self._stale_threshold_ms // 1000}s)"
                )

        return FeedHealth(
            connected=connected,
            stale=stale,
            last_bar_ms=last_bar_ms,
            reason=reason,
            active_subscription_count=active_count,
            observed_at_ms=now,
        )

    def _state_for(self, symbol: str) -> _SymbolLiveness:
        """Return the one mutable liveness record for ``symbol``."""
        return self._symbol_liveness.setdefault(symbol.upper(), _SymbolLiveness())

    def _observe_source_bar(self, symbol: str, source_ms: int) -> None:
        """Advance the raw-source liveness watermark at the ingestion edge."""
        state = self._state_for(symbol)
        state.last_source_ms = source_ms
        state.last_source_wall_ms = now_ms_utc()

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
