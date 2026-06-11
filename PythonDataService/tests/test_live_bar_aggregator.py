"""Tests for the live 1-min OHLCV ring buffer aggregator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.services import live_bar_aggregator as agg_mod
from app.services.live_bar_aggregator import LiveBarAggregator


def _bar(symbol: str, start_ms: int, close: float) -> IbkrMinuteBar:
    return IbkrMinuteBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start_ms + 60_000,
    )


class _FakeClient:
    """Stand-in for IbkrClient — only ``is_connected`` is read in
    ``_resolve_client`` before tests inject bars through the patched stream."""

    def is_connected(self) -> bool:
        return True


@pytest.fixture
def fresh_aggregator(monkeypatch: pytest.MonkeyPatch) -> LiveBarAggregator:
    """Return a fresh aggregator with the IBKR boundary stubbed out.

    ``stream_minute_bars`` is replaced with a fixture-controlled async
    iterator so tests run without IBKR or a connected client.
    """
    monkeypatch.setattr(agg_mod, "get_client", lambda: _FakeClient())
    return LiveBarAggregator()


async def test_ensure_subscribed_starts_stream_and_buffers_bars(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = [_bar("SPY", 1_000_000, 100.0), _bar("SPY", 1_060_000, 101.0)]

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        for b in bars:
            yield b
        # Hang so the task stays "streaming" while we snapshot.
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    state = await fresh_aggregator.ensure_subscribed("SPY")
    # Yield control so the task runs and pushes bars.
    for _ in range(20):
        if len(state.bars) == 2:
            break
        await asyncio.sleep(0.01)

    snap = fresh_aggregator.snapshot("SPY")
    assert [b.start_ms for b in snap] == [1_000_000, 1_060_000]
    assert state.status == "streaming"
    assert state.last_bar_ms == 1_060_000

    await fresh_aggregator.shutdown()


async def test_snapshot_since_ms_filters_to_new_bars(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    bars = [_bar("SPY", 1_000_000, 100.0), _bar("SPY", 1_060_000, 101.0)]

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        for b in bars:
            yield b
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    state = await fresh_aggregator.ensure_subscribed("SPY")
    for _ in range(20):
        if len(state.bars) == 2:
            break
        await asyncio.sleep(0.01)

    snap = fresh_aggregator.snapshot("SPY", since_ms=1_000_000)
    assert [b.start_ms for b in snap] == [1_060_000]

    await fresh_aggregator.shutdown()


async def test_snapshot_unknown_symbol_returns_empty(
    fresh_aggregator: LiveBarAggregator,
) -> None:
    assert fresh_aggregator.snapshot("AAPL") == []
    status, last_error, last_bar_ms = fresh_aggregator.status("AAPL")
    assert (status, last_error, last_bar_ms) == ("idle", None, None)


async def test_stream_error_marks_errored_without_raising(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        if False:
            yield  # pragma: no cover
        raise RuntimeError("IBKR connection lost")

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    state = await fresh_aggregator.ensure_subscribed("SPY")
    # Wait for the task to error out.
    for _ in range(20):
        if state.status == "errored":
            break
        await asyncio.sleep(0.01)

    assert state.status == "errored"
    assert state.last_error is not None
    assert "IBKR connection lost" in state.last_error
    assert fresh_aggregator.snapshot("SPY") == []


async def test_ensure_subscribed_5s_buffers_raw_bars(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = [_bar("SPY", 1_000_000, 100.0), _bar("SPY", 1_005_000, 100.1)]

    async def fake_raw_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        for b in raw:
            yield b
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_raw_5s_bars", fake_raw_stream)

    state = await fresh_aggregator.ensure_subscribed_5s("SPY")
    for _ in range(20):
        if len(state.bars) == 2:
            break
        await asyncio.sleep(0.01)

    snap = fresh_aggregator.snapshot_5s("SPY")
    assert [b.start_ms for b in snap] == [1_000_000, 1_005_000]
    assert state.status == "streaming"
    assert state.last_bar_ms == 1_005_000

    await fresh_aggregator.shutdown()


async def test_5s_and_1m_buffers_are_independent(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscribing 1m must not populate the 5s buffer and vice versa."""
    bar_1m = _bar("SPY", 1_000_000, 100.0)
    bar_5s = _bar("SPY", 2_000_000, 200.0)

    async def fake_1m(_c, _s, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        yield bar_1m
        await asyncio.sleep(3600)

    async def fake_5s(_c, _s, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        yield bar_5s
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_1m)
    monkeypatch.setattr(agg_mod, "stream_raw_5s_bars", fake_5s)

    await fresh_aggregator.ensure_subscribed("SPY")
    await fresh_aggregator.ensure_subscribed_5s("SPY")
    await asyncio.sleep(0.05)

    snap_1m = fresh_aggregator.snapshot("SPY")
    snap_5s = fresh_aggregator.snapshot_5s("SPY")
    assert [b.start_ms for b in snap_1m] == [1_000_000]
    assert [b.start_ms for b in snap_5s] == [2_000_000]

    await fresh_aggregator.shutdown()


async def test_ensure_subscribed_is_idempotent_while_task_alive(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_count = 0

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    await fresh_aggregator.ensure_subscribed("SPY")
    # Yield so the task actually enters the stream function.
    await asyncio.sleep(0.01)
    await fresh_aggregator.ensure_subscribed("SPY")
    await fresh_aggregator.ensure_subscribed("SPY")

    assert call_count == 1
    await fresh_aggregator.shutdown()
