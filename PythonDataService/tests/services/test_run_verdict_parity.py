from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.services.run_verdict_service import compute_run_verdict, failed_run_verdict

V1_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "golden" / "run-verdict-v1" / "fixture.json"
V2_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "golden" / "run-verdict-v2" / "fixture.json"


def _dimension_scores(verdict: Any) -> dict[str, int | None]:
    return {dimension.key: dimension.score for dimension in verdict.dimensions}


def _sub_scores(verdict: Any) -> dict[str, dict[str, int | None]]:
    return {
        dimension.key: {sub_score.key: sub_score.score for sub_score in dimension.sub_scores}
        for dimension in verdict.dimensions
    }


def _canonicalized_v1_input(case: dict[str, Any]) -> dict[str, Any]:
    """Move legacy fixture values into v2's platform-metric namespace.

    The legacy fixture still proves the published threshold buckets. It must
    not prove that v2 accepts LEAN-native fields as readiness inputs.
    """
    payload = deepcopy(case["input"])
    stats = payload.setdefault("statistics", {})
    lean = payload.get("lean_statistics") or {}
    portfolio = lean.get("portfolio") or {}
    trade = lean.get("trade") or {}

    portfolio_fields = {
        "sharpe_ratio": "sharpe_ratio",
        "sortino_ratio": "sortino_ratio",
        "compounding_annual_return": "cagr",
        "annual_standard_deviation": "annual_standard_deviation",
        "drawdown": "max_drawdown_pct",
        "drawdown_recovery": "drawdown_recovery",
        "probabilistic_sharpe_ratio": "probabilistic_sharpe_ratio",
    }
    for legacy_key, canonical_key in portfolio_fields.items():
        if stats.get(canonical_key) is None and portfolio.get(legacy_key) is not None:
            stats[canonical_key] = portfolio[legacy_key]

    trade_fields = {
        "profit_factor": "profit_factor",
        "max_consecutive_losing_trades": "max_consecutive_losing_trades",
    }
    for legacy_key, canonical_key in trade_fields.items():
        if stats.get(canonical_key) is None and trade.get(legacy_key) is not None:
            stats[canonical_key] = trade[legacy_key]

    average_profit = trade.get("average_profit")
    average_loss = trade.get("average_loss")
    if stats.get("payoff_ratio") is None and average_profit is not None and average_loss not in (None, 0):
        stats["payoff_ratio"] = average_profit / abs(average_loss)

    trade_sharpe = trade.get("sharpe_ratio")
    if trade_sharpe is not None and not (case["engine"] == "lean" and trade_sharpe == 0):
        stats["trade_sharpe_ratio"] = trade_sharpe
    return payload


@pytest.mark.parametrize("case", json.loads(V1_FIXTURE_PATH.read_text())["cases"], ids=lambda c: c["id"])
def test_compute_run_verdict_v2_preserves_v1_subscore_thresholds(case: dict[str, Any]) -> None:
    """V2 changes completeness, not the already-published value-to-points buckets."""
    fixture = json.loads(V1_FIXTURE_PATH.read_text())

    verdict = compute_run_verdict(
        _canonicalized_v1_input(case),
        engine=case["engine"],
        generated_at_ms=fixture["generated_at_ms"],
    )
    expected = case["expected"]

    assert verdict.generated_at_ms == fixture["generated_at_ms"]
    assert verdict.engine == case["engine"]
    assert _sub_scores(verdict) == expected["sub_scores"]


@pytest.mark.parametrize("case", json.loads(V2_FIXTURE_PATH.read_text())["cases"], ids=lambda c: c["id"])
def test_compute_run_verdict_matches_v2_golden_fixture(case: dict[str, Any]) -> None:
    fixture = json.loads(V2_FIXTURE_PATH.read_text())

    verdict = compute_run_verdict(
        case["input"],
        engine=case["engine"],
        generated_at_ms=fixture["generated_at_ms"],
    )
    expected = case["expected"]

    assert verdict.verdict_version == 2
    assert verdict.generated_at_ms == fixture["generated_at_ms"]
    assert verdict.engine == case["engine"]
    assert verdict.status == expected["status"]
    assert verdict.available_required_metrics == expected["available_required_metrics"]
    assert verdict.required_metrics == expected["required_metrics"]
    if "missing_required_metrics" in expected:
        assert verdict.missing_required_metrics == expected["missing_required_metrics"]
    else:
        assert len(verdict.missing_required_metrics) == expected["missing_required_metrics_count"]
    assert verdict.composite == expected["composite"]
    assert verdict.grade == expected["grade"]
    assert verdict.signal == expected["signal"]
    assert verdict.normalized_weights is expected["normalized_weights"]
    assert _dimension_scores(verdict) == expected["dimension_scores"]


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
    fixture = json.loads(V1_FIXTURE_PATH.read_text())
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


