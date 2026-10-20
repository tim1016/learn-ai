"""Contract tests for the generated Strategy Lab analytical-metric catalog."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.research.documentation.analytical_metric_catalog import (
    CATALOG_VERSION,
    LEAN_NATIVE_SHARPE_VARIANT,
    PLATFORM_SHARPE_VARIANT,
    AnalyticalMetricCatalog,
    catalog,
    metric_documentation_context_for_source,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "strategy-lab" / "analytical-metric-catalog-v1.json"
GENERATOR_PATH = REPOSITORY_ROOT / "PythonDataService" / "scripts" / "export_analytical_metric_catalog.py"


def test_catalog_contains_the_two_source_aware_sharpe_variants_and_never_shares_metric_id_and_producer() -> None:
    result = catalog()
    variants = {variant.variant_id: variant for variant in result.variants}

    assert result.catalog_version == CATALOG_VERSION, "the catalog carries CATALOG_VERSION"
    assert len(result.variants) == 136, "the catalog holds 136 variants"
    assert {"sharpe.platform.v1", "sharpe.lean_native.v1"} <= set(variants), (
        "both source-aware Sharpe variants are in the catalog"
    )
    assert all(
        alternative in variants
        for variant in result.variants
        for alternative in variant.alternative_variant_ids
    ), "every alternative_variant_id names a variant in the catalog"
    assert {variant.category for variant in result.variants} == {
        "returns", "risk", "drawdown", "benchmark", "trade_population", "trade_economics",
        "duration", "excursion", "statistical_confidence", "validation_atlas", "runtime_snapshot", "verdict_policy",
    }, "the catalog's categories are exactly the documented set"
    assert PLATFORM_SHARPE_VARIANT.formula_latex is not None, "the platform Sharpe variant carries a latex formula"
    assert LEAN_NATIVE_SHARPE_VARIANT.formula_latex is not None, (
        "the lean-native Sharpe variant carries a latex formula"
    )
    assert PLATFORM_SHARPE_VARIANT.alternative_variant_ids == (LEAN_NATIVE_SHARPE_VARIANT.variant_id,), (
        "the platform Sharpe variant's one alternative is the lean-native variant"
    )
    assert LEAN_NATIVE_SHARPE_VARIANT.alternative_variant_ids == (PLATFORM_SHARPE_VARIANT.variant_id,), (
        "the lean-native Sharpe variant's one alternative is the platform variant"
    )

    # frontend resolveMetricContext() filters by metric_id then .find()s a
    # producer match; two distinct variants sharing (metric_id, producer)
    # means one is unreachable and the other is picked arbitrarily. Portfolio
    # vs. trade Sortino/Sharpe/Win rate previously collapsed to the same
    # semantic metric_id despite being genuinely different calculations.
    seen: dict[tuple[str, str], str] = {}
    collisions: list[str] = []
    for variant in result.variants:
        key = (variant.metric_id, variant.producer)
        if key in seen:
            collisions.append(f"{key} claimed by both {seen[key]} and {variant.variant_id}")
        else:
            seen[key] = variant.variant_id

    assert collisions == [], "no two variants share a (metric_id, producer) pair"


def test_new_run_context_records_the_producer_and_contract() -> None:
    platform = metric_documentation_context_for_source("engine")
    lean = metric_documentation_context_for_source("lean-sidecar")

    assert platform == (
        {
            "metric_id": "sharpe",
            "variant_id": "sharpe.platform.v1",
            "producer": "platform",
            "contract_id": "platform-sharpe-v1",
        },
        {
            "metric_id": "sortino",
            "variant_id": "sortino.platform.v1",
            "producer": "platform",
            "contract_id": "platform-results-statistics-v1",
        },
        {
            "metric_id": "maximum_drawdown",
            "variant_id": "maximum_drawdown.platform.v1",
            "producer": "platform",
            "contract_id": "platform-results-statistics-v1",
        },
        {
            "metric_id": "profit_factor",
            "variant_id": "profit_factor.platform.v1",
            "producer": "platform",
            "contract_id": "platform-results-statistics-v1",
        },
    )
    assert lean == (
        {
            "metric_id": "sharpe",
            "variant_id": "sharpe.lean_native.v1",
            "producer": "lean_native",
            "contract_id": "lean-statistics-oracle-v1",
        },
        {
            "metric_id": "sortino",
            "variant_id": "lean_native.portfolio.sortino_ratio.v1",
            "producer": "lean_native",
            "contract_id": "lean-native-statistics-oracle-v1",
        },
        {
            "metric_id": "maximum_drawdown",
            "variant_id": "lean_native.portfolio.drawdown.v1",
            "producer": "lean_native",
            "contract_id": "lean-native-statistics-oracle-v1",
        },
        {
            "metric_id": "profit_factor",
            "variant_id": "lean_native.trade.profit_factor.v1",
            "producer": "lean_native",
            "contract_id": "lean-native-statistics-oracle-v1",
        },
    )


@pytest.mark.parametrize(
    ("platform_changes", "lean_native_changes", "error"),
    [
        pytest.param(
            (("alternative_variant_ids", ("missing.variant.v1",)),),
            (),
            "missing alternatives",
            id="broken-comparison-references",
        ),
        pytest.param((), (("metric_id", "sortino"),), "same metric_id", id="cross-metric-comparisons"),
        pytest.param((), (("alternative_variant_ids", ()),), "reciprocal", id="one-sided-comparisons"),
    ],
)
def test_catalog_rejects_broken_cross_metric_and_one_sided_comparisons(
    platform_changes: tuple[tuple[str, object], ...],
    lean_native_changes: tuple[tuple[str, object], ...],
    error: str,
) -> None:
    platform = PLATFORM_SHARPE_VARIANT.model_copy(update=dict(platform_changes))
    lean_native = LEAN_NATIVE_SHARPE_VARIANT.model_copy(update=dict(lean_native_changes))

    with pytest.raises(ValueError, match=error):
        AnalyticalMetricCatalog(catalog_version=CATALOG_VERSION, variants=(platform, lean_native))


def test_catalog_rejects_duplicate_variant_ids() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        AnalyticalMetricCatalog(
            catalog_version=CATALOG_VERSION,
            variants=(PLATFORM_SHARPE_VARIANT, PLATFORM_SHARPE_VARIANT),
        )


def test_committed_catalog_matches_deterministic_generator() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "--check"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert payload["catalog_version"] == CATALOG_VERSION
