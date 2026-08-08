"""Reusable external-transport fixtures for IBKR feed liveness regressions.

These fixtures keep the production ``stream_minute_bars`` and
``IbkrMarketDataFeed`` implementations intact.  They replace only the IBKR
transport and wall/monotonic clocks so tests can observe bounded stalls without
waiting for a live Gateway or sixty wall-clock seconds.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

_RTH_START = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


class AcceleratedFeedClock:
    """One controllable clock shared by feed freshness and line liveness."""

    def __init__(self, *, wall_ms: int) -> None:
        self._wall_ms = wall_ms
        self._monotonic_s = 0.0

    def now_ms(self) -> int:
        return self._wall_ms

    def monotonic(self) -> float:
        return self._monotonic_s

    def advance_ms(self, elapsed_ms: int) -> None:
        if elapsed_ms < 0:
            raise ValueError("feed clock cannot move backwards")
        self._wall_ms += elapsed_ms
        self._monotonic_s += elapsed_ms / 1_000


class _AdversarialRealtimeBarTransport:
    """IBKR-shaped transport that returns one deterministic plan per request."""

    def __init__(self, plans: tuple[tuple[SimpleNamespace, ...], ...]) -> None:
        self._plans = plans
        self._subscriptions: list[list[SimpleNamespace]] = []
        self._subscription_opened = asyncio.Event()
        self.cancel_count = 0

    @property
    def subscription_count(self) -> int:
        return len(self._subscriptions)

    async def qualifyContractsAsync(self, contract: Any) -> list[Any]:
        contract.conId = 1_415
        contract.primaryExchange = "ARCA"
        return [contract]

    def reqRealTimeBars(
        self,
        contract: Any,
        bar_size: int,
        what_to_show: str,
        *,
        useRTH: bool,
    ) -> list[SimpleNamespace]:
        if getattr(contract, "symbol", None) != "SPY":
            raise ValueError("adversarial feed fixture supports only SPY")
        if bar_size != 5 or what_to_show != "TRADES" or not useRTH:
            raise ValueError("adversarial feed fixture requires RTH 5-second trades")
        plan_index = len(self._subscriptions)
        if plan_index >= len(self._plans):
            raise RuntimeError("adversarial feed fixture exhausted its subscription plans")
        subscription = list(self._plans[plan_index])
        self._subscriptions.append(subscription)
        self._subscription_opened.set()
        return subscription

    def cancelRealTimeBars(self, bars: list[SimpleNamespace]) -> None:
        if not any(bars is subscription for subscription in self._subscriptions):
            raise ValueError("attempted to cancel an unknown fixture subscription")
        self.cancel_count += 1

    async def wait_until_subscribed(self, expected_count: int = 1) -> None:
        while self.subscription_count < expected_count:
            self._subscription_opened.clear()
            await asyncio.wait_for(self._subscription_opened.wait(), timeout=1)


class _AdversarialIbkrClient:
    """Minimal connected client whose transport is the only injected seam."""

    def __init__(self, transport: _AdversarialRealtimeBarTransport) -> None:
        self.ib = transport
        self.connection_lost = False

    def require_connected(self) -> None:
        return

    def is_connected(self) -> bool:
        return True


class _AdversarialFeedFixture:
    def __init__(self, plans: tuple[tuple[SimpleNamespace, ...], ...]) -> None:
        wall_ms = int((_RTH_START + timedelta(minutes=1)).timestamp() * 1_000)
        self.clock = AcceleratedFeedClock(wall_ms=wall_ms)
        self._transport = _AdversarialRealtimeBarTransport(plans)
        self.client = _AdversarialIbkrClient(self._transport)

    @property
    def subscription_count(self) -> int:
        return self._transport.subscription_count

    @property
    def cancel_count(self) -> int:
        return self._transport.cancel_count

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install only clock seams; the real feed and bar stream stay wired."""
        monkeypatch.setattr(
            "app.marketdata.ibkr_feed.now_ms_utc",
            self.clock.now_ms,
        )
        monkeypatch.setattr(
            "app.broker.ibkr.bars.now_ms_utc",
            self.clock.now_ms,
        )
        monkeypatch.setattr(
            "app.broker.ibkr.bars.time.monotonic",
            self.clock.monotonic,
        )

    async def wait_until_subscribed(self, expected_count: int = 1) -> None:
        await self._transport.wait_until_subscribed(expected_count)


class OnePrintThenSilenceFeedFixture(_AdversarialFeedFixture):
    """Established minute, one next-minute print, silence, then replacement."""

    def __init__(self) -> None:
        super().__init__(
            (
                _closed_minute_with_one_tail_print(_RTH_START),
                _closed_minute_with_one_tail_print(
                    _RTH_START + timedelta(minutes=2)
                ),
            )
        )


class NeverFirstBarFeedFixture(_AdversarialFeedFixture):
    """An active subscription that never receives its first source print."""

    def __init__(self) -> None:
        super().__init__(((),))


def _closed_minute_with_one_tail_print(
    minute_start: datetime,
) -> tuple[SimpleNamespace, ...]:
    """Return one closed-minute bootstrap plus one print in the next minute."""
    bars = tuple(
        _raw_bar(minute_start + timedelta(seconds=offset))
        for offset in range(0, 60, 5)
    )
    return (*bars, _raw_bar(minute_start + timedelta(minutes=1)))


def _raw_bar(timestamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        time=timestamp,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=10,
    )


__all__ = (
    "AcceleratedFeedClock",
    "NeverFirstBarFeedFixture",
    "OnePrintThenSilenceFeedFixture",
)
