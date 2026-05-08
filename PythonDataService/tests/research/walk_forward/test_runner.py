"""Walk-forward runner tests.

Exercises the orchestration layer (``run_walk_forward``) against
synthetic bars. Trade-by-trade engine semantics are covered by
Phase A's parity tests; this suite verifies the WF-specific
contract:

  * Each fold's TEST window runs through ``run_strategy_spec``.
  * Every fold's run is persisted under ``tmp_path`` with
    ``parent_run_id = walk_forward_id``.
  * The fold list mirrors the split policy's output exactly.
  * The combined OOS equity curve is compounded across folds.
  * Aggregate metrics (mean / median Sharpe, pct_profitable,
    alpha_decay) are computed only over completed folds.
  * A degenerate split (window too short) produces a
    ``status='failed'`` WF result without raising.
  * Test runs are discoverable via the existing
    ``list_runs(parent_run_id=…)`` filter.
"""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path

import pytest

from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.runs.storage import list_runs
from app.research.walk_forward.runner import WalkForwardRequest, run_walk_forward
from app.research.walk_forward.splits import (
    ChronologicalSplitPolicy,
    RollingSplitPolicy,
)


def _build_test_spec() -> StrategySpec:
    """Same TEST-symbol EMA spec the Phase A in-memory runner uses."""
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
def fake_factory_long():
    """Synthetic data covering ~52 days — enough for rolling splits.

    5000 × 15 min = 75,000 min ≈ 52 calendar days at the synthetic
    cadence (the parity helpers don't gate on session boundaries —
    every 15 min produces a bar including overnight). Enough for a
    rolling split with 10-day train + 5-day test + 5-day step.
    """
    bars = build_minute_bars(closes_for_spy_ema(5000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    return factory


# ---------------------------------------------------------------------------
# Chronological split — single fold, simplest path.
# ---------------------------------------------------------------------------
def test_chronological_split_emits_one_fold(tmp_path: Path, fake_factory_long):
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-15",  # ~6 weeks
        split_policy=ChronologicalSplitPolicy(train_pct=0.6),
    )
    config, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )

    assert result.status == "completed"
    assert result.failure_reason is None
    assert len(result.folds) == 1
    fold = result.folds[0]
    assert fold.fold_index == 0
    # Train precedes test, neither is degenerate.
    assert fold.train_end_ms == fold.test_start_ms
    assert fold.train_start_ms < fold.train_end_ms
    assert fold.test_start_ms < fold.test_end_ms

    # The fold's test run was persisted.
    assert (tmp_path / fold.test_run_id / "ledger.json").is_file()
    assert (tmp_path / fold.test_run_id / "result.json").is_file()

    # And linked back to the WF.
    listed = list_runs(root=tmp_path, parent_run_id=config.walk_forward_id)
    assert [lg.run_id for lg in listed] == [fold.test_run_id]


def test_chronological_split_records_correct_split_policy(
    tmp_path: Path, fake_factory_long
):
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-15",
        split_policy=ChronologicalSplitPolicy(train_pct=0.5),
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )
    assert result.split_policy.kind == "chronological"
    # ConfigDict(extra="allow") preserves the policy-specific field.
    assert result.split_policy.model_dump()["train_pct"] == 0.5


# ---------------------------------------------------------------------------
# Rolling split — multi-fold, exercises aggregation.
# ---------------------------------------------------------------------------
def test_rolling_split_emits_multiple_folds(tmp_path: Path, fake_factory_long):
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-22",  # ~50 days
        split_policy=RollingSplitPolicy(train_days=10, test_days=5, step_days=5),
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )

    assert result.status == "completed"
    # 50 days / step 5 with 15-day fold span: ~7-8 folds.
    assert len(result.folds) >= 5

    # Fold indices are sequential, starting at 0.
    assert [f.fold_index for f in result.folds] == list(range(len(result.folds)))

    # Each fold has a unique run_id.
    run_ids = [f.test_run_id for f in result.folds]
    assert len(set(run_ids)) == len(run_ids)


def test_rolling_aggregates_only_count_completed_folds(
    tmp_path: Path, fake_factory_long
):
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-22",
        split_policy=RollingSplitPolicy(train_days=10, test_days=5, step_days=5),
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )

    # ``pct_profitable_folds`` is a fraction in [0, 1] when at least one
    # fold ran (regardless of trade count). ``mean_oos_sharpe`` may be
    # None on synthetic data with too few trades.
    assert result.pct_profitable_folds is not None
    assert 0.0 <= result.pct_profitable_folds <= 1.0


def test_oos_retention_uses_parent_sharpe(tmp_path: Path, fake_factory_long):
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-22",
        split_policy=RollingSplitPolicy(train_days=10, test_days=5, step_days=5),
        parent_run_id="abcabcabcabcabcabcabcabcabcabcab",
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
        parent_sharpe=2.0,
    )

    if result.mean_oos_sharpe is None:
        pytest.skip("synthetic series produced no finite fold Sharpe")

    assert result.oos_retention == pytest.approx(
        result.mean_oos_sharpe / 2.0,
        abs=1e-12,
        rel=0,
    )


