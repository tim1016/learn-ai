"""Tests for the Layer B replay call site (PRD-B #8).

``replay_session`` drives the SAME LiveEngine decision path the live run
used (apples-to-apples per story 12) over canonical bars, with a
deterministic NEXT_BAR_OPEN ``ReplaySimBroker``, and returns the replayed
decisions in the decisions.parquet shape. Tested over synthetic bars — no
LEAN cache, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.divergence.bar_series_joiner import CanonicalBar
from app.engine.live.divergence.report_bundler import ReportMetadata
from app.engine.live.live_engine import LiveEngine
from app.engine.live.replay_layer import ReplaySimBroker, replay_session, run_layer_b
from app.engine.strategy.base import DecisionSnapshot, Strategy


def _bar(minute: int, close: str = "500") -> TradeBar:
    start = datetime(2026, 5, 4, 14, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
    )


class _SnapshotStrategy(Strategy):
    """Publishes a HOLD decision snapshot on every consolidated 15-min bar."""

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        self.last_decision_snapshot = DecisionSnapshot(
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            ema5=1.0,
            ema10=2.0,
            rsi=50.0,
            signal="HOLD",
            intended_price=float(bar.close),
        )


@pytest.mark.asyncio
async def test_replay_session_returns_decisions_for_each_consolidated_bar(tmp_path) -> None:
    # 17 one-minute bars from :30 → one 15-min consolidated bar closes at :45.
    bars = [_bar(minute) for minute in range(30, 47)]

    decisions = await replay_session(_SnapshotStrategy(), bars, output_dir=tmp_path)

    assert len(decisions) == 1
    row = decisions.iloc[0]
    assert row["signal"] == "HOLD"
    assert row["ema5"] == 1.0
    assert row["ema10"] == 2.0
    # Replay uses live_paper semantics regardless of the live run's mode.
    assert row["mode"] == "live_paper"
    assert row["bar_source"] == "ibkr_paper_delayed"


def _live_decisions_matching(bar_close_ms: int) -> pd.DataFrame:
    from app.engine.live.artifacts import DecisionRow

    row = DecisionRow(
        bar_close_ms=bar_close_ms,
        signal="HOLD",
        intended_price=500.0,
        bar_source="ibkr_paper_delayed",
        bar_close=500.0,  # only field the live engine captures today
        mode="live_paper",
        indicator_values={"ema5": 1.0, "ema10": 2.0, "rsi": 50.0},
    )
    return pd.DataFrame([row.as_row()])


@pytest.mark.asyncio
async def test_run_layer_b_clean_day_passes_and_writes_replay_bundle(tmp_path) -> None:
    bars = [_bar(minute) for minute in range(30, 47)]
    bar_close_ms = int(datetime(2026, 5, 4, 14, 45, tzinfo=UTC).timestamp() * 1000)

    canonical_decision_bars = [
        CanonicalBar(
            bar_close_ms=bar_close_ms,
            open=500.0,
            high=500.0,
            low=500.0,
            close=500.0,  # agrees with live bar_close → no DATA_DRIFT
            volume=100.0,
        )
    ]
    metadata = ReportMetadata(
        run_id="run-1",
        strategy_instance_id="spy-ema:inst-1",
        trading_day=1,
        session_window_ms=(0, bar_close_ms + 1),
        layer="replay",
        tolerances={"bar_value_atol": 0.01},
    )

    paths = await run_layer_b(
        live_decisions=_live_decisions_matching(bar_close_ms),
        strategy=_SnapshotStrategy(),
        canonical_minute_bars=bars,
        canonical_decision_bars=canonical_decision_bars,
        reports_dir=tmp_path / "reports",
        work_dir=tmp_path / "replay-work",
        metadata=metadata,
    )

    assert paths.json.name == "day-1.replay.json"
    summary = json.loads(paths.json.read_text())
    # Live and replayed agree on signal, indicators, and close → clean gate.
    assert summary["passed"] is True
    assert summary["gating_breach_count"] == 0


@pytest.mark.asyncio
async def test_run_layer_b_decision_drift_fails_the_gate(tmp_path) -> None:
    bars = [_bar(minute) for minute in range(30, 47)]
    bar_close_ms = int(datetime(2026, 5, 4, 14, 45, tzinfo=UTC).timestamp() * 1000)

    # Live recorded ENTER with the SAME indicator state the replay computes,
    # but the replay (SnapshotStrategy) emits HOLD → DECISION_DRIFT (gating).
    from app.engine.live.artifacts import DecisionRow

    live = DecisionRow(
        bar_close_ms=bar_close_ms,
        signal="ENTER",
        intended_price=500.0,
        bar_source="ibkr_paper_delayed",
        bar_close=500.0,
        mode="live_paper",
        indicator_values={"ema5": 1.0, "ema10": 2.0, "rsi": 50.0},
    )
    metadata = ReportMetadata(
        run_id="run-1",
        strategy_instance_id="spy-ema:inst-1",
        trading_day=1,
        session_window_ms=(0, bar_close_ms + 1),
        layer="replay",
        tolerances={},
    )

    paths = await run_layer_b(
        live_decisions=pd.DataFrame([live.as_row()]),
        strategy=_SnapshotStrategy(),
        canonical_minute_bars=bars,
        canonical_decision_bars=[
            CanonicalBar(
                bar_close_ms=bar_close_ms,
                open=500.0,
                high=500.0,
                low=500.0,
                close=500.0,
                volume=100.0,
            )
        ],
        reports_dir=tmp_path / "reports",
        work_dir=tmp_path / "replay-work",
        metadata=metadata,
    )

    summary = json.loads(paths.json.read_text())
    assert summary["passed"] is False
    assert "decision_drift" in summary["gating_categories"]


# ---------------------------------------------------------------------------
# Regression test for issue #1302 — final-bar order never fills
# ---------------------------------------------------------------------------


class _EntryOnFinalBarStrategy(Strategy):
    """Emits a HOLD decision and calls set_holdings on EVERY consolidated bar.

    Used to test final-bar order settlement: when this strategy runs over a
    single 15-min consolidation window, it fires exactly once — on the last
    input bar — and queues a BUY.  Without the fix the queued order is never
    advanced (no next bar arrives) and the engine silently returns
    pending_orders=0 even though one broker-side pending order was never filled.
    """

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        self.last_decision_snapshot = DecisionSnapshot(
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            ema5=1.0,
            ema10=2.0,
            rsi=50.0,
            signal="ENTER",
            intended_price=float(bar.close),
        )
        # Queue a market buy — the order lands in broker._pending but can only
        # fill on the *next* bar's open.  When this fires on the last bar of the
        # feed there is no next bar, so the fix must surface the count rather
        # than silently drop it.
        self.ctx.set_holdings("SPY", 1.0)


@pytest.mark.asyncio
async def test_order_emitted_on_final_bar_is_surfaced_as_pending(tmp_path: Path) -> None:
    """Regression test for issue #1302.

    A single 15-min consolidation window (15 one-minute bars) causes the
    strategy to emit a BUY on the last bar.  The order is placed with the
    broker (``place_order`` called, broker._pending has 1 entry) but there
    is no following bar to call ``advance_bar`` and fill it.

    Before the fix: ``result.pending_orders == 0`` — the broker-side pending
    order was invisible to the caller.
    After the fix: ``result.pending_orders == 1`` — the unresolved order is
    cancelled and its count is surfaced in the result.
    """
    # 16 one-minute bars: minutes 30-44 build the working consolidated bar
    # (14:30–14:45 window).  The bar at minute 45 (floor=14:45 ≠ 14:30)
    # triggers the consolidator to fire and starts a new working window.
    # on_bar() fires during bar-45 processing → set_holdings queues a BUY →
    # _submit_pending_with_meta places it with the broker.  The feed is now
    # exhausted — advance_bar is never called for bar 46 — so the order
    # stays in broker._pending and is never filled.
    bars = [_bar(minute) for minute in range(30, 46)]

    broker = ReplaySimBroker(initial_cash=Decimal("100000"))
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=broker,
        output_dir=tmp_path,
    )

    result = await engine.run(_EntryOnFinalBarStrategy(), _async_iter(bars))

    # The strategy fired exactly once (one 15-min window).
    assert len(result.submitted_order_ids) == 1, "expected one order submitted"
    # No fill arrived — no next bar existed.
    assert result.order_events == [], "expected no fills (feed exhausted before next bar)"
    # After the fix: the unresolved broker-pending order is cancelled and
    # surfaced.  Before the fix this asserts 0 (the bug).
    assert result.pending_orders == 1, (
        "expected 1 unresolved pending order surfaced in result (issue #1302)"
    )
    # Equity must reflect reality: no fill, so cash is unchanged.
    assert result.final_equity == Decimal("100000"), (
        "final equity must match initial cash when the order never filled"
    )


async def _async_iter(bars: list[TradeBar]):  # type: ignore[return]
    for bar in bars:
        yield bar