def test_lean_native_metrics_never_feed_platform_readiness() -> None:
    """Run 78 regression: native LEAN evidence cannot manufacture a platform grade."""
    verdict = compute_run_verdict(
        {
            "statistics": {},
            "win_rate": 0.6575,
            "total_trades": 73,
            "net_profit": 8_540.93,
            "total_fees": 146.02,
            "lean_statistics": {
                "portfolio": {
                    "compounding_annual_return": 0.0418,
                    "annual_standard_deviation": 0.026,
                    "probabilistic_sharpe_ratio": 0.5967,
                    "drawdown": 0.026,
                    "drawdown_recovery": 85,
                    "sharpe_ratio": -1.01,
                    "sortino_ratio": -0.60,
                },
                "trade": {
                    "profit_factor": 1.96,
                    "sharpe_ratio": 0,
                    "max_consecutive_losing_trades": 4,
                    "average_profit": 102,
                    "average_loss": -100,
                },
            },
        },
        engine="lean",
        generated_at_ms=1_700_000_000_000,
    )

    assert verdict.verdict_version == 2
    assert verdict.status == "incomplete"
    assert verdict.available_required_metrics == 4
    assert verdict.required_metrics == 17
    assert verdict.composite is None
    assert verdict.grade is None
    assert verdict.signal is None
    assert verdict.normalized_weights is False
    assert verdict.missing_required_metrics == [
        "Return Quality: Sharpe ratio",
        "Return Quality: Sortino ratio",
        "Return Quality: CAGR",
        "Return Quality: Calmar ratio",
        "Return Quality: Annual volatility",
        "Risk Control: Max drawdown",
        "Risk Control: Drawdown recovery",
        "Risk Control: Max consecutive losers",
        "Trade Edge: Profit factor",
        "Trade Edge: Expectancy / trade",
        "Trade Edge: Payoff ratio",
        "Statistical Confidence: Probabilistic Sharpe",
        "Statistical Confidence: Trade vs Portfolio Sharpe gap",
    ]
    trade_edge = next(d for d in verdict.dimensions if d.key == "trade_edge")
    confidence = next(d for d in verdict.dimensions if d.key == "stat_confidence")
    assert trade_edge.score is None
    assert confidence.score is None


def test_complete_platform_readiness_uses_fixed_published_weights() -> None:
    """A 17/17 platform payload receives a grade without runtime reweighting."""
    verdict = compute_run_verdict(
        {
            "statistics": {
                "sharpe_ratio": 1.43,
                "sortino_ratio": 2.59,
                "max_drawdown_pct": 0.0257,
                "profit_factor": 2.00,
                "expectancy_pct": 0.00111,
                "payoff_ratio": 0.97,
                "cagr": 0.0398,
                "annual_standard_deviation": 0.0257,
                "drawdown_recovery": 68,
                "max_consecutive_losing_trades": 4,
                "probabilistic_sharpe_ratio": 0.8006,
                "trade_sharpe_ratio": 4.03,
            },
            "win_rate": 0.6575,
            "total_trades": 73,
            "net_profit": 8_184.95,
            "total_fees": 146.0,
            "lean_statistics": {"portfolio": {"sharpe_ratio": -999}, "trade": {"profit_factor": 999}},
        },
        engine="python",
        generated_at_ms=1_700_000_000_000,
    )

    assert verdict.verdict_version == 2
    assert verdict.status == "complete"
    assert verdict.available_required_metrics == 17
    assert verdict.required_metrics == 17
    assert verdict.composite == 76
    assert verdict.grade == "A"
    assert verdict.signal == "Paper-trade"
    assert verdict.missing_required_metrics == []
    assert verdict.normalized_weights is False
    assert {dimension.key: dimension.weight for dimension in verdict.dimensions} == {
        "return_quality": pytest.approx(25 / 85),
        "risk_control": pytest.approx(20 / 85),
        "trade_edge": pytest.approx(20 / 85),
        "stat_confidence": pytest.approx(20 / 85),
        "alpha_calibration": 0,
    }