# ---------------------------------------------------------------------------
# Combined OOS curve — compounded across folds.
# ---------------------------------------------------------------------------
def test_combined_oos_curve_is_monotonically_concatenated(
    tmp_path: Path, fake_factory_long
):
    """Compounded curve: each fold's start equity equals the previous
    fold's end equity. Timestamps should be monotonically non-decreasing
    across the concatenated curve.
    """
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-22",
        split_policy=RollingSplitPolicy(train_days=10, test_days=5, step_days=5),
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )
    if not result.combined_oos_equity_curve:
        pytest.skip("synthetic series produced empty fold curves")

    timestamps = [p.timestamp_ms for p in result.combined_oos_equity_curve]
    # Compounded curve is the concatenation of fold curves; within a
    # fold the timestamps are strictly increasing, but at fold
    # boundaries the next fold may start at exactly the previous
    # fold's last timestamp + 1 minute. Just verify non-decreasing.
    for prev, cur in itertools.pairwise(timestamps):
        assert cur >= prev, f"timestamps not monotonic at {prev} → {cur}"


# ---------------------------------------------------------------------------
# Failure paths.
# ---------------------------------------------------------------------------
def test_window_too_short_for_split_returns_failed_result(
    tmp_path: Path, fake_factory_long
):
    """A window that can't fit even one fold produces a failed-status
    WF result, NOT an exception. Storage layer can persist the failure
    uniformly with successes (matches Phase A's failed-run contract).
    """
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-01-05",  # 3 days
        split_policy=RollingSplitPolicy(train_days=30, test_days=15, step_days=7),
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )
    assert result.status == "failed"
    assert result.failure_reason is not None
    assert "too short" in result.failure_reason
    assert result.folds == []
    assert result.combined_oos_equity_curve == []
    assert result.warnings == [result.failure_reason]


# ---------------------------------------------------------------------------
# Lineage.
# ---------------------------------------------------------------------------
def test_parent_run_id_is_recorded_on_walk_forward_result(
    tmp_path: Path, fake_factory_long
):
    """When a parent run is supplied, the WF and its folds inherit it.
    The folds' parent_run_id is the WF id (not the user-supplied
    parent), which is the right shape for "give me every fold of this
    WF" via list_runs.
    """
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-15",
        split_policy=ChronologicalSplitPolicy(),
        parent_run_id="abcabcabcabcabcabcabcabcabcabcab",  # not validated; informational
    )
    config, result = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )
    # The WF carries the user-supplied parent.
    assert config.parent_run_id == "abcabcabcabcabcabcabcabcabcabcab"
    assert result.parent_run_id == "abcabcabcabcabcabcabcabcabcabcab"

    # Folds carry the WF id as their parent (that's what lets
    # ``list_runs(parent_run_id=wf_id)`` return them).
    fold_ledgers = list_runs(root=tmp_path, parent_run_id=config.walk_forward_id)
    assert len(fold_ledgers) == 1


def test_failed_folds_are_excluded_from_pct_profitable_folds(
    tmp_path: Path, fake_factory_long
):
    """``pct_profitable_folds`` denominator must exclude folds whose
    underlying ``RunLedger`` is ``failed``. A WF with 3 successful
    folds and 2 failed ones should not report a 0/5 = 0% profitable
    rate when 2 of the 3 *successful* folds were profitable. Regression
    test for PR #110 review.
    """
    bars = build_minute_bars(closes_for_spy_ema(5000))
    fail_call_count = {"n": 0}

    def factory(symbol: str, start: date, end: date):
        # Make every other fold fail by raising at the data-source
        # boundary. ``run_strategy_spec`` catches the exception and
        # produces a failed-status ledger — the WF runner then records
        # the fold with ``status='failed'``.
        fail_call_count["n"] += 1
        if fail_call_count["n"] % 2 == 0:
            raise RuntimeError("synthetic factory failure for fold-N")
        return FakeDataReader(bars=bars)

    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-22",
        split_policy=RollingSplitPolicy(train_days=10, test_days=5, step_days=5),
    )
    _, result = run_walk_forward(
        request,
        data_source_factory=factory,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )

    failed = [f for f in result.folds if f.status == "failed"]
    successful = [f for f in result.folds if f.status == "completed"]
    assert len(failed) > 0, "test setup did not produce any failed folds"
    assert len(successful) > 0, "test setup did not produce any successful folds"

    # The denominator must be the successful-fold count, not the total.
    profitable_count = sum(
        1 for f in successful if f.test_metrics.total_return_pct > 0
    )
    expected = profitable_count / len(successful)
    if result.pct_profitable_folds is None:
        # Only happens when ``successful`` is empty, which we asserted
        # is non-empty above. Promoting to a hard fail here surfaces a
        # regression rather than a vacuous skip.
        pytest.fail("pct_profitable_folds was None despite successful folds")
    assert result.pct_profitable_folds == expected


def test_repeat_walk_forward_runs_have_distinct_walk_forward_ids(
    tmp_path: Path, fake_factory_long
):
    request = WalkForwardRequest(
        spec=_build_test_spec(),
        start_date="2024-01-02",
        end_date="2024-02-15",
        split_policy=ChronologicalSplitPolicy(),
    )
    config1, _ = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )
    config2, _ = run_walk_forward(
        request,
        data_source_factory=fake_factory_long,
        artifacts_root=tmp_path,
        data_root_revision="test-rev",
    )
    assert config1.walk_forward_id != config2.walk_forward_id
    # And both produce the same spec hash — the spec didn't change.
    assert config1.strategy_spec_hash == config2.strategy_spec_hash
