"""Integration tests for the LiveEngine artifact-writer wiring.

Covers Phase C-2b-ii: when ``LiveEngine`` is instantiated with an
``output_dir``, it opens the ``LiveArtifactWriters`` bundle, feeds it
per-bar decisions / per-fill executions / per-closed-trade rows during
``run()``, and closes the bundle in ``finally``.

Replay tests (no output_dir) exercise the no-op path — verifies the
integration is purely additive when a writer isn't requested.

Uses the existing FakeBroker fixture so no IBKR connection is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.live.reconcile import (
    load_python_decisions,
    load_python_executions,
)
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


def _bar(minute: int, open_: str, close: str) -> TradeBar:
    """Build a 1-min bar at 14:00 UTC + ``minute`` minutes (handles hour rollover)."""
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(open_),
        high=max(Decimal(open_), Decimal(close)),
        low=min(Decimal(open_), Decimal(close)),
        close=Decimal(close),
        volume=100,
    )


class _OneEntryWithDecisionSnapshotStrategy(Strategy):
    """Submits one entry on the second consolidated bar; publishes snapshots.

    A minimal strategy that exercises the writer wiring without needing
    SpyEmaCrossover's full indicator stack. Sets last_decision_snapshot
    to a non-None value on every bar so the writer has something to
    record.
    """

    def __init__(self) -> None:
        super().__init__()
        self.bars_seen: int = 0
        # The base Strategy doesn't carry trade_log; declare it so
        # _flush_new_trades observes a real attribute. Empty list
        # means no trades — we don't append in this test.
        self.trade_log: list = []

    def initialize(self) -> None:
        from app.engine.strategy.base import DecisionSnapshot

        self._snap_cls = DecisionSnapshot
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        self.bars_seen += 1
        signal = "HOLD"
        # Trigger a single entry on the second consolidated bar so a
        # broker fill flows through the writer.
        if (
            self.bars_seen == 2
            and not self.ctx.portfolio.pending_orders
            and not self.ctx.portfolio.get_position("SPY").quantity
        ):
            self.ctx.set_holdings("SPY", Decimal("1"))
            signal = "ENTER"
        self.last_decision_snapshot = self._snap_cls(
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            ema5=float(bar.close),
            ema10=float(bar.close) - 0.5,
            rsi=60.0,
            signal=signal,
            intended_price=float(bar.close),
        )


@pytest.mark.asyncio
async def test_live_engine_writes_decisions_when_output_dir_set(tmp_path: Path) -> None:
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    bars = [_bar(minute, "500", "500") for minute in range(30, 61)]

    await engine.run(_OneEntryWithDecisionSnapshotStrategy(), iter_bars(bars))

    decisions_path = tmp_path / "decisions.parquet"
    assert decisions_path.exists(), "decisions.parquet should be written when output_dir is set"
    df = load_python_decisions(decisions_path)
    # Two consolidated 15-min bars fit in 31 minute bars (14:30..15:01),
    # so we expect 2 decision rows. Each is unique by bar_close_ms.
    assert len(df) >= 1
    assert len(df) == df["bar_close_ms"].nunique(), (
        "writer should dedupe by bar_close_ms (consolidator emits once per 15-min)"
    )
    assert "ENTER" in set(df["signal"].unique())


@pytest.mark.asyncio
async def test_live_engine_writes_executions_when_output_dir_set(tmp_path: Path) -> None:
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    # Need bars past the entry so the FakeBroker (next-bar-open fills)
    # actually produces a fill that flows through _drain_replay_order_events.
    # Strategy enters on the 2nd consolidated bar (at minute 60); the next
    # minute bar (61+) carries the fill.
    bars = [_bar(minute, "500", "500") for minute in range(30, 80)]

    await engine.run(_OneEntryWithDecisionSnapshotStrategy(), iter_bars(bars))

    executions_path = tmp_path / "executions.parquet"
    assert executions_path.exists()
    df = load_python_executions(executions_path)
    assert len(df) == 1
    assert df.iloc[0]["account_id"] == "DU123"
    assert df.iloc[0]["fill_quantity"] == 200  # SetHoldings(SPY, 1.0) at 500 ⇒ 200 shares
    assert df.iloc[0]["fill_price"] == pytest.approx(500.0)
    # client_order_id must be unique per order — the reconciler joins on it later.
    assert df.iloc[0]["client_order_id"] == "live-1"


@pytest.mark.asyncio
async def test_live_engine_writes_no_files_when_output_dir_omitted(tmp_path: Path) -> None:
    """No output_dir ⇒ no file IO. Replay tests that don't care must stay clean."""
    broker = FakeBroker()
    engine = LiveEngine(None, LiveConfig(), broker=broker)
    bars = [_bar(minute, "500", "500") for minute in range(30, 47)]
    await engine.run(_OneEntryWithDecisionSnapshotStrategy(), iter_bars(bars))

    # The tmp_path was never passed to the engine; nothing should appear there.
    assert not any(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_live_engine_dedupes_decisions_across_minute_bars(tmp_path: Path) -> None:
    """Consolidator is silent on most minute bars — the writer must not record duplicates.

    The strategy publishes snapshot on every CONSOLIDATED bar (15-min
    boundary). The minute-bar loop iterates 31 times here; if the
    writer wasn't deduping, we'd see 31 decision rows instead of 2.
    """
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    bars = [_bar(minute, "500", "500") for minute in range(30, 61)]
    await engine.run(_OneEntryWithDecisionSnapshotStrategy(), iter_bars(bars))

    df = pd.read_parquet(tmp_path / "decisions.parquet")
    # 31 minute bars span two complete 15-min windows (14:30→14:45 and
    # 14:45→15:00); the third window (15:00→15:01) doesn't close.
    # Consolidator emits at most twice in this range.
    assert len(df) <= 2, f"expected ≤ 2 dedup'd rows for 2 windows; got {len(df)}: {df}"


@pytest.mark.asyncio
async def test_live_engine_invokes_live_state_writer_per_bar(tmp_path: Path) -> None:
    """Smoke test for the LiveStateSidecar wire-up: the engine calls
    the live_state_writer callable once per bar with the bar's close_ms.
    """
    invocations: list[tuple[object, int]] = []

    def writer(portfolio: object, bar_close_ms: int) -> None:
        invocations.append((portfolio, bar_close_ms))

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        live_state_writer=writer,
    )
    bars = [_bar(minute, "500", "500") for minute in range(30, 35)]
    await engine.run(_OneEntryWithDecisionSnapshotStrategy(), iter_bars(bars))

    assert len(invocations) >= len(bars), (
        f"writer must run at least once per bar; got {len(invocations)} "
        f"for {len(bars)} bars"
    )
    # Each invocation carries a positive int64 ms timestamp.
    for _portfolio, bar_close_ms in invocations:
        assert isinstance(bar_close_ms, int) and bar_close_ms > 0


