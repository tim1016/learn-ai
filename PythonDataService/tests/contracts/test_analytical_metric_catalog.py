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


def test_catalog_contains_the_two_source_aware_sharpe_variants() -> None:
    result = catalog()
    variants = {variant.variant_id: variant for variant in result.variants}

    assert result.catalog_version == CATALOG_VERSION
    assert len(result.variants) == 132
    assert {"sharpe.platform.v1", "sharpe.lean_native.v1"} <= set(variants)
    assert all(
        alternative in variants
        for variant in result.variants
        for alternative in variant.alternative_variant_ids
    )
    assert {variant.category for variant in result.variants} == {
        "returns", "risk", "drawdown", "benchmark", "trade_population", "trade_economics",
        "duration", "excursion", "statistical_confidence", "validation_atlas", "runtime_snapshot", "verdict_policy",
    }
    assert PLATFORM_SHARPE_VARIANT.formula_latex is not None
    assert LEAN_NATIVE_SHARPE_VARIANT.formula_latex is not None
    assert PLATFORM_SHARPE_VARIANT.alternative_variant_ids == (LEAN_NATIVE_SHARPE_VARIANT.variant_id,)
    assert LEAN_NATIVE_SHARPE_VARIANT.alternative_variant_ids == (PLATFORM_SHARPE_VARIANT.variant_id,)


def test_catalog_never_lets_two_variants_share_metric_id_and_producer() -> None:
    # frontend resolveMetricContext() filters by metric_id then .find()s a
    # producer match; two distinct variants sharing (metric_id, producer)
    # means one is unreachable and the other is picked arbitrarily. Portfolio
    # vs. trade Sortino/Sharpe/Win rate previously collapsed to the same
    # semantic metric_id despite being genuinely different calculations.
    result = catalog()
    seen: dict[tuple[str, str], str] = {}
    collisions: list[str] = []
    for variant in result.variants:
        key = (variant.metric_id, variant.producer)
        if key in seen:
            collisions.append(f"{key} claimed by both {seen[key]} and {variant.variant_id}")
        else:
            seen[key] = variant.variant_id

    assert collisions == []


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
    )
    assert lean == (
        {
            "metric_id": "sharpe",
            "variant_id": "sharpe.lean_native.v1",
            "producer": "lean_native",
            "contract_id": "lean-statistics-oracle-v1",
        },
    )


def test_catalog_rejects_broken_comparison_references() -> None:
    broken_platform = PLATFORM_SHARPE_VARIANT.model_copy(
        update={"alternative_variant_ids": ("missing.variant.v1",)},
    )

    with pytest.raises(ValueError, match="missing alternatives"):
        AnalyticalMetricCatalog(catalog_version=CATALOG_VERSION, variants=(broken_platform, LEAN_NATIVE_SHARPE_VARIANT))


def test_catalog_rejects_duplicate_variant_ids() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        AnalyticalMetricCatalog(
            catalog_version=CATALOG_VERSION,
            variants=(PLATFORM_SHARPE_VARIANT, PLATFORM_SHARPE_VARIANT),
        )


def test_catalog_rejects_cross_metric_comparisons() -> None:
    foreign = LEAN_NATIVE_SHARPE_VARIANT.model_copy(update={"metric_id": "sortino"})

    with pytest.raises(ValueError, match="same metric_id"):
        AnalyticalMetricCatalog(
            catalog_version=CATALOG_VERSION,
            variants=(PLATFORM_SHARPE_VARIANT, foreign),
        )


def test_catalog_rejects_one_sided_comparisons() -> None:
    one_sided = LEAN_NATIVE_SHARPE_VARIANT.model_copy(update={"alternative_variant_ids": ()})

    with pytest.raises(ValueError, match="reciprocal"):
        AnalyticalMetricCatalog(
            catalog_version=CATALOG_VERSION,
            variants=(PLATFORM_SHARPE_VARIANT, one_sided),
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