def test_readiness_parity_signature_ignores_sub_epsilon_float_noise() -> None:
    fixture = json.loads(V2_FIXTURE_PATH.read_text())
    case = next(case for case in fixture["cases"] if case["id"] == "complete_platform_contract")
    left_input = deepcopy(case["input"])
    right_input = deepcopy(case["input"])
    right_input["statistics"]["cagr"] += 2e-15

    left = compute_run_verdict(left_input, engine="python", generated_at_ms=1)
    right = compute_run_verdict(right_input, engine="lean", generated_at_ms=2)

    assert left.parity_signature == right.parity_signature
    assert left.parity_signature["required_metrics"] == 17
    assert len(left.parity_signature["required_inputs"]) == 17

    right_input["statistics"]["cagr"] += 1e-6
    materially_different = compute_run_verdict(right_input, engine="lean", generated_at_ms=3)
    assert left.parity_signature != materially_different.parity_signature


def test_removing_required_evidence_revokes_grade_instead_of_improving_it() -> None:
    fixture = json.loads(V2_FIXTURE_PATH.read_text())
    complete = next(case for case in fixture["cases"] if case["id"] == "complete_platform_contract")
    payload = deepcopy(complete["input"])
    del payload["statistics"]["expectancy_pct"]

    verdict = compute_run_verdict(
        payload,
        engine="python",
        generated_at_ms=fixture["generated_at_ms"],
    )

    assert verdict.status == "incomplete"
    assert verdict.available_required_metrics == 16
    assert verdict.missing_required_metrics == ["Trade Edge: Expectancy / trade"]
    assert verdict.composite is None
    assert verdict.grade is None
    assert verdict.signal is None


def test_missing_sortino_remains_unavailable_with_its_platform_reason() -> None:
    fixture = json.loads(V2_FIXTURE_PATH.read_text())
    complete = next(case for case in fixture["cases"] if case["id"] == "complete_platform_contract")
    payload = deepcopy(complete["input"])
    del payload["statistics"]["sortino_ratio"]

    verdict = compute_run_verdict(
        payload,
        engine="python",
        generated_at_ms=fixture["generated_at_ms"],
    )

    assert verdict.status == "incomplete"
    assert verdict.available_required_metrics == 16
    assert verdict.composite is None
    assert verdict.grade is None
    assert verdict.evidence_action is None
    assert [item.model_dump() for item in verdict.missing_required_evidence] == [
        {
            "key": "sortino",
            "label": "Return Quality: Sortino ratio",
            "producer": "platform",
            "reason": "No negative returns this window.",
        }
    ]
    return_quality = next(dimension for dimension in verdict.dimensions if dimension.key == "return_quality")
    sortino = next(sub_score for sub_score in return_quality.sub_scores if sub_score.key == "sortino")
    assert sortino.raw_value is None
    assert sortino.score is None
    assert sortino.display == "-"


def test_evidence_grade_reframes_the_frozen_v2_result_without_changing_its_score() -> None:
    fixture = json.loads(V2_FIXTURE_PATH.read_text())
    complete = next(case for case in fixture["cases"] if case["id"] == "complete_platform_contract")

    verdict = compute_run_verdict(
        complete["input"],
        engine="python",
        generated_at_ms=fixture["generated_at_ms"],
    )

    assert verdict.composite == 76
    assert verdict.grade == "A"
    assert verdict.signal == "Paper-trade"
    assert verdict.evidence_action == "Continue forward and out-of-sample validation"
    assert verdict.headline == "Strong backtest evidence; continue forward and out-of-sample validation."
    assert "Production Readiness" not in verdict.headline


def test_failed_run_is_a_run_failure_not_an_f_grade() -> None:
    verdict = failed_run_verdict("worker did not produce normalized results", generated_at_ms=1_700_000_000_000)

    assert verdict.status == "failed"
    assert verdict.composite is None
    assert verdict.grade is None
    assert verdict.evidence_action is None
    assert verdict.signal is None
    assert verdict.headline.startswith("Run failed —")
