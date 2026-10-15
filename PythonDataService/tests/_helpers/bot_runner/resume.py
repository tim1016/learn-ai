"""Reusable broker and feed doubles for bot-runner supervision tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from decimal import Decimal

from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition
from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar
from app.utils.timestamps import now_ms_utc

_STRATEGY_INSTANCE_ID = "alpaca-skeleton-1"


class _FlatBroker:
    """Broker truth needed by the real SQLite Clerk admission boundary."""

    broker_id = "alpaca"

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del status, limit, after_ms
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        return []

    async def submit(
        self,
        _leg: BrokerOrderLeg,
        *,
        client_order_id: str,
    ) -> BrokerOrder:
        del client_order_id
        raise AssertionError("the warmup bar must not submit an order")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("the warmup bar must not cancel an order")

    async def get_order_by_client_order_id(
        self,
        _client_order_id: str,
    ) -> BrokerOrder | None:
        return None


class _ResumeFeed:
    """Replay one explicitly installed batch, then hold or fail."""

    feed_id = "issue-1692"

    def __init__(self) -> None:
        self._bars: tuple[MarketDataBar, ...] = ()
        self._error: Exception | None = None
        self.bars_consumed = 0

    def install(
        self,
        bars: tuple[MarketDataBar, ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self._bars = bars
        self._error = error
        self.bars_consumed = 0

    async def stream_bars(
        self,
        _symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]:
        del use_rth, continuity
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        if self._error is not None:
            raise self._error
        await asyncio.Event().wait()

    async def recent_closed_bars(
        self,
        _symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del use_rth, lookback_days
        return []

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._bars[-1].start_ms if self._bars else None,
            reason="",
            active_subscription_count=0,
            observed_at_ms=now_ms_utc(),
        )


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _first_resumed_bar() -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=1_787_153_280_000,
        end_ms=1_787_153_340_000,
        open=Decimal("771.12"),
        high=Decimal("771.14"),
        low=Decimal("771.01"),
        close=Decimal("771.12"),
        volume=14_463,
        fetched_at_ms=1_787_153_345_348,
        feed_id="ibkr",
        session_phase="RTH",
    )