@pytest.mark.asyncio
async def test_live_engine_swallows_live_state_writer_exceptions(
    tmp_path: Path,
) -> None:
    """A sidecar I/O failure must not crash the bar loop. The engine
    catches and logs but processing continues.
    """
    call_count = 0

    def boom(_portfolio: object, _bar_close_ms: int) -> None:
        nonlocal call_count
        call_count += 1
        raise OSError("simulated sidecar I/O failure")

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        live_state_writer=boom,
    )
    bars = [_bar(minute, "500", "500") for minute in range(30, 33)]
    # Must NOT raise.
    await engine.run(_OneEntryWithDecisionSnapshotStrategy(), iter_bars(bars))
    assert call_count >= 1, "writer was never invoked"


@pytest.mark.asyncio
async def test_live_engine_closes_writers_on_exception(tmp_path: Path) -> None:
    """Writers must close (and flush) even when run() raises mid-loop.

    Closes happen in the finally block; without that, partial writes
    would be lost on any unhandled exception. We arrange the raise on
    the SECOND consolidated bar so the first one's row has already
    been buffered before the failure.
    """

    class _RaisingStrategy(_OneEntryWithDecisionSnapshotStrategy):
        def on_bar(self, bar: TradeBar) -> None:
            super().on_bar(bar)
            # First consolidated bar is allowed through cleanly so its
            # decision row lands in the buffer; second bar triggers the
            # fault path. Without finally→close_all, that buffered row
            # would never reach disk.
            if self.bars_seen >= 2:
                raise RuntimeError("boom")

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    bars = [_bar(minute, "500", "500") for minute in range(30, 80)]

    with pytest.raises(RuntimeError, match="boom"):
        await engine.run(_RaisingStrategy(), iter_bars(bars))

    # Bar #1's snapshot was buffered before the bar #2 raise; close_all
    # in finally must have flushed it.
    decisions_path = tmp_path / "decisions.parquet"
    assert decisions_path.exists(), "writers must flush on exception path"
    df = pd.read_parquet(decisions_path)
    assert len(df) >= 1
