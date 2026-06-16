"""Tests for the live 1-min OHLCV ring buffer aggregator."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
from app.services import live_bar_aggregator as agg_mod
from app.services.bar_persistence import BarPersistence
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


async def test_stream_error_survives_across_resubscribe_polls(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the snapshot-hides-error bug.

    The earlier implementation cleared ``last_error`` + reset status
    to 'subscribing' inside ``ensure_subscribed`` whenever the prior
    task had completed (errored). Combined with the snapshot endpoint
    that calls ``ensure_subscribed`` before reading the state, every
    poll after a failed stream returned ``status='subscribing',
    last_error=None`` — the operator panel never saw the broker
    disconnect. The new ``_pump`` clears ``last_error`` only on the
    first successful bar; the resubscribe path leaves the prior
    failure visible until then.
    """
    raise_count = 0

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        nonlocal raise_count
        raise_count += 1
        if False:
            yield  # pragma: no cover
        raise RuntimeError("IBKR connection lost")

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    # First poll: task starts, hits the raise, transitions to errored.
    state = await fresh_aggregator.ensure_subscribed("SPY")
    for _ in range(20):
        if state.status == "errored":
            break
        await asyncio.sleep(0.01)
    first_error = state.last_error

    # Second poll: simulates the next 5-second snapshot. The prior
    # task is done — ensure_subscribed restarts it — but the snapshot
    # endpoint must still see the prior failure, not "subscribing".
    state_again = await fresh_aggregator.ensure_subscribed("SPY")
    assert state_again is state
    assert state.status == "errored", (
        "resubscribe must preserve 'errored' until the new task yields a bar"
    )
    assert state.last_error == first_error, (
        "resubscribe must preserve the prior last_error message"
    )

    await fresh_aggregator.shutdown()


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


async def test_resubscribe_all_restarts_existing_streams(
    fresh_aggregator: LiveBarAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[str] = []

    async def fake_1m(_c, symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        starts.append(f"1m:{symbol}")
        yield _bar(symbol, 1_000_000 + len(starts), 100.0)
        await asyncio.sleep(3600)

    async def fake_5s(_c, symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        starts.append(f"5s:{symbol}")
        yield _bar(symbol, 2_000_000 + len(starts), 100.0)
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_1m)
    monkeypatch.setattr(agg_mod, "stream_raw_5s_bars", fake_5s)

    await fresh_aggregator.ensure_subscribed("SPY")
    await fresh_aggregator.ensure_subscribed_5s("QQQ")
    for _ in range(20):
        if len(starts) == 2:
            break
        await asyncio.sleep(0.01)

    await fresh_aggregator.resubscribe_all()
    for _ in range(20):
        if len(starts) == 4:
            break
        await asyncio.sleep(0.01)

    assert starts.count("1m:SPY") == 2
    assert starts.count("5s:QQQ") == 2
    assert fresh_aggregator.status("SPY")[0] == "streaming"
    assert fresh_aggregator.status_5s("QQQ")[0] == "streaming"

    await fresh_aggregator.shutdown()


async def test_pump_persists_each_emitted_bar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Slice 4: each bar the stream yields is written through BarPersistence so
    a restart can replay today's bars to the chart."""
    monkeypatch.setattr(agg_mod, "get_client", lambda: _FakeClient())
    persistence = BarPersistence(root=tmp_path)
    aggregator = LiveBarAggregator(persistence=persistence, today_provider=_bar_date)

    bars = [_bar("SPY", 1_775_001_600_000, 100.0), _bar("SPY", 1_775_001_660_000, 100.5)]

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        for b in bars:
            yield b
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    state = await aggregator.ensure_subscribed("SPY")
    for _ in range(50):
        if len(state.bars) == 2:
            break
        await asyncio.sleep(0.01)

    replayed = persistence.replay("SPY", "1m", _bar_date())
    assert [b.start_ms for b in replayed] == [1_775_001_600_000, 1_775_001_660_000]

    await aggregator.shutdown()


async def test_ensure_subscribed_replays_todays_persisted_bars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Slice 4: on subscribe, the aggregator seeds its ring buffer with bars
    already on disk so a restart hands the chart today's morning bars
    before the stream produces a single new one."""
    monkeypatch.setattr(agg_mod, "get_client", lambda: _FakeClient())
    persistence = BarPersistence(root=tmp_path)
    # Pre-seed today's JSONL with two bars (a prior daemon wrote them).
    persistence.append("SPY", "1m", _bar("SPY", 1_775_001_600_000, 100.0))
    persistence.append("SPY", "1m", _bar("SPY", 1_775_001_660_000, 100.5))

    aggregator = LiveBarAggregator(persistence=persistence, today_provider=_bar_date)

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        # The stream stays open but emits nothing — the chart's first
        # snapshot must therefore come entirely from replay.
        await asyncio.sleep(3600)
        yield  # pragma: no cover

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    state = await aggregator.ensure_subscribed("SPY")
    # Replay is synchronous on ensure_subscribed — no await loop needed.
    assert [b.start_ms for b in state.bars] == [1_775_001_600_000, 1_775_001_660_000]
    assert aggregator.snapshot("SPY")[0].start_ms == 1_775_001_600_000

    await aggregator.shutdown()


async def test_pump_drops_partial_first_bar_at_stream_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Slice 4: a restart mid-minute lets the consolidator emit a partial
    first bar (a bar whose end_ms - start_ms != the full window). The
    pump must drop that first bar so the chart never shows a ragged
    short bar from a mid-minute restart."""
    monkeypatch.setattr(agg_mod, "get_client", lambda: _FakeClient())
    persistence = BarPersistence(root=tmp_path)
    aggregator = LiveBarAggregator(persistence=persistence, today_provider=_bar_date)

    # Build a partial 1-min bar (only 30s of coverage) followed by a full one.
    partial = IbkrMinuteBar(
        symbol="SPY",
        start_ms=1_775_001_600_000,
        end_ms=1_775_001_630_000,  # only 30s of window
        open=Decimal("100.0"),
        high=Decimal("100.0"),
        low=Decimal("100.0"),
        close=Decimal("100.0"),
        volume=10,
        fetched_at_ms=1_775_001_630_000,
    )
    full = _bar("SPY", 1_775_001_660_000, 100.5)

    async def fake_stream(_client, _symbol, **_kw) -> AsyncIterator[IbkrMinuteBar]:
        yield partial
        yield full
        await asyncio.sleep(3600)

    monkeypatch.setattr(agg_mod, "stream_minute_bars", fake_stream)

    state = await aggregator.ensure_subscribed("SPY")
    for _ in range(50):
        if len(state.bars) == 1 and state.bars[-1].start_ms == full.start_ms:
            break
        await asyncio.sleep(0.01)

    snap = aggregator.snapshot("SPY")
    assert [b.start_ms for b in snap] == [1_775_001_660_000]
    # Persistence must not record the dropped partial either — it would
    # poison the replay path with a malformed bar.
    replayed = persistence.replay("SPY", "1m", _bar_date())
    assert [b.start_ms for b in replayed] == [1_775_001_660_000]

    await aggregator.shutdown()


def _bar_date():
    """UTC date of the anchor timestamp used in Slice 4 tests."""
    from datetime import date

    # 2026-04-01 00:00:00 UTC anchor matches the ms values above.
    return date(2026, 4, 1)


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
