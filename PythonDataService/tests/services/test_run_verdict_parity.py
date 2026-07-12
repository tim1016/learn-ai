from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.services.run_verdict_service import compute_run_verdict

FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "golden" / "run-verdict-v1" / "fixture.json"


def _dimension_scores(verdict: Any) -> dict[str, int | None]:
    return {dimension.key: dimension.score for dimension in verdict.dimensions}


def _sub_scores(verdict: Any) -> dict[str, dict[str, int | None]]:
    return {
        dimension.key: {sub_score.key: sub_score.score for sub_score in dimension.sub_scores}
        for dimension in verdict.dimensions
    }


@pytest.mark.parametrize("case", json.loads(FIXTURE_PATH.read_text())["cases"], ids=lambda c: c["id"])
def test_compute_run_verdict_matches_golden_fixture(case: dict[str, Any]) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())

    verdict = compute_run_verdict(
        case["input"],
        engine=case["engine"],
        generated_at_ms=fixture["generated_at_ms"],
    )
    expected = case["expected"]

    assert verdict.verdict_version == expected["verdict_version"]
    assert verdict.generated_at_ms == fixture["generated_at_ms"]
    assert verdict.engine == case["engine"]
    assert verdict.composite == expected["composite"]
    assert verdict.grade == expected["grade"]
    assert verdict.signal == expected["signal"]
    assert verdict.headline == expected["headline"]
    assert len(verdict.missing_metrics) == expected["missing_metrics_count"]
    assert verdict.normalized_weights is expected["normalized_weights"]
    assert _dimension_scores(verdict) == expected["dimension_scores"]
    assert _sub_scores(verdict) == expected["sub_scores"]


def test_compute_run_verdict_grades_infinite_profit_factor_as_unavailable_edge() -> None:
    verdict = compute_run_verdict(
        {
            "statistics": {"profit_factor": "Infinity"},
            "total_trades": 30,
            "net_profit": 1000,
            "total_fees": 50,
        },
        engine="python",
        generated_at_ms=1_700_000_000_000,
    )

    trade_edge = next(d for d in verdict.dimensions if d.key == "trade_edge")
    profit_factor = next(s for s in trade_edge.sub_scores if s.key == "pf")
    assert profit_factor.score == 10
    assert profit_factor.display == "∞"


def test_lean_unclean_run_forces_rework_signal() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text())
    case = next(c for c in fixture["cases"] if c["id"] == "strong_python")

    verdict = compute_run_verdict(
        case["input"],
        engine="lean",
        generated_at_ms=fixture["generated_at_ms"],
        cleanliness={
            "is_clean": False,
            "is_reconciliation_grade": True,
            "error_counts": {"runtime_error": 1},
        },
    )

    assert verdict.signal == "Rework"
    assert "lean_run_not_clean" in verdict.red_flags
    assert verdict.cleanliness is not None
    assert verdict.cleanliness.error_counts == {"runtime_error": 1}
