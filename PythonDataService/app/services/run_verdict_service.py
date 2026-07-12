"""Run verdict authoring for Engine Lab.

Formula: Weighted production-readiness composite from five 0-100 dimensions
  with unavailable sub-scores omitted and scored dimensions reweighted.
Reference: Frontend/src/app/components/lean-engine/readiness-score-card/
  readiness-score.util.ts at commit fe0e9e1c1, intentionally ported before
  deleting the frontend scorer.
Canonical implementation: this file.
Validated against: PythonDataService/tests/services/test_run_verdict_parity.py.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

from app.schemas.run_verdict import (
    EngineKind,
    RunVerdict,
    RunVerdictCleanliness,
    RunVerdictDimension,
    RunVerdictInput,
    RunVerdictSubScore,
)

RUN_VERDICT_VERSION = 1


def compute_run_verdict(
    payload: RunVerdictInput | Mapping[str, Any] | None,
    *,
    engine: EngineKind,
    generated_at_ms: int | None = None,
    cleanliness: RunVerdictCleanliness | Mapping[str, Any] | None = None,
) -> RunVerdict:
    data = _coerce_input(payload)
    generated = generated_at_ms if generated_at_ms is not None else int(time.time() * 1000)
    clean = _coerce_cleanliness(cleanliness)

    if data is None or data.statistics is None:
        verdict = _empty_verdict(
            headline="Run a backtest to generate a Production Readiness score.",
            engine=engine,
            generated_at_ms=generated,
            cleanliness=clean,
        )
        return _apply_cleanliness(verdict)

    dimensions = [
        _score_return_quality(data),
        _score_risk_control(data),
        _score_trade_edge(data),
        _score_statistical_confidence(data, engine),
        _score_alpha_calibration(),
    ]
    missing_metrics = [
        f"{dimension.label}: {sub.label}"
        for dimension in dimensions
        for sub in dimension.sub_scores
        if sub.score is None
    ]
    scored = [dimension for dimension in dimensions if dimension.score is not None]
    total_weight = sum(d.weight for d in scored)
    normalized_weights = total_weight > 0 and abs(total_weight - 1) > 1e-6

    if not scored or total_weight == 0:
        return RunVerdict(
            verdict_version=RUN_VERDICT_VERSION,
            engine=engine,
            generated_at_ms=generated,
            composite=None,
            grade=None,
            signal=None,
            headline="Not enough data to grade.",
            red_flags=[],
            dimensions=dimensions,
            missing_metrics=missing_metrics,
            normalized_weights=False,
            cleanliness=clean,
        )

    composite = _round_half_up(sum((d.score or 0) * (d.weight / total_weight) for d in scored))
    grade, signal, headline = _grade_and_signal(composite, len(missing_metrics))
    verdict = RunVerdict(
        verdict_version=RUN_VERDICT_VERSION,
        engine=engine,
        generated_at_ms=generated,
        composite=composite,
        grade=grade,
        signal=signal,
        headline=headline,
        red_flags=[],
        dimensions=dimensions,
        missing_metrics=missing_metrics,
        normalized_weights=normalized_weights,
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
    return verdict.model_copy(
        update={
            "composite": 0,
            "grade": "F",
            "signal": "Reject",
            "headline": "Reject - " + verdict.headline,
            "red_flags": ["lean_run_failed"],
        }
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
        engine=engine,
        generated_at_ms=generated_at_ms,
        composite=None,
        grade=None,
        signal=None,
        headline=headline,
        red_flags=[],
        dimensions=[],
        missing_metrics=[],
        normalized_weights=False,
        cleanliness=cleanliness,
    )


def _apply_cleanliness(verdict: RunVerdict) -> RunVerdict:
    if verdict.cleanliness is None or verdict.cleanliness.is_clean:
        return verdict
    headline = "LEAN run is not reconciliation-clean. " + verdict.headline
    return verdict.model_copy(
        update={
            "signal": "Rework",
            "headline": headline,
            "red_flags": [*verdict.red_flags, "lean_run_not_clean"],
        }
    )


def _score_return_quality(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    lean = _lean_portfolio(r)
    sub_scores = [
        _grade_sharpe_sub(_first_not_none(_num(stats.get("sharpe_ratio")), _num(lean.get("sharpe_ratio")))),
        _grade_sortino_sub(_first_not_none(_num(stats.get("sortino_ratio")), _num(lean.get("sortino_ratio")))),
        _grade_cagr_sub(_num(lean.get("compounding_annual_return"))),
        _grade_calmar_sub(
            _first_not_none(_num(stats.get("max_drawdown_pct")), _num(lean.get("drawdown"))),
            _num(lean.get("compounding_annual_return")),
        ),
        _grade_annual_vol_sub(_num(lean.get("annual_standard_deviation"))),
    ]
    return _dimension(
        "return_quality",
        "Return Quality",
        0.25,
        sub_scores,
        "Does the strategy make money efficiently per unit of risk?",
    )


def _score_risk_control(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    lean = _lean_portfolio(r)
    trade = _lean_trade(r)
    sub_scores = [
        _grade_max_drawdown_sub(_first_not_none(_num(stats.get("max_drawdown_pct")), _num(lean.get("drawdown")))),
        _grade_recovery_sub(_num(lean.get("drawdown_recovery"))),
        _grade_consecutive_losses_sub(_num(trade.get("max_consecutive_losing_trades"))),
        _sub("dd_duration", "Drawdown duration", None, None, "-", "Not yet computed - needs equity-curve timestamps."),
        _sub("downside_vol", "Downside volatility", None, None, "-", "Planned - uses Sortino's sigma_d separately."),
    ]
    return _dimension(
        "risk_control",
        "Risk Control",
        0.20,
        sub_scores,
        "Does the strategy preserve capital when it's wrong?",
    )


def _score_trade_edge(r: RunVerdictInput) -> RunVerdictDimension:
    stats = r.statistics or {}
    trade = _lean_trade(r)
    payoff = _payoff_ratio(_num(trade.get("average_profit")), _num(trade.get("average_loss")))
    sub_scores = [
        _grade_profit_factor_sub(_first_not_none(_num(stats.get("profit_factor")), _num(trade.get("profit_factor")))),
        _grade_expectancy_sub(_num(stats.get("expectancy_pct"))),
        _grade_win_rate_sub(_num(r.win_rate)),
        _grade_payoff_sub(payoff),
        _grade_fee_drag_sub(_num(r.net_profit), _num(r.total_fees)),
    ]
    return _dimension("trade_edge", "Trade Edge", 0.20, sub_scores, "Is there a real per-trade edge after costs?")


def _score_statistical_confidence(r: RunVerdictInput, engine: EngineKind) -> RunVerdictDimension:
    stats = r.statistics or {}
    lean = _lean_portfolio(r)
    trade = _lean_trade(r)
    portfolio_sharpe = _first_not_none(_num(stats.get("sharpe_ratio")), _num(lean.get("sharpe_ratio")))
    trade_sharpe = _num(trade.get("sharpe_ratio"))
    if engine == "lean" and trade_sharpe == 0:
        trade_sharpe = None
    sub_scores = [
        _grade_psr_sub(_num(lean.get("probabilistic_sharpe_ratio"))),
        _grade_sample_size_sub(_num(r.total_trades)),
        _grade_skepticism_sub(
            portfolio_sharpe,
            _first_not_none(_num(stats.get("profit_factor")), _num(trade.get("profit_factor"))),
            _num(r.win_rate),
        ),
        _grade_trade_gap_sub(portfolio_sharpe, trade_sharpe),
        _sub("benchmark", "Benchmark outperformance", None, None, "-", "Planned - requires a Buy-and-Hold return series alongside the backtest."),
    ]
    return _dimension(
        "stat_confidence",
        "Statistical Confidence",
        0.20,
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
        weight=0.15,
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
    return RunVerdictDimension(
        key=key,
        label=label,
        weight=weight,
        score=_average_subs(sub_scores),
        sub_scores=sub_scores,
        summary=summary,
    )


def _lean_portfolio(r: RunVerdictInput) -> Mapping[str, Any]:
    lean = r.lean_statistics or {}
    portfolio = lean.get("portfolio") if isinstance(lean, Mapping) else None
    return portfolio if isinstance(portfolio, Mapping) else {}


def _lean_trade(r: RunVerdictInput) -> Mapping[str, Any]:
    lean = r.lean_statistics or {}
    trade = lean.get("trade") if isinstance(lean, Mapping) else None
    return trade if isinstance(trade, Mapping) else {}


def _grade_and_signal(score: int, missing_count: int) -> tuple[str, str, str]:
    if score >= 85:
        grade, signal, headline = "A+", "Deploy", "Institutional-grade. Ready for live deployment at standard size."
    elif score >= 70:
        grade, signal, headline = "A", "Paper-trade", "Strong backtest. Paper-trade for 30 days before sizing up."
    elif score >= 55:
        grade, signal, headline = "B", "Iterate", "Promising edge, but specific weaknesses need addressing before deployment."
    elif score >= 40:
        grade, signal, headline = "C", "Rework", "Material problems — core parameters or logic need revisiting."
    elif score >= 25:
        grade, signal, headline = "D", "Rework", "Fundamental issues. Rework the thesis, not just the parameters."
    else:
        grade, signal, headline = "F", "Reject", "Reject — the backtest does not clear baseline viability."
    if missing_count > 5:
        headline += f" {missing_count} sub-scores unavailable; grade may move once missing metrics are computed."
    return grade, signal, headline


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
    return base.model_copy(update={"score": 12, "note": "Suspiciously high (>3.0) - likely overfit."})


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
    return base.model_copy(update={"score": 10, "note": "PF > 4 is rare OOS - assume overfit until proven."})


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
    return base.model_copy(update={"score": 6, "note": "Above 85% is a data-leak red flag."})


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
            else f"Skeptical thresholds tripped: {', '.join(flags)}. Verify OOS and check for look-ahead bias.",
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


def _average_subs(subs: list[RunVerdictSubScore]) -> int | None:
    scored = [sub for sub in subs if isinstance(sub.score, int)]
    if not scored:
        return None
    return _round_half_up((sum(sub.score or 0 for sub in scored) / (len(scored) * 20)) * 100)


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


def _first_not_none(*values: float | None) -> float | None:
    for value in values:
        if value is not None:
            return value
    return None


def _ratio_display(v: float | None) -> str:
    if v is None:
        return "-"
    if not math.isfinite(v):
        return "∞"
    return f"{v:.2f}"


def _payoff_ratio(avg_win: float | None, avg_loss: float | None) -> float | None:
    if avg_win is None or avg_loss is None or avg_loss == 0:
        return None
    return abs(avg_win / avg_loss)
