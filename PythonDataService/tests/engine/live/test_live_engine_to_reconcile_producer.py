"""Producer-consumer CI test: LiveEngine artifacts feed reconcile cleanly.

The live-session gate is the real integration test; this cheap CI version
proves the LiveEngine -> reconcile file contract without an IB Gateway.

The engine itself produces the decision, execution, equity, and hydration
artifacts. The QC CSV is a compact independently authored fixture because
cross-engine numerical equivalence belongs to reconciliation unit tests and
tomorrow's external live-session validation, not this no-Gateway test.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.indicator_state import HydratePolicy, HydrationReceipt
from app.engine.live.live_engine import LiveEngine
from app.engine.live.reconcile import write_day_report
from app.engine.strategy.base import DecisionSnapshot, Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker

_ET = ZoneInfo("America/New_York")


async def _bar_source() -> None:
    """Yield enough bars for decision publication and a FakeBroker fill."""
    t0 = datetime(2026, 5, 18, 10, 30, tzinfo=_ET)
    for i in range(50):
        t = t0 + timedelta(minutes=i)
        yield TradeBar(
            symbol="SPY",
            time=t,
            end_time=t + timedelta(minutes=1),
            open=Decimal("400"),
            high=Decimal("400"),
            low=Decimal("400"),
            close=Decimal("400"),
            volume=100,
        )


class _ArtifactProducerStrategy(Strategy):
    """Minimal strategy that emits snapshots and one deterministic entry."""

    STRATEGY_KEY = "artifact_producer"
    CONSOLIDATOR_PERIOD_MIN = 15

    def __init__(self) -> None:
        super().__init__()
        self.bars_seen = 0
        self.trade_log: list = []

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        self.bars_seen += 1
        signal = "HOLD"
        if self.bars_seen == 2:
            self.ctx.set_holdings("SPY", Decimal("1"))
            signal = "ENTER"
        self.last_decision_snapshot = DecisionSnapshot(
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            ema5=float(bar.close),
            ema10=float(bar.close) - 0.5,
            rsi=60.0,
            signal=signal,
            intended_price=float(bar.close),
        )


def _write_qc_indicator_fixture(qc_dir: Path, decisions: pd.DataFrame) -> None:
    """Write the external-engine-shaped input expected by the reconciler.

    Values are fixed by ``_ArtifactProducerStrategy`` rather than copied from
    the engine output. Timestamps come from the emitted decision artifact
    because this test targets the producer/consumer boundary, not a second
    engine's timestamp-alignment contract.
    """
    qc_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "bar_close_ms": decisions["bar_close_ms"],
            "ema5": 400.0,
            "ema10": 399.5,
            "rsi": 60.0,
            "signal": decisions["signal"],
        }
    ).to_csv(qc_dir / "indicators.csv", index=False)


@pytest.mark.asyncio
async def test_live_engine_artifacts_feed_reconcile_and_hydration_hash(tmp_path: Path) -> None:
    """LiveEngine emits real artifacts that reconcile consumes end-to-end."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "producer_test"
    run_dir.mkdir(parents=True)
    qc_dir = tmp_path / "qc" / "2026-05-18"
    docs_dir = tmp_path / "docs-out"

    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=_ET).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
        code_sha="producer-test",
        strategy_spec_sha="producer-test",
    )

    await engine.run(_ArtifactProducerStrategy(), bars=_bar_source())

    receipt_path = run_dir / "indicator_state_hydration.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt_obj = HydrationReceipt.model_validate_json(receipt_bytes)
    assert receipt_obj.accepted is False
    assert receipt_obj.validation.failure_reason == "disabled_by_operator"
    assert receipt_obj.policy == HydratePolicy.DISABLED
    expected_hydration_sha = hashlib.sha256(receipt_bytes).hexdigest()

    decisions_path = run_dir / "decisions.parquet"
    executions_path = run_dir / "executions.parquet"
    equity_path = run_dir / "equity_curve.parquet"
    assert decisions_path.exists(), "LiveEngine did not produce decisions.parquet"
    assert executions_path.exists(), "LiveEngine did not produce executions.parquet"
    assert equity_path.exists(), "LiveEngine did not produce equity_curve.parquet"
    decisions = pd.read_parquet(decisions_path)
    executions = pd.read_parquet(executions_path)
    assert not decisions.empty
    assert not executions.empty
    _write_qc_indicator_fixture(qc_dir, decisions)

    paths = write_day_report(
        run_dir=run_dir,
        qc_dir=qc_dir,
        docs_dir=docs_dir,
        run_label="producer-test-2026-05-18",
        day_n=0,
        day_date=date(2026, 5, 18),
    )

    hashes = json.loads(paths.hashes.read_text(encoding="utf-8"))
    assert hashes["indicator_state_hydration.json"] == expected_hydration_sha
    assert hashes["python_equity_curve_parquet"]
    assert hashes["python_input_bars_parquet"] is None
    md_text = paths.md.read_text(encoding="utf-8")
    assert expected_hydration_sha in md_text
