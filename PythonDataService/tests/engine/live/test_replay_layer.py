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

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.divergence.bar_series_joiner import CanonicalBar
from app.engine.live.divergence.report_bundler import ReportMetadata
from app.engine.live.replay_layer import replay_session, run_layer_b
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
