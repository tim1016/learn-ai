"""Results-domain coverage tests for the analytical metric catalog shard."""

from __future__ import annotations

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


def test_platform_headline_entries_carry_their_own_authored_category() -> None:
    # Categories are authored explicitly per entry, not inferred from prose
    # substrings -- an editorial change to a definition must not silently
    # relocate a metric (net_pnl's "after recorded fees" used to false-match
    # trade_economics; max_consecutive_losers' "completed trades" -- with a
    # space, unlike the "completed_trades" token -- fell through to returns).
    expected_categories = {
        "net_pnl": "returns",
        "initial_cash": "returns",
        "final_equity": "returns",
        "total_fees": "trade_economics",
        "completed_trades": "trade_population",
        "winning_trades": "trade_population",
        "losing_trades": "trade_population",
        "profit_factor": "trade_economics",
        "expectancy": "trade_economics",
        "payoff_ratio": "trade_economics",
        "win_rate": "trade_population",
        "sortino": "statistical_confidence",
        "maximum_drawdown": "drawdown",
        "cagr": "returns",
        "calmar": "drawdown",
        "annual_volatility": "statistical_confidence",
        "recovery_duration": "drawdown",
        "max_consecutive_losers": "risk",
        "fee_drag": "trade_economics",
        "probabilistic_sharpe": "statistical_confidence",
        "sample_size": "trade_population",
        "skepticism_penalty": "trade_population",
        "trade_portfolio_sharpe_gap": "statistical_confidence",
        "full_run_totals": "returns",
        "recent_trade_ledger": "returns",
        "realized_equity": "returns",
        "risk_statistic_input_curve": "risk",
    }

    entries = {entry.metric_id: entry for entry in PLATFORM_HEADLINE_VARIANTS}
    assert set(entries) == set(expected_categories)
    for metric_id, category in expected_categories.items():
        assert entries[metric_id].category == category, metric_id


def test_platform_headline_fixture_receipt_is_only_claimed_for_covered_metrics() -> None:
    # strategy-metric-help-golden-v1.json only asserts these 7 platform
    # concepts (see tests/fixtures/test_strategy_metric_help_golden.py);
    # citing it for e.g. total_fees or cagr would claim a receipt the fixture
    # doesn't actually cover.
    golden_covered = {"net_pnl", "profit_factor", "expectancy", "sortino", "maximum_drawdown", "win_rate", "completed_trades"}
    entries = {entry.metric_id: entry for entry in PLATFORM_HEADLINE_VARIANTS}

    for metric_id, entry in entries.items():
        if metric_id in golden_covered:
            assert entry.fixture_or_receipt == "contracts/fixtures/strategy-metric-help-golden-v1.json", metric_id
        elif entry.canonical_symbol.startswith(("Backend/", "PythonDataService/app/engine/results/equity_downsample.py")) or any(
            marker in entry.canonical_symbol for marker in ("engine_validation_analytics.py", "run_verdict_service.py")
        ):
            continue  # These branches have their own dedicated receipts, asserted elsewhere.
        else:
            assert entry.fixture_or_receipt is None, metric_id


def test_verdict_policy_input_series_is_prose_not_a_bare_variant_id() -> None:
    for entry in VERDICT_POLICY_VARIANTS:
        assert " " in entry.input_series, entry.variant_id
        assert not entry.input_series.endswith(".platform.v1"), entry.variant_id


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
        "rolling_daily_sharpe.validation_analytics.v1",
        "cumulative_pnl_divergence.validation_analytics.v1",
        "divergence_observation_share.validation_analytics.v1",
        "longest_divergence_streak.validation_analytics.v1",
    }

    entries = {entry.variant_id: entry for entry in PERFORMANCE_MEMORY_VARIANTS}
    assert set(entries) == expected_ids
    assert "America/New_York" in entries["timing_cell_average_return.validation_analytics.v1"].input_series
    assert "overlapping trailing windows" in entries["rolling_trade_win_rate.validation_analytics.v1"].input_series
    assert "full horizon" in entries["trailing_horizon_return.validation_analytics.v1"].definition
    assert "do not treat it as a trading signal" in entries["cumulative_pnl_divergence.validation_analytics.v1"].interpretation


def test_results_catalog_variant_ids_are_unique() -> None:
    variant_ids = [str(entry["variant_id"]) for entry in RESULTS_CATALOG_VARIANTS]

    assert len(variant_ids) == len(set(variant_ids))


def test_results_catalog_has_specific_trader_guidance_for_every_quantity() -> None:
    generic = "Read this value together with its producer, input evidence, and any unavailable state."

    for entry in (*PLATFORM_HEADLINE_VARIANTS, *VERDICT_POLICY_VARIANTS, *PERFORMANCE_MEMORY_VARIANTS):
        assert entry.interpretation != generic, entry.variant_id
        assert len(entry.interpretation) >= 24, entry.variant_id
        assert entry.common_misreadings, entry.variant_id


def test_policy_entries_derive_their_threshold_prose_from_the_scorer_owned_descriptor() -> None:
    policy_by_id = {entry.variant_id: entry for entry in VERDICT_POLICY_VARIANTS}

    assert set(VERDICT_POLICY_DOCUMENTATION) == {
        variant_id.removeprefix("verdict_policy.").removesuffix(".v2")
        for variant_id in policy_by_id
    }
    for key, description in VERDICT_POLICY_DOCUMENTATION.items():
        assert description in policy_by_id[f"verdict_policy.{key}.v2"].definition
