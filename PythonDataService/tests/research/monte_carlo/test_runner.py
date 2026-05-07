"""Monte Carlo runner tests.

Verifies:
  * The runner loads the parent run and only operates on its trade list
    (no engine re-execution).
  * Reshuffle vs resample behave per the architecture-spec contract.
  * Equity bands are P5 ≤ P50 ≤ P95 by construction.
  * Quantile dicts cover {p5, p50, p95}.
  * Breach probabilities are in [0, 1].
  * Same-seed runs produce identical results; different seeds differ.
  * Failed paths (no trades, missing parent, bad inputs) produce
    ``status='failed'`` results with a reason — first-class failure
    records, same as Phase A/C.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest

from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.monte_carlo.runner import MonteCarloRequest, run_monte_carlo
from app.research.runs import RunRequest, run_strategy_spec, save_run


def _build_test_spec() -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": 5, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": 10, "source": "close"},
                {
                    "id": "rsi",
                    "kind": "RSI",
                    "period": 14,
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
                    {"kind": "IndicatorBetween", "indicator": "rsi", "lo": 50, "hi": 70, "inclusive": True},
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 5}],
            },
            "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
        }
    )


@pytest.fixture
def parent_run(tmp_path: Path):
    """Build, run, and persist a parent run with non-trivial trades.

    Returns ``(ledger, result)`` so tests can use ``ledger.run_id`` as
    the MC's ``parent_run_id``.
    """
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    ledger, result = run_strategy_spec(
        RunRequest(
            spec=_build_test_spec(),
            start_date=date(2024, 1, 2),
            end_date=date(2024, 12, 31),
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
    )
    save_run(ledger, result, root=tmp_path)
    return ledger, result


# ---------------------------------------------------------------------------
# Happy paths.
# ---------------------------------------------------------------------------
def test_reshuffle_returns_completed_status(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("synthetic series produced zero trades on parent run")

    config, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="reshuffle",
            simulation_count=100,
            random_seed=42,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "completed"
    assert config.method == "reshuffle"
    assert mc.simulation_count == 100
    assert mc.realised_trade_count == len(result.trades)
    # Equity bands have len(trades) + 1 points (one before any trade,
    # one after each).
    assert len(mc.equity_bands) == len(result.trades) + 1


def test_resample_returns_completed_status(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("synthetic series produced zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="resample",
            simulation_count=100,
            random_seed=7,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "completed"


def test_resample_with_projection_extends_path_length(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("synthetic series produced zero trades on parent run")

    target = len(result.trades) * 3  # 3× historical
    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="resample",
            simulation_count=50,
            projection_trade_count=target,
            random_seed=0,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.realised_trade_count == target
    assert len(mc.equity_bands) == target + 1


# ---------------------------------------------------------------------------
# Quantile invariants.
# ---------------------------------------------------------------------------
def test_equity_bands_satisfy_p5_le_p50_le_p95(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="reshuffle",
            simulation_count=200,
            random_seed=0,
        ),
        artifacts_root=tmp_path,
    )
    for point in mc.equity_bands:
        assert point.p5 <= point.p50 <= point.p95


def test_drawdown_quantiles_satisfy_p5_le_p50_le_p95(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="resample",
            simulation_count=200,
            random_seed=0,
        ),
        artifacts_root=tmp_path,
    )
    q = mc.drawdown_quantiles
    assert q["p5"] <= q["p50"] <= q["p95"]


def test_breach_probabilities_in_unit_interval(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="resample",
            simulation_count=100,
            random_seed=0,
            breach_thresholds=[0.0, 0.05, 0.10, 0.50, 1.0],
        ),
        artifacts_root=tmp_path,
    )
    assert len(mc.breach_probabilities) == 5
    for bp in mc.breach_probabilities:
        assert 0.0 <= bp.probability <= 1.0
    # P(MDD >= 0.0) is by definition 1.0 — every curve has non-negative DD.
    p_zero = next(bp for bp in mc.breach_probabilities if bp.threshold == 0.0)
    assert p_zero.probability == 1.0
    # P(MDD >= 1.0) is essentially 0 unless the strategy bankrupts.
    p_one = next(bp for bp in mc.breach_probabilities if bp.threshold == 1.0)
    assert p_one.probability <= 0.5  # synthetic data shouldn't blow up


# ---------------------------------------------------------------------------
# Determinism.
# ---------------------------------------------------------------------------
def test_same_seed_produces_identical_results(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    request = MonteCarloRequest(
        parent_run_id=ledger.run_id,
        method="resample",
        simulation_count=50,
        random_seed=42,
    )
    _, mc_a = run_monte_carlo(request, artifacts_root=tmp_path)
    _, mc_b = run_monte_carlo(request, artifacts_root=tmp_path)

    # Equity-band quantiles match exactly (numpy is deterministic).
    assert len(mc_a.equity_bands) == len(mc_b.equity_bands)
    for a, b in zip(mc_a.equity_bands, mc_b.equity_bands, strict=True):
        assert a.p5 == b.p5
        assert a.p50 == b.p50
        assert a.p95 == b.p95
    assert mc_a.drawdown_quantiles == mc_b.drawdown_quantiles
    assert mc_a.terminal_pnl_quantiles == mc_b.terminal_pnl_quantiles


def test_different_seeds_produce_different_results(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades or len(result.trades) < 3:
        pytest.skip("need at least 3 trades for permutation diversity")

    base = MonteCarloRequest(
        parent_run_id=ledger.run_id,
        method="reshuffle",
        simulation_count=50,
    )
    _, mc_a = run_monte_carlo(
        MonteCarloRequest(**{**base.__dict__, "random_seed": 1}),
        artifacts_root=tmp_path,
    )
    _, mc_b = run_monte_carlo(
        MonteCarloRequest(**{**base.__dict__, "random_seed": 2}),
        artifacts_root=tmp_path,
    )

    # Some equity-band point at some intermediate index should differ —
    # the start (index 0) and the end (terminal) for *reshuffle* are
    # the same across all simulations because reshuffle preserves the
    # multiset; the intermediate points should have different
    # percentile snapshots.
    if len(result.trades) >= 3:
        intermediate = mc_a.equity_bands[1]
        intermediate_b = mc_b.equity_bands[1]
        # At least one of the three percentiles should differ.
        assert (
            intermediate.p5 != intermediate_b.p5
            or intermediate.p50 != intermediate_b.p50
            or intermediate.p95 != intermediate_b.p95
        )


# ---------------------------------------------------------------------------
# Reshuffle preserves multiset → terminal equity is invariant across sims.
# ---------------------------------------------------------------------------
def test_reshuffle_terminal_equity_is_constant_across_sims(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="reshuffle",
            simulation_count=20,
            random_seed=0,
        ),
        artifacts_root=tmp_path,
    )
    # Reshuffle preserves the multiset of returns ⇒ the compounded
    # terminal equity is identical for every simulation (commutativity
    # of multiplication). So the terminal band has p5 == p50 == p95
    # to floating-point precision.
    last = mc.equity_bands[-1]
    assert last.p5 == pytest.approx(last.p50)
    assert last.p50 == pytest.approx(last.p95)


# ---------------------------------------------------------------------------
# Failure paths.
# ---------------------------------------------------------------------------
def test_missing_parent_run_returns_failed(tmp_path: Path):
    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id="deadbeefdeadbeefdeadbeefdeadbeef",
            method="reshuffle",
            simulation_count=10,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "failed"
    assert "parent run not found" in (mc.failure_reason or "")


def test_malformed_parent_run_id_returns_failed(tmp_path: Path):
    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id="../etc/passwd",
            method="reshuffle",
            simulation_count=10,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "failed"
    assert "parent_run_id rejected" in (mc.failure_reason or "")


def test_reshuffle_with_mismatched_projection_returns_failed(
    tmp_path: Path, parent_run
):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="reshuffle",
            simulation_count=10,
            projection_trade_count=len(result.trades) + 5,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "failed"
    assert "reshuffle requires" in (mc.failure_reason or "")


def test_invalid_breach_threshold_returns_failed(tmp_path: Path, parent_run):
    ledger, _ = parent_run
    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="reshuffle",
            simulation_count=10,
            breach_thresholds=[0.5, 1.5],  # 1.5 is out of [0, 1]
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "failed"
    assert "breach_thresholds" in (mc.failure_reason or "")


# ---------------------------------------------------------------------------
# Warnings (degenerate-but-not-failed inputs).
# ---------------------------------------------------------------------------
def test_low_simulation_count_emits_warning(tmp_path: Path, parent_run):
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    _, mc = run_monte_carlo(
        MonteCarloRequest(
            parent_run_id=ledger.run_id,
            method="reshuffle",
            simulation_count=10,  # well below the "100+" threshold
            random_seed=0,
        ),
        artifacts_root=tmp_path,
    )
    assert mc.status == "completed"
    # Warning surfaced but the run completed.
    assert any("simulation_count=10 is low" in w for w in mc.warnings)


# ---------------------------------------------------------------------------
# Equity-curve compounding sanity check (independent of parent run).
# ---------------------------------------------------------------------------
def test_resample_equity_curve_matches_compound_of_resampled_returns(
    tmp_path: Path, parent_run
):
    """Spot-check: each simulation's equity curve at the last index
    equals ``initial_equity * prod(1 + sampled_returns)``.

    Validated by re-implementing the math against a single seeded
    draw and comparing to ``mc.equity_bands[-1].p50`` — which on a
    single-simulation batch reduces to the curve's own terminal value.
    """
    ledger, result = parent_run
    if not result.trades:
        pytest.skip("zero trades on parent run")

    initial = result.initial_cash
    returns = np.array([t.pnl_pct for t in result.trades])

    # Single-simulation MC so the percentile == that one simulation.
    request = MonteCarloRequest(
        parent_run_id=ledger.run_id,
        method="resample",
        simulation_count=1,
        random_seed=123,
    )
    _, mc = run_monte_carlo(request, artifacts_root=tmp_path)

    # Replay the same draw locally.
    rng = np.random.default_rng(123)
    sampled = returns[
        rng.integers(low=0, high=returns.size, size=mc.realised_trade_count)
    ]
    expected_terminal = initial * float(np.prod(1.0 + sampled))

    assert mc.equity_bands[-1].p50 == pytest.approx(expected_terminal)
