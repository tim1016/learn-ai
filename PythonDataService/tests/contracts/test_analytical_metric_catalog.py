"""Contract tests for the generated Strategy Lab analytical-metric catalog."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.research.documentation.analytical_metric_catalog import (
    CATALOG_VERSION,
    LEAN_NATIVE_SHARPE_VARIANT,
    PLATFORM_SHARPE_VARIANT,
    catalog,
    metric_documentation_context_for_source,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPOSITORY_ROOT / "contracts" / "strategy-lab" / "analytical-metric-catalog-v1.json"
GENERATOR_PATH = REPOSITORY_ROOT / "PythonDataService" / "scripts" / "export_analytical_metric_catalog.py"


def test_catalog_contains_the_two_source_aware_sharpe_variants() -> None:
    result = catalog()

    assert result.catalog_version == CATALOG_VERSION
    assert [variant.variant_id for variant in result.variants] == [
        "sharpe.platform.v1",
        "sharpe.lean_native.v1",
    ]
    assert PLATFORM_SHARPE_VARIANT.formula_latex is not None
    assert LEAN_NATIVE_SHARPE_VARIANT.formula_latex is not None
    assert PLATFORM_SHARPE_VARIANT.alternative_variant_ids == (LEAN_NATIVE_SHARPE_VARIANT.variant_id,)
    assert LEAN_NATIVE_SHARPE_VARIANT.alternative_variant_ids == (PLATFORM_SHARPE_VARIANT.variant_id,)


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


def test_committed_catalog_matches_deterministic_generator() -> None:
    result = subprocess.run(
        [sys.executable, str(GENERATOR_PATH), "--check"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert payload["catalog_version"] == CATALOG_VERSION
