"""Broker-neutral market-data port: MarketDataFeed, MarketDataBar, FeedHealth.

Design constraints (from ADR 0022 + phase-3 design §4 + #1258 L2):

* No IBKR types escape this module.  The IBKR implementation (ibkr_feed.py)
  translates ``IbkrMinuteBar`` and ``IBKRBarStreamError`` at the boundary.
* All temporal fields are ``int64 ms UTC`` — no ISO strings, no naive datetimes.
* ``MarketDataFeed`` is a structural Protocol: any class with the right
  signature satisfies it without inheriting from it.
* Fan-out is reference-counted: N consumers of the same symbol share one
  underlying broker subscription; the last unsubscribe releases it.
* Ordinary bar gaps are non-fatal. A bounded stalled broker request is
  replaced internally; connection death is fatal and typed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class MarketDataFeedError(Exception):
    """Fatal feed error: connection death or unrecoverable broker failure.

    Raised by ``MarketDataFeed.stream_bars`` when the underlying connection
    dies or violates an unrecoverable data invariant. Ordinary bar gaps
    (expected during pre-market or after-hours silence) are not errors.
    """


BarSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]


class MarketDataBar(BaseModel):
    """Broker-neutral closed 1-minute bar.

    All temporal values are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
    ``start_ms`` is the bar-open boundary (inclusive); ``end_ms`` is bar-close
    (exclusive), i.e. ``end_ms = start_ms + 60_000``.

    OHLC prices use ``Decimal`` for exactness — no silent float rounding at the
    port boundary.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    start_ms: int = Field(..., description="Bar-open boundary, int64 ms UTC, inclusive.")
    end_ms: int = Field(..., description="Bar-close boundary, int64 ms UTC, exclusive.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    fetched_at_ms: int = Field(..., description="Wall-clock at which the bar was assembled, int64 ms UTC.")
    feed_id: str = Field(..., description="Provenance tag, e.g. 'ibkr'. NOT an execution identity.")
    session_phase: BarSessionPhase = "UNKNOWN"


class FeedHealth(BaseModel):
    """Point-in-time health snapshot for a MarketDataFeed.

    ``connected`` — the underlying broker connection is alive.
    ``stale``     — connected but required source liveness has not been proven
                    within the implementation's stale threshold.
    ``last_bar_ms`` — ``start_ms`` of the most recently emitted bar, or ``None``
                      if no bar has been emitted yet.
    ``reason``    — human-readable detail when unhealthy (empty string when healthy).
    ``active_subscription_count`` — number of symbols currently subscribed.
    ``observed_at_ms`` — wall-clock of this snapshot, int64 ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    connected: bool
    stale: bool
    last_bar_ms: int | None
    reason: str
    active_subscription_count: int
    observed_at_ms: int = Field(..., description="Snapshot wall-clock, int64 ms UTC.")


class MarketDataFeed(Protocol):
    """Broker-neutral market-data port.

    Implementations back this with a specific vendor (IBKR today; a
    multi-symbol hub later).  Consumers depend only on this Protocol —
    they never import vendor-specific feed code.

    ``feed_id`` is a provenance tag stamped on every bar (e.g. ``"ibkr"``).
    It is NOT an execution-broker identity and must not be used for routing
    orders.

    ``stream_bars`` returns an async iterator of closed 1-minute bars.  It
    raises ``MarketDataFeedError`` when the underlying connection dies or an
    unrecoverable invariant fails. Implementations may replace a bounded
    stalled subscription transparently; ordinary bar gaps are silent.

    ``health`` is a synchronous point-in-time snapshot.  Callers may poll it
    on any cadence; it never blocks.
    """

    feed_id: str

    @property
    def capability_account_id(self) -> str | None:
        """Account whose broker capability snapshots authorize this feed."""
        ...

    def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]: ...

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        """Return closed 1-minute bars from the trailing ``lookback_days``
        calendar days, oldest first.

        Used to warm up a consumer's indicator state before it starts
        making decisions from ``stream_bars`` -- never itself a decision
        input. A source that cannot serve history returns an empty list;
        callers must treat that as "no warmup available", not an error.
        """
        ...

    def health(self, symbol: str | None = None) -> FeedHealth: ...
