"""Tests for IbkrMinuteBar → TradeBar conversion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.engine.live.bar_adapter import (
    ibkr_minute_bar_to_trade_bar,
    trade_bars_from_ibkr,
)


def _ibkr_bar(start: datetime) -> IbkrMinuteBar:
    start_ms = int(start.astimezone(UTC).timestamp() * 1000)
    return IbkrMinuteBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("500.00"),
        high=Decimal("501.00"),
        low=Decimal("499.50"),
        close=Decimal("500.50"),
        volume=1234,
        fetched_at_ms=start_ms + 60_000,
    )


def test_ibkr_minute_bar_to_trade_bar_preserves_ohlcv() -> None:
    bar = _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC))
    trade = ibkr_minute_bar_to_trade_bar(bar)

    assert trade.symbol == "SPY"
    assert trade.open == Decimal("500.00")
    assert trade.high == Decimal("501.00")
    assert trade.low == Decimal("499.50")
    assert trade.close == Decimal("500.50")
    assert trade.volume == 1234


def test_ibkr_minute_bar_to_trade_bar_returns_eastern_tz_aware() -> None:
    bar = _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC))
    trade = ibkr_minute_bar_to_trade_bar(bar)

    assert trade.time.tzinfo is not None
    assert trade.end_time.tzinfo is not None
    # 14:30 UTC = 10:30 ET (DST in May 2026); the engine consumes
    # tz-aware datetimes in the exchange timezone so wall-clock session
    # rules (force_flat_at = 15:55 ET) work without re-conversion.
    assert trade.time.astimezone(ZoneInfo("America/New_York")).hour == 10
    assert trade.time.astimezone(ZoneInfo("America/New_York")).minute == 30
    assert (trade.end_time - trade.time).total_seconds() == 60


def test_ibkr_minute_bar_to_trade_bar_round_trips_int64_ms() -> None:
    start_ms = 1_746_369_000_000  # arbitrary epoch ms
    bar = IbkrMinuteBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=0,
        fetched_at_ms=start_ms,
    )
    trade = ibkr_minute_bar_to_trade_bar(bar)

    assert int(trade.time.astimezone(UTC).timestamp() * 1000) == start_ms
    assert int(trade.end_time.astimezone(UTC).timestamp() * 1000) == start_ms + 60_000


@pytest.mark.asyncio
async def test_trade_bars_from_ibkr_yields_converted_stream() -> None:
    bars = [
        _ibkr_bar(datetime(2026, 5, 4, 14, 30, tzinfo=UTC)),
        _ibkr_bar(datetime(2026, 5, 4, 14, 31, tzinfo=UTC)),
    ]

    async def _source() -> AsyncIterator[IbkrMinuteBar]:
        for bar in bars:
            yield bar

    out = []
    async for trade in trade_bars_from_ibkr(_source()):
        out.append(trade)

    assert len(out) == 2
    assert all(t.symbol == "SPY" for t in out)
    assert (out[1].time - out[0].time).total_seconds() == 60
