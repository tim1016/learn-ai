"""Golden LEAN-native statistics parity tests."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.results.lean_statistics import (
    format_lean_statistics_summary,
    reproduce_lean_total_performance,
)
from app.lean_sidecar.config import PINNED_LEAN_RUNTIME_PROVENANCE_ARM64
from app.lean_sidecar.normalized_parser import parse_workspace
from app.lean_sidecar.workspace import Workspace
from app.services.lean_sidecar_persistence import (
    NormalizedResult as PersistenceNormalizedResult,
)
from app.services.lean_sidecar_persistence import (
    _lean_native_parity_envelope,
)

FIXTURE = Path(__file__).parent / "fixtures" / "golden" / "lean-statistics-oracle-v1"
ABSOLUTE_TOLERANCE = 5.00001e-5


def _normalized() -> dict:
    workspace_root = FIXTURE.resolve()
    workspace = Workspace(
        run_id="lean-statistics-oracle-v1",
        artifacts_root=workspace_root.parent,
        root=workspace_root,
    )
    return parse_workspace(workspace).model_dump(mode="python")


def _reproduced(normalized: dict) -> dict:
    return reproduce_lean_total_performance(
        normalized,
        interest_rate_csv=FIXTURE / "workspace" / "data" / "alternative" / "interest-rate" / "usa" / "interest-rate.csv",
    )


def test_oracle_artifact_hashes_match_manifest() -> None:
    manifest = json.loads((FIXTURE / "manifest.json").read_text())
    assert manifest["lean_source_commit"] == "261366a7e26ae942df858ab20df4fef8fa07de67"
    assert manifest["lean_image_digest"] == PINNED_LEAN_RUNTIME_PROVENANCE_ARM64.image_digest
    for relative_path, receipt in manifest["artifacts"].items():
        path = FIXTURE / relative_path
        assert path.stat().st_size == receipt["size_bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt["sha256"]


def test_every_native_portfolio_and_trade_metric_matches_oracle() -> None:
    normalized = _normalized()
    reproduced = _reproduced(normalized)
    oracle = normalized["total_performance"]

    assert set(reproduced["portfolioStatistics"]) == set(oracle["portfolioStatistics"])
    oracle_trade_keys = set(oracle["tradeStatistics"])
    assert set(reproduced["tradeStatistics"]) == oracle_trade_keys

    for section in ("portfolioStatistics", "tradeStatistics"):
        for key, actual in reproduced[section].items():
            expected = oracle[section][key]
            try:
                assert float(actual) == pytest.approx(float(expected), abs=ABSOLUTE_TOLERANCE), (
                    section,
                    key,
                    actual,
                    expected,
                )
            except (TypeError, ValueError):
                assert str(actual) == str(expected), (section, key, actual, expected)


def test_reproduction_does_not_read_oracle_statistic_answers() -> None:
    normalized = _normalized()
    expected = _reproduced(normalized)
    poisoned = copy.deepcopy(normalized)
    poisoned["total_performance"]["portfolioStatistics"] = {"sharpeRatio": "999"}
    poisoned["total_performance"]["tradeStatistics"] = {"profitFactor": "999"}

    assert _reproduced(poisoned) == expected


def test_formatted_lean_dashboard_strings_match_oracle() -> None:
    normalized = _normalized()
    reproduced = _reproduced(normalized)
    formatted = format_lean_statistics_summary(
        reproduced,
        total_orders=normalized["total_orders"],
        total_fees=Decimal(reproduced["tradeStatistics"]["totalFees"]),
    )

    assert formatted == {key: normalized["statistics"][key] for key in formatted}


def test_formatted_lean_dashboard_preserves_native_trailing_zero_scale() -> None:
    reproduced = _reproduced(_normalized())
    reproduced["portfolioStatistics"]["expectancy"] = Decimal("-0.4601")
    reproduced["portfolioStatistics"]["totalNetProfit"] = Decimal("-0.0117")

    formatted = format_lean_statistics_summary(
        reproduced,
        total_orders=10,
        total_fees=Decimal("10"),
    )

    assert formatted["Expectancy"] == "-0.460"
    assert formatted["Net Profit"] == "-1.170%"
    assert formatted["Total Fees"] == "$10.00"


def test_persisted_native_parity_receipt_is_a_complete_match(tmp_path: Path) -> None:
    normalized = _normalized()
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(normalized, default=str), encoding="utf-8")
    persistence_result = PersistenceNormalizedResult.from_path(result_path)

    receipt = _lean_native_parity_envelope(persistence_result, FIXTURE)

    assert receipt["status"] == "match"
    assert receipt["native_metric_count"] == 66
    assert receipt["formatted_metric_count"] == 25
    assert receipt["divergences"] == []
    assert receipt["source_commit"] == "261366a7e26ae942df858ab20df4fef8fa07de67"
