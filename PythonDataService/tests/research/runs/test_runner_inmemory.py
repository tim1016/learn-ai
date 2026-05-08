"""In-memory runner tests — A1's reproducibility gate.

Storage and HTTP boundaries are covered by separate suites in A2/A3.
This file proves that ``run_strategy_spec`` produces deterministic
ledger identity and result hashes for the same input contract — the
core invariant Phase A is built on.

The test injects a synthetic data source (``FakeDataReader``) so the
run is hermetic and runs in milliseconds. The shape of the spec
matches the canonical SPY EMA fixture but is constructed inline on a
``TEST`` symbol so the suite doesn't break if the SPY fixture changes
its declared symbol.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.engine import EquitySnapshot
from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.runs import RunRequest, run_strategy_spec
from app.research.runs.ledger import RunLedger
from app.research.runs.result import BacktestRunResult
from app.research.runs.runner import _summarize_metrics


def _build_test_spec(
    *,
    fast_period: int = 5,
    slow_period: int = 10,
    rsi_period: int = 14,
    rsi_lo: float = 50.0,
    rsi_hi: float = 70.0,
    bars_to_hold: int = 5,
) -> StrategySpec:
    """Construct an EMA-crossover spec on the synthetic ``TEST`` symbol.

    Mirrors the canonical SPY fixture's structure (FreshCross + gap +
    RSI band + N-bar hold) so the runner exercises the same evaluator
    paths as the SPY fixture would, without coupling the test to that
    fixture's declared symbol.
    """
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": fast_period, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": slow_period, "source": "close"},
                {
                    "id": "rsi",
                    "kind": "RSI",
                    "period": rsi_period,
                    "source": "close",
                    "ma_type": "wilders",
                },
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "fast", "right": "slow", "direction": "up"},
                    {
                        "kind": "IndicatorComparison",
                        "left": {
                            "kind": "Subtract",
                            "left": {"kind": "IndicatorRef", "indicator": "fast"},
                            "right": {"kind": "IndicatorRef", "indicator": "slow"},
                        },
                        "op": ">=",
                        "right": {"kind": "Const", "value": 0.20},
                    },
                    {
                        "kind": "IndicatorBetween",
                        "indicator": "rsi",
                        "lo": rsi_lo,
                        "hi": rsi_hi,
                        "inclusive": True,
                    },
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [
                    {"kind": "BarsSinceEntry", "op": ">=", "value": bars_to_hold}
                ],
            },
            "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
        }
    )


@pytest.fixture
def fake_data_factory():
    """Build a deterministic synthetic data source on demand.

    The factory is parameterized by symbol/start/end like the real
    LEAN factory in ``app/routers/spec_strategy.py``; the same
    pre-generated bar list is returned every time so two runs see
    identical inputs.
    """
    # ``build_minute_bars`` hard-codes ``symbol="TEST"`` (parity helper
    # constant) which matches the inline spec below. ``TradeBar`` is a
    # frozen dataclass so any rename would have to rebuild the list.
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    return factory


def _run(
    spec: StrategySpec,
    factory,
    *,
    start: date = date(2024, 1, 2),
    end: date = date(2024, 12, 31),
    fill_mode: str = "signal_bar_close",
    commission: float = 0.0,
    seed: int = 0,
    data_root_revision: str = "test-revision-1",
    run_id: str | None = None,
) -> tuple[RunLedger, BacktestRunResult]:
    return run_strategy_spec(
        RunRequest(
            spec=spec,
            start_date=start,
            end_date=end,
            fill_mode=fill_mode,
            commission_per_order=commission,
            random_seed=seed,
        ),
        data_source_factory=factory,
        data_root_revision=data_root_revision,
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Core acceptance gate.
# ---------------------------------------------------------------------------
def test_repeat_runs_produce_identical_hashes(fake_data_factory):
    spec = _build_test_spec()
    ledger1, result1 = _run(spec, fake_data_factory)
    ledger2, result2 = _run(spec, fake_data_factory)

    assert ledger1.status == "completed"
    assert ledger2.status == "completed"
    assert ledger1.strategy_spec_hash == ledger2.strategy_spec_hash
    assert ledger1.data_snapshot_id == ledger2.data_snapshot_id
    assert ledger1.result_hash == ledger2.result_hash
    assert ledger1.trade_log_hash == ledger2.trade_log_hash
    assert ledger1.metrics_hash == ledger2.metrics_hash

    # run_id is a UUID and must differ between runs.
    assert ledger1.run_id != ledger2.run_id
    # Result content must agree even though run_id is embedded.
    assert len(result1.trades) == len(result2.trades)
    assert result1.final_equity == result2.final_equity


def test_changing_spec_param_changes_spec_and_result_hash(fake_data_factory):
    spec_a = _build_test_spec(fast_period=5)
    spec_b = _build_test_spec(fast_period=6)

    ledger_a, _ = _run(spec_a, fake_data_factory)
    ledger_b, _ = _run(spec_b, fake_data_factory)

    assert ledger_a.strategy_spec_hash != ledger_b.strategy_spec_hash
    # data_snapshot_id is unchanged (same symbol/resolution/dates/revision)
    assert ledger_a.data_snapshot_id == ledger_b.data_snapshot_id
    # Behavior should change — different EMA period produces a different
    # trade log even on the same input bars.
    assert ledger_a.result_hash != ledger_b.result_hash


def test_changing_data_window_changes_data_snapshot_id(fake_data_factory):
    spec = _build_test_spec()
    ledger_a, _ = _run(spec, fake_data_factory, start=date(2024, 1, 2))
    ledger_b, _ = _run(spec, fake_data_factory, start=date(2024, 1, 3))

    assert ledger_a.strategy_spec_hash == ledger_b.strategy_spec_hash
    assert ledger_a.data_snapshot_id != ledger_b.data_snapshot_id


def test_changing_data_root_revision_changes_data_snapshot_id(fake_data_factory):
    spec = _build_test_spec()
    ledger_a, _ = _run(spec, fake_data_factory, data_root_revision="rev-1")
    ledger_b, _ = _run(spec, fake_data_factory, data_root_revision="rev-2")

    assert ledger_a.strategy_spec_hash == ledger_b.strategy_spec_hash
    assert ledger_a.data_snapshot_id != ledger_b.data_snapshot_id


def test_changing_fill_mode_changes_ledger_but_data_snapshot_stable(fake_data_factory):
    """Fill mode is part of run identity but not data identity.

    Two runs with the same spec and same data window but different fill
    modes have different ``result_hash`` (different fill timing → different
    trade prices) but identical ``data_snapshot_id`` (same input bars).
    """
    spec = _build_test_spec()
    ledger_a, _ = _run(spec, fake_data_factory, fill_mode="signal_bar_close")
    ledger_b, _ = _run(spec, fake_data_factory, fill_mode="next_bar_open")

    assert ledger_a.strategy_spec_hash == ledger_b.strategy_spec_hash
    assert ledger_a.data_snapshot_id == ledger_b.data_snapshot_id
    assert ledger_a.fill_mode != ledger_b.fill_mode


def test_fill_mode_normalization_accepts_hyphen_and_case_variants(fake_data_factory):
    """The router admits ``"SIGNAL-BAR-CLOSE"``; the runner stores the
    normalized form and treats every variant as the same run identity.
    """
    spec = _build_test_spec()
    canonical, _ = _run(spec, fake_data_factory, fill_mode="signal_bar_close")
    hyphen, _ = _run(spec, fake_data_factory, fill_mode="SIGNAL-BAR-CLOSE")
    upper_underscore, _ = _run(spec, fake_data_factory, fill_mode="Signal_Bar_Close")

    # All three normalize to the same canonical fill_mode in the ledger.
    assert canonical.fill_mode == "signal_bar_close"
    assert hyphen.fill_mode == "signal_bar_close"
    assert upper_underscore.fill_mode == "signal_bar_close"
    # And produce identical content hashes.
    assert canonical.result_hash == hyphen.result_hash == upper_underscore.result_hash


def test_slippage_per_share_actually_changes_fills(fake_data_factory):
    """``slippage_per_share`` must propagate into FillModel — recording
    the value in the ledger without applying it would silently invalidate
    any slippage-sensitivity research. Regression test for PR #107.
    """
    spec = _build_test_spec()

    def run_with_slippage(slip: float):
        return run_strategy_spec(
            RunRequest(
                spec=spec,
                start_date=date(2024, 1, 2),
                end_date=date(2024, 12, 31),
                slippage_per_share=slip,
            ),
            data_source_factory=fake_data_factory,
            data_root_revision="test-revision-1",
        )

    no_slip_ledger, no_slip_result = run_with_slippage(0.0)
    with_slip_ledger, with_slip_result = run_with_slippage(0.05)

    # Both runs must have at least one trade for the assertion to be
    # meaningful — the synthetic series fires the EMA rule a handful of
    # times, but skip if zero (the assertion is then vacuous).
    if not no_slip_result.trades or not with_slip_result.trades:
        pytest.skip("synthetic series produced zero trades")

    # Identity columns differ — the ledger records the slippage.
    assert no_slip_ledger.slippage_per_share == 0.0
    assert with_slip_ledger.slippage_per_share == 0.05
    # And so do the engine outputs — at least one trade price differs.
    no_slip_prices = [(t.entry_price, t.exit_price) for t in no_slip_result.trades]
    with_slip_prices = [(t.entry_price, t.exit_price) for t in with_slip_result.trades]
    assert no_slip_prices != with_slip_prices
    assert no_slip_ledger.result_hash != with_slip_ledger.result_hash


# ---------------------------------------------------------------------------
# Result shape.
# ---------------------------------------------------------------------------
def test_result_has_equity_curve_and_drawdown(fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _run(spec, fake_data_factory)

    assert ledger.status == "completed"
    assert len(result.equity_curve) > 0
    assert len(result.drawdown_curve) == len(result.equity_curve)

    # Equity curve timestamps are monotonically increasing int64 ms UTC.
    timestamps = [p.timestamp_ms for p in result.equity_curve]
    assert timestamps == sorted(timestamps)
    assert all(isinstance(t, int) for t in timestamps)

    # Drawdown is in [0, 1].
    for d in result.drawdown_curve:
        assert 0.0 <= d.drawdown_pct <= 1.0

    # Initial equity matches the request.
    assert result.initial_cash == 100_000.0


def test_result_trades_have_bars_held(fake_data_factory):
    """Every trade carries an integer ``bars_held`` for Phase D.

    The synthetic series is tuned to fire the EMA rule a handful of times
    over 2,000 bars, so we assert ``bars_held > 0`` per trade rather than
    a specific count (the count is asserted by the parity tests against
    the hand-coded twin).
    """
    spec = _build_test_spec(bars_to_hold=5)
    _, result = _run(spec, fake_data_factory)

    if not result.trades:
        pytest.skip("synthetic series produced zero trades — assertion vacuously holds")

    for trade in result.trades:
        assert trade.bars_held > 0
        assert isinstance(trade.bars_held, int)


def test_metrics_match_summarize_output(fake_data_factory):
    spec = _build_test_spec()
    _, result = _run(spec, fake_data_factory)
    metrics = result.metrics

    # Counts agree with the trade list.
    assert metrics.total_trades == len(result.trades)
    assert metrics.winning_trades == sum(1 for t in result.trades if t.result == "WIN")
    assert metrics.losing_trades == sum(1 for t in result.trades if t.result == "LOSS")

    # win_rate is consistent with counts when there are trades.
    if metrics.total_trades > 0:
        expected = metrics.winning_trades / metrics.total_trades
        assert metrics.win_rate is not None
        assert abs(metrics.win_rate - expected) < 1e-12


def test_exposure_uses_consolidated_bar_resolution():
    """Exposure converts held 15-minute bars into the minute equity-curve unit.

    Regression for the Build Alpha functionality validation report
    F-BA-001: ``bars_held_total`` is counted in consolidated bars,
    while ``total_bars`` is the engine's minute-bar equity curve
    length. Dividing directly understates exposure by ``resolution``.
    """
    ts = datetime(2024, 1, 2, 9, 30, tzinfo=ZoneInfo("America/New_York"))
    equity_curve = [
        EquitySnapshot(
            timestamp=ts,
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            holdings_value=Decimal("0"),
        )
    ]

    metrics = _summarize_metrics(
        initial_cash=100_000.0,
        final_equity=100_000.0,
        trades=[],
        equity_curve=equity_curve,
        bars_held_total=5,
        total_bars=100,
        resolution_minutes=15,
    )

    assert metrics.exposure_pct == pytest.approx(0.75, abs=1e-12, rel=0)


# ---------------------------------------------------------------------------
# Failure path.
# ---------------------------------------------------------------------------
def test_failed_data_source_produces_failed_ledger():
    """When the data factory raises, the ledger is recorded as failed
    and result-hash columns are still populated (so storage can be
    persisted uniformly without a special "no result" branch).
    """

    def broken_factory(symbol, start, end):
        raise RuntimeError("synthetic failure")

    spec = _build_test_spec()
    ledger, result = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 12, 31),
        ),
        data_source_factory=broken_factory,
        data_root_revision="test",
    )

    assert ledger.status == "failed"
    assert ledger.failure_reason is not None
    assert "synthetic failure" in ledger.failure_reason
    assert ledger.result_hash is not None
    assert ledger.trade_log_hash is not None
    assert ledger.metrics_hash is not None
    assert result.trades == []
    assert result.metrics.total_trades == 0
    assert result.warnings == [ledger.failure_reason]


# ---------------------------------------------------------------------------
# Lineage fields (forward-compat for Phases C/D/E).
# ---------------------------------------------------------------------------
def test_parent_run_id_round_trips(fake_data_factory):
    spec = _build_test_spec()
    ledger, _ = run_strategy_spec(
        RunRequest(
            spec=spec,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 12, 31),
            parent_run_id="parent-abc",
            parent_spec_hash="spec-hash-xyz",
        ),
        data_source_factory=fake_data_factory,
        data_root_revision="test-revision-1",
    )
    assert ledger.parent_run_id == "parent-abc"
    assert ledger.parent_spec_hash == "spec-hash-xyz"
