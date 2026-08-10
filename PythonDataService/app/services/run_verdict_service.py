"""Run verdict v2 authoring for Engine Lab.

Formula: If all 17 required sub-scores exist, readiness = round_half_up(
  Σ fixed_dimension_weight[d] · dimension_score[d]); otherwise the verdict is
  incomplete and has no composite, grade, or deployment signal.
Reference: docs/references/reconciliations/
  engine-lab-runs-75-76-statistics-validation-plan.md § "Replace dynamic
  readiness reweighting with a fixed completeness contract".
Canonical implementation: this file.
Validated against: PythonDataService/tests/services/test_run_verdict_parity.py.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from app.schemas.run_verdict import (
    EngineKind,
    RunVerdict,
    RunVerdictCleanliness,
    RunVerdictDimension,
    RunVerdictInput,
    RunVerdictMissingEvidence,
    RunVerdictSubScore,
)

RUN_VERDICT_VERSION = 2

# V2 freezes the old scorer's relative 25:20:20:20 intent once at the
# definition level. It never changes weights based on which values a run happens
# to omit. Alpha Calibration remains visible but ungraded until a future verdict
# version defines and validates those metrics.
CORE_DIMENSION_WEIGHTS: dict[str, float] = {
    "return_quality": 25 / 85,
    "risk_control": 20 / 85,
    "trade_edge": 20 / 85,
    "stat_confidence": 20 / 85,
}
REQUIRED_SUB_SCORE_KEYS: dict[str, frozenset[str]] = {
    "return_quality": frozenset({"sharpe", "sortino", "cagr", "calmar", "annual_vol"}),
    "risk_control": frozenset({"max_dd", "recovery", "cons_losses"}),
    "trade_edge": frozenset({"pf", "expectancy", "win_rate", "payoff", "fee_drag"}),
    "stat_confidence": frozenset({"psr", "sample", "skepticism", "trade_gap"}),
}
REQUIRED_METRIC_COUNT = sum(len(keys) for keys in REQUIRED_SUB_SCORE_KEYS.values())
READINESS_PARITY_CONTRACT_ID = "readiness-core-v2"
READINESS_PARITY_ABSOLUTE_TOLERANCE = Decimal("0.000000000001")

# Catalog prose is kept beside the fixed scorer so documentation cannot drift
# into a second threshold registry. The catalog imports these descriptions; it
# never owns or evaluates the policy.
VERDICT_POLICY_DOCUMENTATION: dict[str, str] = {
    "sharpe": "<0: 0; <0.5: 4; <1: 10; <1.5: 15; <2: 18; <3: 20; otherwise: 12.",
    "sortino": "<0.5: 3; <1: 8; <1.5: 13; <2.5: 18; <4: 20; otherwise: 14.",
    "cagr": "≤0: 0; <4%: 6; <8%: 11; <15%: 16; <30%: 20; otherwise: 14.",
    "calmar": "<0: 0; <0.5: 5; <1: 10; <3: 15; <5: 20; otherwise: 14.",
    "annual_volatility": "<3%: 20; <10%: 17; <20%: 13; <35%: 8; otherwise: 3.",
    "maximum_drawdown": "<2%: 17; <5%: 20; <10%: 18; <15%: 14; <20%: 8; <30%: 4; otherwise: 0.",
    "recovery_duration": "≤10 days: 20; ≤30: 16; ≤60: 12; ≤120: 8; ≤252: 4; otherwise: 1.",
    "max_consecutive_losers": "≤3: 20; ≤5: 16; ≤8: 10; ≤12: 5; otherwise: 0.",
    "profit_factor": "<1: 0; <1.25: 6; <1.75: 12; <3: 18; <4: 20; otherwise: 10. Infinite value scores 10.",
    "expectancy": "≤0: 0; <0.1%: 8; <0.5%: 14; <2%: 20; otherwise: 18.",
    "win_rate": "<30%: 4; <50%: 10; <55%: 14; <75%: 20; <85%: 16; otherwise: 6.",
    "payoff_ratio": "<0.5: 4; <1: 10; <1.5: 15; <3: 20; otherwise: 16.",
    "fee_drag": "Gross profit ≤0: 0; otherwise <5%: 20; <15%: 16; <30%: 11; <50%: 5; otherwise: 1.",
    "probabilistic_sharpe": "<50%: 2; <80%: 8; <95%: 14; <99%: 20; otherwise: 18.",
    "sample_size": "<20: 2; <50: 7; <100: 13; <250: 18; otherwise: 20.",
    "skepticism_penalty": "Start at 20; subtract 8 for Sharpe >3, 6 for finite profit factor >4, and 6 for win rate >85%; floor at 0.",
    "trade_portfolio_sharpe_gap": "<1: 20; <2: 16; <3: 12; <5: 6; otherwise: 2.",
}


def build_run_verdict_parity_signature(verdict: RunVerdict) -> dict[str, Any]:
    """Freeze the 17 readiness inputs without engine-specific float noise.

    Python authors this receipt so transport layers only compare canonical
    evidence. Raw input values are quantized at 1e-12, far below any published
    display or scoring threshold, before exact JSON comparison.
    """

    def canonical_raw(value: float | None) -> str | None:
        if value is None:
            return None
        if not math.isfinite(value):
            return str(value)
        rounded = Decimal(str(value)).quantize(
            READINESS_PARITY_ABSOLUTE_TOLERANCE,
            rounding=ROUND_HALF_EVEN,
        )
        if rounded == 0:
            rounded = abs(rounded)
        return format(rounded, "f")

    required_inputs = [
        {
            "dimension": dimension.key,
            "metric": sub_score.key,
            "raw_value": canonical_raw(sub_score.raw_value),
            "score": sub_score.score,
            "display": sub_score.display,
        }
        for dimension in verdict.dimensions
        for sub_score in dimension.sub_scores
        if sub_score.key in REQUIRED_SUB_SCORE_KEYS.get(dimension.key, frozenset())
    ]
    return {
        "contract_id": READINESS_PARITY_CONTRACT_ID,
        "absolute_tolerance": str(READINESS_PARITY_ABSOLUTE_TOLERANCE),
        "status": verdict.status,
        "composite": verdict.composite,
        "grade": verdict.grade,
        "evidence_action": verdict.evidence_action,
        "signal": verdict.signal,
        "red_flags": verdict.red_flags,
        "missing_required_metrics": verdict.missing_required_metrics,
        "missing_required_evidence": [item.model_dump(mode="json") for item in verdict.missing_required_evidence],
        "available_required_metrics": verdict.available_required_metrics,
        "required_metrics": verdict.required_metrics,
        "normalized_weights": verdict.normalized_weights,
        "required_inputs": required_inputs,
    }


def _with_parity_signature(verdict: RunVerdict) -> RunVerdict:
    return verdict.model_copy(update={"parity_signature": build_run_verdict_parity_signature(verdict)})


def compute_run_verdict(
    payload: RunVerdictInput | Mapping[str, Any] | None,
    *,
    engine: EngineKind,
    generated_at_ms: int | None = None,
    cleanliness: RunVerdictCleanliness | Mapping[str, Any] | None = None,
) -> RunVerdict:
    """Score platform-canonical metrics; retain LEAN-native fields as evidence.

    ``lean_statistics`` remains on the input model for persisted-envelope
    compatibility, but verdict v2 deliberately never reads it. Native LEAN
    values have their own definition and cannot backfill a platform metric.
    """
    data = _coerce_input(payload)
    generated = generated_at_ms if generated_at_ms is not None else int(time.time() * 1000)
    clean = _coerce_cleanliness(cleanliness)

    if data is None:
        verdict = _empty_verdict(
            headline="Run a backtest to produce backtest evidence.",
            engine=engine,
            generated_at_ms=generated,
            cleanliness=clean,
        )
        return _apply_cleanliness(verdict)

    dimensions = [
        _score_return_quality(data),
        _score_risk_control(data),
        _score_trade_edge(data),
        _score_statistical_confidence(data),
        _score_alpha_calibration(),
    ]
    missing_metrics = [
        f"{dimension.label}: {sub.label}"
        for dimension in dimensions
        for sub in dimension.sub_scores
        if sub.score is None
    ]
    missing_required_metrics = _missing_required_metrics(dimensions)
    missing_required_evidence = _missing_required_evidence(dimensions)
    available_required_metrics = REQUIRED_METRIC_COUNT - len(missing_required_metrics)

    if missing_required_metrics:
        status = "unavailable" if available_required_metrics == 0 else "incomplete"
        if status == "unavailable":
            headline = "Not enough required evidence to produce a Backtest Evidence Grade."
        else:
            headline = (
                "Backtest Evidence Grade incomplete — "
                f"{available_required_metrics}/{REQUIRED_METRIC_COUNT} required metrics available. "
                f"Missing: {', '.join(missing_required_metrics)}."
            )
        verdict = RunVerdict(
            verdict_version=RUN_VERDICT_VERSION,
            status=status,
            engine=engine,
            generated_at_ms=generated,
            composite=None,
            grade=None,
            evidence_action=None,
            signal=None,
            headline=headline,
            red_flags=[],
            dimensions=dimensions,
            missing_metrics=missing_metrics,
            missing_required_metrics=missing_required_metrics,
            missing_required_evidence=missing_required_evidence,
            available_required_metrics=available_required_metrics,
            required_metrics=REQUIRED_METRIC_COUNT,
            normalized_weights=False,
            cleanliness=clean,
        )
        return _apply_cleanliness(verdict)

    dimension_by_key = {dimension.key: dimension for dimension in dimensions}
    composite = _round_half_up(
        sum(
            (dimension_by_key[key].score or 0) * weight
            for key, weight in CORE_DIMENSION_WEIGHTS.items()
        )
    )
    grade, signal, evidence_action, headline = _grade_and_signal(composite)
    verdict = RunVerdict(
        verdict_version=RUN_VERDICT_VERSION,
        status="complete",
        engine=engine,
        generated_at_ms=generated,
        composite=composite,
        grade=grade,
        evidence_action=evidence_action,
        signal=signal,
        headline=headline,
        red_flags=[],
        dimensions=dimensions,
        missing_metrics=missing_metrics,
        missing_required_metrics=[],
        missing_required_evidence=[],
        available_required_metrics=REQUIRED_METRIC_COUNT,
        required_metrics=REQUIRED_METRIC_COUNT,
        normalized_weights=False,
        cleanliness=clean,
    )
    return _apply_cleanliness(verdict)


def failed_run_verdict(error: str, *, generated_at_ms: int | None = None) -> RunVerdict:
    generated = generated_at_ms if generated_at_ms is not None else int(time.time() * 1000)
    verdict = _empty_verdict(
        headline=f"LEAN run failed before producing normalized results: {error}",
        engine="lean",
        generated_at_ms=generated,
        cleanliness=RunVerdictCleanliness(
            is_clean=False,
            is_reconciliation_grade=False,
            error_counts={"runtime_error": 1},
        ),
    )
    return _with_parity_signature(
        verdict.model_copy(
            update={
                "status": "failed",
                "composite": None,
                "grade": None,
                "evidence_action": None,
                "signal": None,
                "headline": "Run failed — " + verdict.headline,
                "red_flags": ["lean_run_failed"],
            }
        )
    )


def _coerce_input(payload: RunVerdictInput | Mapping[str, Any] | None) -> RunVerdictInput | None:
    if payload is None:
        return None
    if isinstance(payload, RunVerdictInput):
        return payload
    return RunVerdictInput.model_validate(payload)


def _coerce_cleanliness(
    cleanliness: RunVerdictCleanliness | Mapping[str, Any] | None,
) -> RunVerdictCleanliness | None:
    if cleanliness is None:
        return None
    if isinstance(cleanliness, RunVerdictCleanliness):
        return cleanliness
    return RunVerdictCleanliness.model_validate(cleanliness)


def _empty_verdict(
    *,
    headline: str,
    engine: EngineKind,
    generated_at_ms: int,
    cleanliness: RunVerdictCleanliness | None,
) -> RunVerdict:
    return RunVerdict(
        verdict_version=RUN_VERDICT_VERSION,
        status="unavailable",
        engine=engine,
        generated_at_ms=generated_at_ms,
        composite=None,
        grade=None,
        evidence_action=None,
        signal=None,
        headline=headline,
        red_flags=[],
        dimensions=[],
        missing_metrics=[],
        missing_required_metrics=[],
        missing_required_evidence=[],
        available_required_metrics=0,
        required_metrics=REQUIRED_METRIC_COUNT,
        normalized_weights=False,
        cleanliness=cleanliness,
    )


def _apply_cleanliness(verdict: RunVerdict) -> RunVerdict:
    if verdict.cleanliness is None or verdict.cleanliness.is_clean:
        return _with_parity_signature(verdict)
    headline = "LEAN run is not reconciliation-clean. " + verdict.headline
    update: dict[str, Any] = {
        "evidence_action": "Inspect reconciliation discrepancies before relying on this evidence",
        "signal": "Rework",
        "headline": headline,
        "red_flags": [*verdict.red_flags, "lean_run_not_clean"],
    }
    return _with_parity_signature(verdict.model_copy(update=update))


def _score_return_quality(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    cagr = _num(stats.get("cagr"))
    sub_scores = [
        _grade_sharpe_sub(_num(stats.get("sharpe_ratio"))),
        _grade_sortino_sub(_num(stats.get("sortino_ratio"))),
        _grade_cagr_sub(cagr),
        _grade_calmar_sub(_num(stats.get("max_drawdown_pct")), cagr),
        _grade_annual_vol_sub(_num(stats.get("annual_standard_deviation"))),
    ]
    return _dimension(
        "return_quality",
        "Return Quality",
        CORE_DIMENSION_WEIGHTS["return_quality"],
        sub_scores,
        "Does the strategy make money efficiently per unit of risk?",
    )


def _score_risk_control(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    sub_scores = [
        _grade_max_drawdown_sub(_num(stats.get("max_drawdown_pct"))),
        _grade_recovery_sub(_num(stats.get("drawdown_recovery"))),
        _grade_consecutive_losses_sub(_num(stats.get("max_consecutive_losing_trades"))),
        _sub("dd_duration", "Drawdown duration", None, None, "-", "Not yet computed - needs equity-curve timestamps."),
        _sub("downside_vol", "Downside volatility", None, None, "-", "Planned - uses Sortino's sigma_d separately."),
    ]
    return _dimension(
        "risk_control",
        "Risk Control",
        CORE_DIMENSION_WEIGHTS["risk_control"],
        sub_scores,
        "Does the strategy preserve capital when it's wrong?",
    )


def _score_trade_edge(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    sub_scores = [
        _grade_profit_factor_sub(_num(stats.get("profit_factor"))),
        _grade_expectancy_sub(_num(stats.get("expectancy_pct"))),
        _grade_win_rate_sub(_num(r.win_rate)),
        _grade_payoff_sub(_num(stats.get("payoff_ratio"))),
        _grade_fee_drag_sub(_num(r.net_profit), _num(r.total_fees)),
    ]
    return _dimension(
        "trade_edge",
        "Trade Edge",
        CORE_DIMENSION_WEIGHTS["trade_edge"],
        sub_scores,
        "Is there a real per-trade edge after costs?",
    )


def _score_statistical_confidence(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    portfolio_sharpe = _num(stats.get("sharpe_ratio"))
    sub_scores = [
        _grade_psr_sub(_num(stats.get("probabilistic_sharpe_ratio"))),
        _grade_sample_size_sub(_num(r.total_trades)),
        _grade_skepticism_sub(
            portfolio_sharpe,
            _num(stats.get("profit_factor")),
            _num(r.win_rate),
        ),
        _grade_trade_gap_sub(portfolio_sharpe, _num(stats.get("trade_sharpe_ratio"))),
        _sub("benchmark", "Benchmark outperformance", None, None, "-", "Planned - requires a Buy-and-Hold return series alongside the backtest."),
    ]
    return _dimension(
        "stat_confidence",
        "Statistical Confidence",
        CORE_DIMENSION_WEIGHTS["stat_confidence"],
        sub_scores,
        "Is the edge trustworthy, or sample-size / overfitting noise?",
    )


def _score_alpha_calibration() -> RunVerdictDimension:
    sub_scores = [
        _sub("ece", "Expected Calibration Error", None, None, "-", "Planned - derive from insight_summary confidence buckets."),
        _sub("conf_spread", "Over/under-confidence spread", None, None, "-", "Planned - per-bucket accuracy minus emitted confidence."),
        _sub("magnitude_bias", "Magnitude bias", None, None, "-", "Planned - mean of (actual - predicted) move."),
        _sub("worst_hour", "Worst-hour accuracy", None, None, "-", "Planned - min accuracy across hour-of-day buckets."),
        _sub("regime_consistency", "Regime consistency", None, None, "-", "Planned - rolling accuracy variance across market regimes."),
    ]
    return RunVerdictDimension(
        key="alpha_calibration",
        label="Alpha Calibration",
        weight=0.0,
        score=None,
        sub_scores=sub_scores,
        summary="Does the alpha model's confidence match its empirical accuracy?",
    )


def _dimension(
    key: str,
    label: str,
    weight: float,
    sub_scores: list[RunVerdictSubScore],
    summary: str,
) -> RunVerdictDimension:
    required_keys = REQUIRED_SUB_SCORE_KEYS[key]
    return RunVerdictDimension(
        key=key,
        label=label,
        weight=weight,
        score=_score_required_subs(sub_scores, required_keys),
        sub_scores=sub_scores,
        summary=summary,
    )


def _missing_required_metrics(dimensions: list[RunVerdictDimension]) -> list[str]:
    missing: list[str] = []
    for dimension in dimensions:
        required_keys = REQUIRED_SUB_SCORE_KEYS.get(dimension.key, frozenset())
        missing.extend(
            f"{dimension.label}: {sub_score.label}"
            for sub_score in dimension.sub_scores
            if sub_score.key in required_keys and sub_score.score is None
        )
    return missing


def _missing_required_evidence(
    dimensions: list[RunVerdictDimension],
) -> list[RunVerdictMissingEvidence]:
    missing: list[RunVerdictMissingEvidence] = []
    for dimension in dimensions:
        required_keys = REQUIRED_SUB_SCORE_KEYS.get(dimension.key, frozenset())
        for sub_score in dimension.sub_scores:
            if sub_score.key not in required_keys or sub_score.score is not None:
                continue
            missing.append(
                RunVerdictMissingEvidence(
                    key=sub_score.key,
                    label=f"{dimension.label}: {sub_score.label}",
                    producer="platform",
                    reason=sub_score.note,
                )
            )
    return missing


def _grade_and_signal(score: int) -> tuple[str, str, str, str]:
    if score >= 85:
        grade, signal, evidence_action, headline = (
            "A+",
            "Deploy",
            "Advance to independent validation",
            "Very strong backtest evidence; advance to independent validation.",
        )
    elif score >= 70:
        grade, signal, evidence_action, headline = (
            "A",
            "Paper-trade",
            "Continue forward and out-of-sample validation",
            "Strong backtest evidence; continue forward and out-of-sample validation.",
        )
    elif score >= 55:
        grade, signal, evidence_action, headline = (
            "B",
            "Iterate",
            "Investigate identified weaknesses",
            "Promising backtest evidence; investigate identified weaknesses.",
        )
    elif score >= 40:
        grade, signal, evidence_action, headline = (
            "C",
            "Rework",
            "Revise the hypothesis or validation design",
            "Mixed backtest evidence; revise the hypothesis or validation design.",
        )
    elif score >= 25:
        grade, signal, evidence_action, headline = (
            "D",
            "Rework",
            "Substantial rework is required",
            "Weak backtest evidence; substantial rework is required.",
        )
    else:
        grade, signal, evidence_action, headline = (
            "F",
            "Reject",
            "Rework the tested strategy hypothesis and validate independently",
            "Insufficient support for the tested strategy hypothesis.",
        )
    return grade, signal, evidence_action, headline


def _sub(
    key: str,
    label: str,
    score: int | None,
    raw_value: float | None,
    display: str,
    note: str,
) -> RunVerdictSubScore:
    return RunVerdictSubScore(
        key=key,
        label=label,
        score=score,
        raw_value=raw_value,
        display=display,
        note=note,
    )


def _grade_sharpe_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("sharpe", "Sharpe ratio", None, v, "-" if v is None else f"{v:.2f}", "")
    if v is None:
        return base.model_copy(update={"note": "Not computed this window."})
    if v < 0:
        return base.model_copy(update={"score": 0, "note": "Negative Sharpe - losing money on risk-adjusted basis."})
    if v < 0.5:
        return base.model_copy(update={"score": 4, "note": "Below professional viability."})
    if v < 1.0:
        return base.model_copy(update={"score": 10, "note": "Below the 1.0 institutional floor."})
    if v < 1.5:
        return base.model_copy(update={"score": 15, "note": "Clears the institutional floor."})
    if v < 2.0:
        return base.model_copy(update={"score": 18, "note": "Solidly institutional."})
    if v < 3.0:
        return base.model_copy(update={"score": 20, "note": "Elite - verify out-of-sample."})
    return base.model_copy(
        update={
            "score": 12,
            "note": "Extreme Sharpe; inspect sampling, annualization, fills, data leakage, and selection history.",
        }
    )


def _grade_sortino_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("sortino", "Sortino ratio", None, v, "-" if v is None else f"{v:.2f}", "")
    if v is None:
        return base.model_copy(update={"note": "No negative returns this window."})
    if v < 0.5:
        return base.model_copy(update={"score": 3, "note": "Downside risk dominates."})
    if v < 1.0:
        return base.model_copy(update={"score": 8, "note": "Below the 1.0 baseline."})
    if v < 1.5:
        return base.model_copy(update={"score": 13, "note": "Approaching the 1.5 baseline."})
    if v < 2.5:
        return base.model_copy(update={"score": 18, "note": "Meets the institutional baseline."})
    if v < 4.0:
        return base.model_copy(update={"score": 20, "note": "Excellent downside profile."})
    return base.model_copy(update={"score": 14, "note": "Extreme Sortino - validate sample size."})


def _grade_cagr_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("cagr", "CAGR", None, v, "-" if v is None else f"{v * 100:.2f}%", "")
    if v is None:
        return base.model_copy(update={"note": "Not provided by engine (lean_statistics missing)."})
    if v <= 0:
        return base.model_copy(update={"score": 0, "note": "Negative compound annual return."})
    if v < 0.04:
        return base.model_copy(update={"score": 6, "note": "Below risk-free - consider T-bills."})
    if v < 0.08:
        return base.model_copy(update={"score": 11, "note": "Below long-run equity baseline."})
    if v < 0.15:
        return base.model_copy(update={"score": 16, "note": "Healthy annualized return."})
    if v < 0.30:
        return base.model_copy(update={"score": 20, "note": "Elite annualized return."})
    return base.model_copy(update={"score": 14, "note": "Very high CAGR - check for overfitting or leverage."})


def _grade_calmar_sub(max_dd: float | None, cagr: float | None) -> RunVerdictSubScore:
    base = _sub("calmar", "Calmar ratio", None, None, "-", "")
    if cagr is None or max_dd is None or max_dd <= 0:
        return base.model_copy(update={"note": "Needs CAGR and Max DD to compute Calmar."})
    calmar = cagr / max_dd
    base = base.model_copy(update={"raw_value": calmar, "display": f"{calmar:.2f}"})
    if calmar < 0:
        return base.model_copy(update={"score": 0, "note": "Negative Calmar."})
    if calmar < 0.5:
        return base.model_copy(update={"score": 5, "note": "Return-to-risk ratio is weak."})
    if calmar < 1.0:
        return base.model_copy(update={"score": 10, "note": "Below the 1.0 threshold."})
    if calmar < 3.0:
        return base.model_copy(update={"score": 15, "note": "Healthy return-to-drawdown ratio."})
    if calmar < 5.0:
        return base.model_copy(update={"score": 20, "note": "Elite Calmar."})
    return base.model_copy(update={"score": 14, "note": "Very high Calmar - verify the drawdown window is representative."})


def _grade_annual_vol_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("annual_vol", "Annual volatility", None, v, "-" if v is None else f"{v * 100:.2f}%", "")
    if v is None:
        return base.model_copy(update={"note": "Not provided by engine."})
    if v < 0.03:
        return base.model_copy(update={"score": 20, "note": "Very low volatility - stable return profile."})
    if v < 0.10:
        return base.model_copy(update={"score": 17, "note": "Low volatility - below typical equity."})
    if v < 0.20:
        return base.model_copy(update={"score": 13, "note": "Typical equity volatility."})
    if v < 0.35:
        return base.model_copy(update={"score": 8, "note": "Elevated volatility."})
    return base.model_copy(update={"score": 3, "note": "Very high volatility - position sizing is critical."})


def _grade_max_drawdown_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("max_dd", "Max drawdown", None, v, "-" if v is None else f"{v * 100:.2f}%", "")
    if v is None:
        return base.model_copy(update={"note": "Not computed."})
    if v < 0.02:
        return base.model_copy(update={"score": 17, "note": "Extreme preservation - verify window is long enough."})
    if v < 0.05:
        return base.model_copy(update={"score": 20, "note": "Superior capital preservation."})
    if v < 0.10:
        return base.model_copy(update={"score": 18, "note": "Excellent drawdown profile."})
    if v < 0.15:
        return base.model_copy(update={"score": 14, "note": "Within institutional tolerance."})
    if v < 0.20:
        return base.model_copy(update={"score": 8, "note": "Approaching the 20% institutional cap."})
    if v < 0.30:
        return base.model_copy(update={"score": 4, "note": "Above typical institutional limit."})
    return base.model_copy(update={"score": 0, "note": "Fails typical risk-committee review."})


def _grade_recovery_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("recovery", "Drawdown recovery", None, v, "-" if v is None else f"{v:g} days", "")
    if v is None:
        return base.model_copy(update={"note": "Not provided by engine."})
    if v <= 10:
        return base.model_copy(update={"score": 20, "note": "Quick recovery - strategy bounces back fast."})
    if v <= 30:
        return base.model_copy(update={"score": 16, "note": "Healthy recovery window."})
    if v <= 60:
        return base.model_copy(update={"score": 12, "note": "Moderate recovery time."})
    if v <= 120:
        return base.model_copy(update={"score": 8, "note": "Long recovery - \"staircase\" pattern risk."})
    if v <= 252:
        return base.model_copy(update={"score": 4, "note": "Nearly a full year to recover - investor patience risk."})
    return base.model_copy(update={"score": 1, "note": "Very long recovery - likely unacceptable for investors."})


def _grade_consecutive_losses_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("cons_losses", "Max consecutive losers", None, v, "-" if v is None else f"{v:g}", "")
    if v is None:
        return base.model_copy(update={"note": "Not computed."})
    if v <= 3:
        return base.model_copy(update={"score": 20, "note": "Resilient through streaks."})
    if v <= 5:
        return base.model_copy(update={"score": 16, "note": "Typical losing streak length."})
    if v <= 8:
        return base.model_copy(update={"score": 10, "note": "Long streak - psychologically hard to trade live."})
    if v <= 12:
        return base.model_copy(update={"score": 5, "note": "Very long streak - kill-switch risk."})
    return base.model_copy(update={"score": 0, "note": "Extreme streak - most traders would bail before recovery."})


def _grade_profit_factor_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("pf", "Profit factor", None, v, _ratio_display(v), "")
    if v is None:
        return base.model_copy(update={"note": "Not computed."})
    if not math.isfinite(v):
        return base.model_copy(update={"score": 10, "note": "No losing trades yet - need a longer window."})
    if v < 1.0:
        return base.model_copy(update={"score": 0, "note": "Losing system."})
    if v < 1.25:
        return base.model_copy(update={"score": 6, "note": "Edge likely not robust after slippage."})
    if v < 1.75:
        return base.model_copy(update={"score": 12, "note": "Marginal - below the 1.75 threshold."})
    if v < 3.0:
        return base.model_copy(update={"score": 18, "note": "Healthy profit factor."})
    if v < 4.0:
        return base.model_copy(update={"score": 20, "note": "Elite-tier efficiency."})
    return base.model_copy(
        update={
            "score": 10,
            "note": "Extreme profit factor; inspect out-of-sample evidence and the loss tail.",
        }
    )


def _grade_expectancy_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("expectancy", "Expectancy / trade", None, v, "-" if v is None else f"{v * 100:.3f}%", "")
    if v is None:
        return base.model_copy(update={"note": "Not computed."})
    if v <= 0:
        return base.model_copy(update={"score": 0, "note": "Non-positive edge per trade."})
    if v < 0.001:
        return base.model_copy(update={"score": 8, "note": "Thin edge - slippage may erase it live."})
    if v < 0.005:
        return base.model_copy(update={"score": 14, "note": "Reasonable per-trade edge."})
    if v < 0.02:
        return base.model_copy(update={"score": 20, "note": "Strong per-trade edge."})
    return base.model_copy(update={"score": 18, "note": "Very high expectancy - sanity-check the trade log."})


def _grade_win_rate_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("win_rate", "Win rate", None, v, "-" if v is None else f"{v * 100:.2f}%", "")
    if v is None:
        return base.model_copy(update={"note": "Not computed."})
    if v < 0.3:
        return base.model_copy(update={"score": 4, "note": "Very low - needs outsized payoff to compensate."})
    if v < 0.5:
        return base.model_copy(update={"score": 10, "note": "Trend-style range - pair with payoff > 2x."})
    if v < 0.55:
        return base.model_copy(update={"score": 14, "note": "Below typical mean-reversion range."})
    if v < 0.75:
        return base.model_copy(update={"score": 20, "note": "Classic mean-reversion range."})
    if v < 0.85:
        return base.model_copy(update={"score": 16, "note": "Very high - confirm with larger sample."})
    return base.model_copy(
        update={
            "score": 6,
            "note": "Extreme win rate; inspect sample size, payoff, leakage, and fill assumptions.",
        }
    )


def _grade_payoff_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("payoff", "Payoff ratio", None, v, "-" if v is None else f"{v:.2f}", "")
    if v is None:
        return base.model_copy(update={"note": "Needs average win + average loss from trade stats."})
    if v < 0.5:
        return base.model_copy(update={"score": 4, "note": "Avg loser is 2x the avg winner - fragile edge."})
    if v < 1.0:
        return base.model_copy(update={"score": 10, "note": "Below 1.0 - edge depends entirely on hit-rate."})
    if v < 1.5:
        return base.model_copy(update={"score": 15, "note": "Typical for mean-reversion."})
    if v < 3.0:
        return base.model_copy(update={"score": 20, "note": "Asymmetric winners - robust edge."})
    return base.model_copy(update={"score": 16, "note": "Very asymmetric - verify it's not one whale trade."})


def _grade_fee_drag_sub(net_profit: float | None, fees: float | None) -> RunVerdictSubScore:
    base = _sub("fee_drag", "Fee drag on gross", None, None, "-", "")
    if net_profit is None or fees is None:
        return base.model_copy(update={"note": "Net profit or fee total unavailable."})
    gross = net_profit + fees
    if gross <= 0:
        return base.model_copy(update={"score": 0, "note": "Gross profit non-positive - fees are not the limiting factor."})
    drag = fees / gross
    base = base.model_copy(update={"raw_value": drag, "display": f"{drag * 100:.2f}%"})
    if drag < 0.05:
        return base.model_copy(update={"score": 20, "note": "Fees barely touch gross profit."})
    if drag < 0.15:
        return base.model_copy(update={"score": 16, "note": "Healthy fee efficiency."})
    if drag < 0.30:
        return base.model_copy(update={"score": 11, "note": "Fees taking a noticeable bite - stress-test at higher cost."})
    if drag < 0.50:
        return base.model_copy(update={"score": 5, "note": "Fees eating half the edge - fragile live."})
    return base.model_copy(update={"score": 1, "note": "Fees dominate - strategy won't survive realistic costs."})


def _grade_psr_sub(v: float | None) -> RunVerdictSubScore:
    base = _sub("psr", "Probabilistic Sharpe", None, v, "-" if v is None else f"{v * 100:.2f}%", "")
    if v is None:
        return base.model_copy(update={"note": "Not yet computed by engine."})
    if v < 0.5:
        return base.model_copy(update={"score": 2, "note": "Cannot distinguish strategy from noise."})
    if v < 0.8:
        return base.model_copy(update={"score": 8, "note": "Weak statistical confidence."})
    if v < 0.95:
        return base.model_copy(update={"score": 14, "note": "Approaching the 95% threshold."})
    if v < 0.99:
        return base.model_copy(update={"score": 20, "note": "High statistical confidence."})
    return base.model_copy(update={"score": 18, "note": "Near-certain - verify sample size isn't inflated."})


def _grade_sample_size_sub(n: float | None) -> RunVerdictSubScore:
    base = _sub("sample", "Sample size (trades)", None, n, "-" if n is None else f"{n:g}", "")
    if n is None:
        return base.model_copy(update={"note": "Trade count unavailable."})
    if n < 20:
        return base.model_copy(update={"score": 2, "note": "Too few trades to draw any conclusion."})
    if n < 50:
        return base.model_copy(update={"score": 7, "note": "Thin - run on a longer window."})
    if n < 100:
        return base.model_copy(update={"score": 13, "note": "Reasonable sample - CI still wide."})
    if n < 250:
        return base.model_copy(update={"score": 18, "note": "Robust sample."})
    return base.model_copy(update={"score": 20, "note": "Large sample - statistical power is solid."})


def _grade_skepticism_sub(sharpe: float | None, pf: float | None, win_rate: float | None) -> RunVerdictSubScore:
    base = _sub("skepticism", "Skepticism penalty", None, None, "-", "")
    if sharpe is None and pf is None and win_rate is None:
        return base.model_copy(update={"note": "Need at least one of Sharpe, PF, or Win Rate."})
    score = 20
    flags: list[str] = []
    if sharpe is not None and sharpe > 3.0:
        score -= 8
        flags.append("Sharpe > 3")
    if pf is not None and math.isfinite(pf) and pf > 4.0:
        score -= 6
        flags.append("PF > 4")
    if win_rate is not None and win_rate > 0.85:
        score -= 6
        flags.append("Win rate > 85%")
    return base.model_copy(
        update={
            "score": max(0, score),
            "display": "Clean" if not flags else " · ".join(flags),
            "note": "None of the skepticism thresholds tripped."
            if not flags
            else (
                f"Investigation triggers: {', '.join(flags)}. Inspect out-of-sample evidence, "
                "sampling, and fill assumptions; these thresholds do not establish a cause."
            ),
        }
    )


def _grade_trade_gap_sub(portfolio: float | None, trade: float | None) -> RunVerdictSubScore:
    base = _sub("trade_gap", "Trade vs Portfolio Sharpe gap", None, None, "-", "")
    if portfolio is None or trade is None:
        return base.model_copy(update={"note": "Needs both Portfolio Sharpe and Trade Sharpe."})
    gap = trade - portfolio
    base = base.model_copy(update={"raw_value": gap, "display": f"{gap:.2f}"})
    if gap < 1.0:
        return base.model_copy(update={"score": 20, "note": "Low sequencing risk."})
    if gap < 2.0:
        return base.model_copy(update={"score": 16, "note": "Modest sequencing risk."})
    if gap < 3.0:
        return base.model_copy(update={"score": 12, "note": "Capital spends long periods idle."})
    if gap < 5.0:
        return base.model_copy(update={"score": 6, "note": "Elevated sequencing risk."})
    return base.model_copy(update={"score": 2, "note": "Severe gap - performance bursts between long idle periods."})


def _score_required_subs(
    subs: list[RunVerdictSubScore],
    required_keys: frozenset[str],
) -> int | None:
    required = [sub for sub in subs if sub.key in required_keys]
    if len(required) != len(required_keys):
        missing_keys = sorted(required_keys - {sub.key for sub in required})
        raise ValueError(f"Run verdict definition is missing required sub-scores: {missing_keys}")
    if any(sub.score is None for sub in required):
        return None
    return _round_half_up((sum(sub.score or 0 for sub in required) / (len(required) * 20)) * 100)


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        parsed = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed):
        return None
    return parsed


def _ratio_display(v: float | None) -> str:
    if v is None:
        return "-"
    if not math.isfinite(v):
        return "∞"
    return f"{v:.2f}"
