"""Results-domain coverage tests for the analytical metric catalog shard."""

from __future__ import annotations

from app.research.documentation.analytical_metric_catalog_results_entries import (
    PLATFORM_HEADLINE_VARIANTS,
    RESULTS_CATALOG_VARIANTS,
)


def test_platform_headline_fixture_receipt_is_only_claimed_for_covered_metrics() -> None:
    # strategy-metric-help-golden-v2.json only asserts these 7 platform
    # concepts (see tests/fixtures/test_strategy_metric_help_golden.py);
    # citing it for e.g. total_fees or cagr would claim a receipt the fixture
    # doesn't actually cover.
    golden_covered = {"net_pnl", "profit_factor", "expectancy", "sortino", "maximum_drawdown", "win_rate", "completed_trades"}
    entries = {entry.metric_id: entry for entry in PLATFORM_HEADLINE_VARIANTS}

    for metric_id, entry in entries.items():
        if metric_id in golden_covered:
            assert entry.fixture_or_receipt == "contracts/fixtures/strategy-metric-help-golden-v2.json", metric_id
        elif entry.canonical_symbol.startswith(("Backend/", "PythonDataService/app/engine/results/equity_downsample.py")) or any(
            marker in entry.canonical_symbol for marker in ("engine_validation_analytics.py", "run_verdict_service.py")
        ):
            continue  # These branches have their own dedicated receipts, asserted elsewhere.
        else:
            assert entry.fixture_or_receipt is None, metric_id


def test_results_catalog_variant_ids_are_unique() -> None:
    variant_ids = [str(entry["variant_id"]) for entry in RESULTS_CATALOG_VARIANTS]

    assert len(variant_ids) == len(set(variant_ids))
