from __future__ import annotations

import pytest

pytest.importorskip("app.research.documentation.analytical_metric_catalog")

from app.research.documentation.analytical_metric_catalog_results_entries import (
    PERFORMANCE_MEMORY_VARIANTS,
    PLATFORM_HEADLINE_VARIANTS,
    RESULTS_CATALOG_VARIANTS,
    VERDICT_POLICY_VARIANTS,
)
from app.services.run_verdict_service import VERDICT_POLICY_DOCUMENTATION


def test_results_catalog_covers_all_frozen_verdict_inputs_as_policy_concepts() -> None:
    expected_policy_ids = {
        "verdict_policy.sharpe.v2",
        "verdict_policy.sortino.v2",
        "verdict_policy.cagr.v2",
        "verdict_policy.calmar.v2",
        "verdict_policy.annual_volatility.v2",
        "verdict_policy.maximum_drawdown.v2",
        "verdict_policy.recovery_duration.v2",
        "verdict_policy.max_consecutive_losers.v2",
        "verdict_policy.profit_factor.v2",
        "verdict_policy.expectancy.v2",
        "verdict_policy.win_rate.v2",
        "verdict_policy.payoff_ratio.v2",
        "verdict_policy.fee_drag.v2",
        "verdict_policy.probabilistic_sharpe.v2",
        "verdict_policy.sample_size.v2",
        "verdict_policy.skepticism_penalty.v2",
        "verdict_policy.trade_portfolio_sharpe_gap.v2",
    }

    assert {entry.variant_id for entry in VERDICT_POLICY_VARIANTS} == expected_policy_ids
    assert all(entry.producer == "verdict_policy" for entry in VERDICT_POLICY_VARIANTS)
    assert all(entry.verdict_membership for entry in VERDICT_POLICY_VARIANTS)
    assert all("does not redefine the underlying metric" in entry.definition for entry in VERDICT_POLICY_VARIANTS)


def test_results_catalog_preserves_sortino_unavailability_and_full_run_projection_distinctions() -> None:
    entries = {entry.variant_id: entry for entry in PLATFORM_HEADLINE_VARIANTS}

    expected_platform_ids = {
        "net_pnl.platform.v1", "initial_cash.platform.v1", "final_equity.platform.v1", "total_fees.platform.v1",
        "completed_trades.platform.v1", "winning_trades.platform.v1", "losing_trades.platform.v1",
        "profit_factor.platform.v1", "expectancy.platform.v1", "payoff_ratio.platform.v1", "win_rate.platform.v1",
        "sortino.platform.v1", "maximum_drawdown.platform.v1", "cagr.platform.v1", "calmar.platform.v1",
        "annual_volatility.platform.v1", "recovery_duration.platform.v1", "max_consecutive_losers.platform.v1",
        "fee_drag.platform.v1", "probabilistic_sharpe.platform.v1", "sample_size.platform.v1",
        "skepticism_penalty.platform.v1", "trade_portfolio_sharpe_gap.platform.v1", "full_run_totals.platform.v1",
        "recent_trade_ledger.platform.v1", "realized_equity.platform.v1", "risk_statistic_input_curve.platform.v1",
    }

    assert set(entries) == expected_platform_ids
    sortino = entries["sortino.platform.v1"]
    assert {state.state for state in sortino.value_states} == {"zero", "unavailable"}
    assert "not displayed or scored as zero" in sortino.value_states[1].scoring_behavior
    assert "authoritative full-run ledger" in entries["completed_trades.platform.v1"].input_series.lower()
    assert "recent up to 500 trades" in entries["recent_trade_ledger.platform.v1"].definition
    assert "can differ" in entries["risk_statistic_input_curve.platform.v1"].definition
    assert {state.state for state in entries["profit_factor.platform.v1"].value_states} == {"zero", "infinite", "unavailable"}
    assert {state.state for state in entries["cagr.platform.v1"].value_states} == {"zero", "undefined", "unavailable"}
    assert entries["realized_equity.platform.v1"].canonical_symbol.endswith("equity_downsample.py::build_realized_equity_envelope")
    assert entries["realized_equity.platform.v1"].fixture_or_receipt == "PythonDataService/tests/fixtures/golden/engine-results/ENG-006/v1/"
    assert entries["initial_cash.platform.v1"].canonical_symbol.endswith("StrategyExecution.cs::InitialCash")
    assert entries["final_equity.platform.v1"].canonical_symbol.endswith("StrategyExecution.cs::FinalEquity")


def test_performance_memory_entries_cover_horizon_timing_seasonality_and_overlapping_rolls() -> None:
    expected_ids = {
        "trailing_horizon_return.validation_analytics.v1",
        "trailing_horizon_coverage.validation_analytics.v1",
        "trailing_horizon_trade_count.validation_analytics.v1",
        "trailing_horizon_win_rate.validation_analytics.v1",
        "trailing_horizon_profit_factor.validation_analytics.v1",
        "timing_cell_trade_count.validation_analytics.v1",
        "timing_cell_win_rate.validation_analytics.v1",
        "timing_cell_average_return.validation_analytics.v1",
        "calendar_month_observation_count.validation_analytics.v1",
        "calendar_month_median_compounded_return.validation_analytics.v1",
        "rolling_trade_average_return.validation_analytics.v1",
        "rolling_trade_win_rate.validation_analytics.v1",
    }

    entries = {entry.variant_id: entry for entry in PERFORMANCE_MEMORY_VARIANTS}
    assert set(entries) == expected_ids
    assert "America/New_York" in entries["timing_cell_average_return.validation_analytics.v1"].input_series
    assert "overlapping trailing windows" in entries["rolling_trade_win_rate.validation_analytics.v1"].input_series
    assert "full horizon" in entries["trailing_horizon_return.validation_analytics.v1"].definition


def test_results_catalog_variant_ids_are_unique() -> None:
    variant_ids = [str(entry["variant_id"]) for entry in RESULTS_CATALOG_VARIANTS]

    assert len(variant_ids) == len(set(variant_ids))


def test_policy_entries_derive_their_threshold_prose_from_the_scorer_owned_descriptor() -> None:
    policy_by_id = {entry.variant_id: entry for entry in VERDICT_POLICY_VARIANTS}

    assert set(VERDICT_POLICY_DOCUMENTATION) == {
        variant_id.removeprefix("verdict_policy.").removesuffix(".v2")
        for variant_id in policy_by_id
    }
    for key, description in VERDICT_POLICY_DOCUMENTATION.items():
        assert description in policy_by_id[f"verdict_policy.{key}.v2"].definition
