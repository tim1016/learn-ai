"""Tests for app.broker.ibkr.bars."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker.ibkr.bars import (
    IBKRBarStreamError,
    aggregate_realtime_bar,
    stream_minute_bars,
)


def _bar(second: int, open_: str, high: str, low: str, close: str, volume: int):
    return SimpleNamespace(
        time=datetime(2026, 5, 4, 14, 30, second, tzinfo=UTC),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def test_realtime_bars_aggregate_within_one_minute() -> None:
    current = None
    last_ms = None
    emitted = None
    for raw in (
        _bar(0, "100.00", "101.00", "99.00", "100.50", 10),
        _bar(5, "100.50", "102.00", "100.25", "101.50", 20),
        _bar(10, "101.50", "101.75", "98.50", "99.50", 30),
    ):
        current, emitted, last_ms = aggregate_realtime_bar(
            current,
            raw,
            symbol="SPY",
            last_source_ms=last_ms,
        )
        assert emitted is None

    assert current is not None
    minute = current.to_model()
    assert minute.open == Decimal("100.00")
    assert minute.high == Decimal("102.00")
    assert minute.low == Decimal("98.50")
    assert minute.close == Decimal("99.50")
    assert minute.volume == 60


def test_new_minute_fires_previous_closed_bar() -> None:
    current = None
    last_ms = None
    current, emitted, last_ms = aggregate_realtime_bar(
        current,
        _bar(55, "100", "101", "99", "100.5", 10),
        symbol="SPY",
        last_source_ms=last_ms,
    )
    current, emitted, last_ms = aggregate_realtime_bar(
        current,
        SimpleNamespace(
            time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("101.5"),
            volume=20,
        ),
        symbol="SPY",
        last_source_ms=last_ms,
    )

    assert emitted is not None
    assert emitted.start_ms == int(datetime(2026, 5, 4, 14, 30, tzinfo=UTC).timestamp() * 1000)
    assert emitted.end_ms == int(datetime(2026, 5, 4, 14, 31, tzinfo=UTC).timestamp() * 1000)
    assert emitted.close == Decimal("100.5")
    assert current.start_ms == emitted.end_ms


def test_duplicate_source_timestamp_raises() -> None:
    current, _, last_ms = aggregate_realtime_bar(None, _bar(0, "1", "1", "1", "1", 1), symbol="SPY", last_source_ms=None)
    with pytest.raises(IBKRBarStreamError, match="Duplicate"):
        aggregate_realtime_bar(current, _bar(0, "1", "1", "1", "1", 1), symbol="SPY", last_source_ms=last_ms)


def test_non_monotonic_source_timestamp_raises() -> None:
    current, _, last_ms = aggregate_realtime_bar(
        None,
        _bar(10, "1", "1", "1", "1", 1),
        symbol="SPY",
        last_source_ms=None,
    )
    with pytest.raises(IBKRBarStreamError, match="Non-monotonic"):
        aggregate_realtime_bar(current, _bar(5, "1", "1", "1", "1", 1), symbol="SPY", last_source_ms=last_ms)


def test_naive_datetime_raises() -> None:
    raw = SimpleNamespace(
        time=datetime(2026, 5, 4, 14, 30),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )
    with pytest.raises(IBKRBarStreamError, match="naive"):
        aggregate_realtime_bar(None, raw, symbol="SPY", last_source_ms=None)


class _FakeIb:
    def __init__(self) -> None:
        self.bars = [
            _bar(55, "100", "101", "99", "100.5", 10),
            SimpleNamespace(
                time=datetime(2026, 5, 4, 14, 31, 0, tzinfo=UTC),
                open=Decimal("101"),
                high=Decimal("102"),
                low=Decimal("100"),
                close=Decimal("101.5"),
                volume=20,
            ),
        ]
        self.cancelled = False
        self.use_rth_seen: bool | None = None

    def reqRealTimeBars(self, contract, bar_size: int, what_to_show: str, *, useRTH: bool):
        self.use_rth_seen = useRTH
        assert contract.symbol == "SPY"
        assert bar_size == 5
        assert what_to_show == "TRADES"
        return self.bars

    def cancelRealTimeBars(self, bars) -> None:
        assert bars is self.bars
        self.cancelled = True

    async def qualifyContractsAsync(self, contract):
        contract.conId = 1
        return [contract]


class _FakeClient:
    def __init__(self) -> None:
        self.ib = _FakeIb()

    def require_connected(self) -> None:
        return


@pytest.mark.asyncio
async def test_stream_minute_bars_yields_closed_bar_and_cancels() -> None:
    client = _FakeClient()
    stream = stream_minute_bars(client, "SPY", use_rth=True)
    emitted = await stream.__anext__()
    await stream.aclose()

    assert emitted.close == Decimal("100.5")
    assert client.ib.use_rth_seen is True
    assert client.ib.cancelled is True
