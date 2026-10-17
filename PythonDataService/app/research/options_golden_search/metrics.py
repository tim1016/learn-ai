"""Canonical Options search metrics from retained minute and daily inputs (#3332).

Formula: DD = max_t(running_high(equity) - equity_t) / fixed starting capital;
  x_d = net daily dollars / fixed starting capital; Sharpe = sqrt(A) mean(x-rf) / stdev(x-rf, ddof=1).
  Recovery counts distinct sessions with below-peak minute observations until that peak is regained.
Reference: owner-defined fixed-capital, calendar annualization and recovery conventions in #3332.
Canonical implementation: this file. Stock peak-relative/account-return metrics keep their own semantics.
Validated against: tests/research/options_golden_search/test_metrics.py and test_evaluator.py.

Dollar accumulation is exact. Ratios use 50 decimal digits; Sharpe alone uses binary64.
Annualization is descriptive and assumes time-scaling conditions; serial dependence can invalidate them.
An incomplete trace is a technical failure even if the final session profit is positive.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Context, Decimal, localcontext
from itertools import pairwise
from statistics import fmean, stdev
from typing import Any

from app.lean_sidecar.trading_calendar import session_window_for_date, session_windows_ms_utc
from app.options_backtest import fills
from app.options_backtest.capital_evidence import CapitalEvidence, CapitalSession, capital_evidence
from app.options_backtest.observation import MINUTE_OBSERVATION_ADAPTER, MinuteObservation
from app.options_backtest.results import (
    ENTRY_REFUSAL_EVENTS,
    SESSION_OUTCOME_ADAPTER,
    SessionOutcome,
    SessionStatus,
    SkipReason,
)
from app.options_backtest.setup import MS_PER_MINUTE
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

METRIC_VERSION = "options-fixed-capital-minute-v1"
_RATIO = Context(prec=50)


@dataclass(frozen=True)
class SessionEvidence:
    session_open_ms: int
    session_close_ms: int
    cash_before_usd: Decimal
    outcome: SessionOutcome
    observations: tuple[MinuteObservation, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_MS for value in (self.session_open_ms, self.session_close_ms)):
            raise ValueError("session boundaries must be bounded int64 UTC milliseconds")
        window = session_window_for_date(et_date_at_ms(self.session_open_ms))
        if (self.session_open_ms, self.session_close_ms) != (window.open_ms_utc, window.close_ms_utc):
            raise ValueError("evidence boundaries must equal the canonical session window")
        if not self.cash_before_usd.is_finite():
            raise ValueError("session starting cash must be finite")


@dataclass(frozen=True)
class RecoverySpell:
    peak_at_ms: int
    recovered_at_ms: int | None
    below_peak_sessions: int


@dataclass(frozen=True)
class DailyProfit:
    session_open_ms: int
    net_profit_usd: Decimal
    fixed_capital_return: float
    traded: bool


@dataclass(frozen=True)
class MonthlyProfit:
    first_session_open_ms: int
    net_profit_usd: Decimal
    completed_sessions: int


@dataclass(frozen=True)
class QualificationMetrics:
    net_profit_usd: Decimal
    max_drawdown_usd: Decimal
    max_drawdown_fraction: Decimal
    sharpe: float | None
    sharpe_unavailable_reason: str | None
    annualization_sessions: float
    daily_observation_count: int
    completed_traded_sessions: int
    max_recovery_sessions: int
    ongoing_recovery: bool
    technical_failures: tuple[str, ...]
    daily: tuple[DailyProfit, ...]
    recoveries: tuple[RecoverySpell, ...]
    monthly: tuple[MonthlyProfit, ...]
    winning_sessions: int
    losing_sessions: int
    breakeven_sessions: int
    no_trade_sessions: int
    skipped_sessions: int
    refused_entries: int
    average_winning_session_usd: Decimal | None
    average_losing_session_usd: Decimal | None
    expectancy_usd: Decimal | None
    largest_winning_session_usd: Decimal | None
    largest_losing_session_usd: Decimal | None
    profit_factor: Decimal | None
    average_win_loss_ratio: Decimal | None
    longest_winning_streak: int
    longest_losing_streak: int
    top_five_profit_contribution: Decimal | None
    profit_without_best_session_usd: Decimal | None
    peak_relative_drawdown_fraction: Decimal
    session_end_drawdown_usd: Decimal
    required_held_marks: int
    missing_held_marks: int
    capital_evidence: CapitalEvidence | None
    winning_session_rate: Decimal | None
    losing_session_rate: Decimal | None
    breakeven_session_rate: Decimal | None


@dataclass(frozen=True)
class PairedDailyComparison:
    paired_common_sessions: int
    paired_net_difference_usd: Decimal | None
    candidate_beats_baseline: bool | None
    reason: str | None


def paired_daily_comparison(
    candidate: QualificationMetrics, baseline: QualificationMetrics,
) -> PairedDailyComparison:
    """Compare dated daily dollars descriptively; this never adds a qualification gate.

    Formula: paired difference = Σ common complete dates (candidate daily net - baseline daily net).
    Reference: #3332 adversarial amendment, finding 9.
    Canonical implementation: this file.
    Validated against: tests/research/options_golden_search/test_metrics.py.
    """
    candidate_days = {day.session_open_ms: day.net_profit_usd for day in candidate.daily}
    baseline_days = {day.session_open_ms: day.net_profit_usd for day in baseline.daily}
    common = sorted(candidate_days.keys() & baseline_days.keys())
    if not common:
        return PairedDailyComparison(0, None, None, "no_common_complete_sessions")
    with localcontext(fills.EXACT):
        difference = sum((candidate_days[day] - baseline_days[day] for day in common), Decimal(0))
    if candidate.technical_failures or baseline.technical_failures:
        return PairedDailyComparison(len(common), difference, None, "technically_unverifiable_evidence")
    if candidate_days.keys() != baseline_days.keys():
        return PairedDailyComparison(len(common), difference, None, "incomplete_paired_session_coverage")
    return PairedDailyComparison(len(common), difference, difference > 0, None)


def calendar_annualization(session_ms: Sequence[int]) -> float:
    """Average scheduled sessions over all complete calendar years touched by the declared window.

    Formula: A = Σ scheduled_sessions(year) / number_of_years.
    Reference: owner-selected convention in #3332; canonical NYSE calendar.
    Canonical implementation: this file.
    Validated against: tests/research/options_golden_search/test_metrics.py.
    """
    if not session_ms:
        raise ValueError("annualization needs at least one scheduled session")
    first, last = et_date_at_ms(min(session_ms)).year, et_date_at_ms(max(session_ms)).year
    counts = [len(session_windows_ms_utc(date(year, 1, 1), date(year, 12, 31))) for year in range(first, last + 1)]
    return fmean(counts)


def _ratio(numerator: Decimal, denominator: Decimal | int) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext(_RATIO):
        return numerator / denominator


def summarize_evidence(
    sessions: Sequence[SessionEvidence], *, starting_capital_usd: Decimal,
    annualization_sessions: float, risk_free_daily_return: float = 0,
) -> QualificationMetrics:
    """Recompute metrics and validity from exact persisted trace and canonical session outcomes."""
    if not starting_capital_usd.is_finite() or starting_capital_usd <= 0:
        raise ValueError("starting capital must be finite and positive")
    if not math.isfinite(annualization_sessions) or annualization_sessions <= 0:
        raise ValueError("annualization must be finite and positive")
    if not math.isfinite(risk_free_daily_return):
        raise ValueError("daily risk-free return must be finite")
    if any(b.session_open_ms <= a.session_close_ms for a, b in pairwise(sessions)):
        raise ValueError("evidence sessions must be chronological and disjoint")
    with localcontext(fills.EXACT):
        return _summarize(sessions, starting_capital_usd, annualization_sessions, risk_free_daily_return)


def _summarize(
    sessions: Sequence[SessionEvidence], capital: Decimal, annualization: float, risk_free: float,
) -> QualificationMetrics:
    failures: list[str] = []
    daily: list[DailyProfit] = []
    recoveries: list[RecoverySpell] = []
    peak = capital
    peak_at = sessions[0].session_open_ms if sessions else 0
    below_sessions: set[int] = set()
    drawdown = peak_relative = end_drawdown = Decimal(0)
    end_peak = capital
    expected_cash = capital
    required_marks = missing_marks = 0
    monthly: dict[tuple[int, int], MonthlyProfit] = {}
    capital_sessions: list[CapitalSession] = []
    for session in sessions:
        tag = str(session.session_open_ms)
        expected_times = tuple(range(session.session_open_ms, session.session_close_ms + MS_PER_MINUTE, MS_PER_MINUTE))
        if tuple(point.at_ms for point in session.observations) != expected_times:
            failures.append(f"{tag}:incomplete_minute_trace")
        if session.cash_before_usd != expected_cash:
            failures.append(f"{tag}:cash_carry_mismatch")
        if session.outcome.status is SessionStatus.INCOMPLETE:
            failures.append(f"{tag}:incomplete_session")
        if session.outcome.trades and not session.outcome.money_steps:
            failures.append(f"{tag}:missing_capital_money_steps")
        else:
            capital_sessions.append(CapitalSession.of_outcome(session.session_open_ms, session.outcome))
        if session.outcome.skip_reason is SkipReason.UNDERLYING_DID_NOT_TRADE:
            failures.append(f"{tag}:missing_entry_underlying")
        if session.observations:
            initial = session.observations[0]
            if initial.held or initial.cash_usd != session.cash_before_usd:
                failures.append(f"{tag}:initial_equity_mismatch")
        for point in session.observations:
            required_marks += len(point.held)
            missing_marks += len(point.missing_leg_ids)
            equity = point.equity_usd
            if equity is None:
                failures.append(f"{point.at_ms}:missing_held_mark:{','.join(point.missing_leg_ids)}")
                continue
            if not equity.is_finite():
                failures.append(f"{point.at_ms}:nonfinite_equity")
                continue
            if equity >= peak:
                if below_sessions:
                    recoveries.append(RecoverySpell(peak_at, point.at_ms, len(below_sessions)))
                    below_sessions = set()
                peak, peak_at = equity, point.at_ms
            else:
                below_sessions.add(session.session_open_ms)
            fall = peak - equity
            drawdown = max(drawdown, fall)
            if peak > 0:
                peak_relative = max(peak_relative, _ratio(fall, peak) or Decimal(0))
        pnl = session.outcome.pnl_usd
        expected_cash += pnl
        if session.observations and session.observations[-1].equity_usd != expected_cash:
            failures.append(f"{tag}:daily_equity_reconciliation")
        end_peak = max(end_peak, expected_cash)
        end_drawdown = max(end_drawdown, end_peak - expected_cash)
        if (
            session.outcome.status in (SessionStatus.TRADED, SessionStatus.NO_TRADE)
            or session.outcome.skip_reason is SkipReason.WINDOW_DOES_NOT_FIT
        ):
            daily.append(DailyProfit(session.session_open_ms, pnl, float(_ratio(pnl, capital)), bool(session.outcome.trades)))
            day = et_date_at_ms(session.session_open_ms)
            month = (day.year, day.month)
            previous = monthly.get(month, MonthlyProfit(session.session_open_ms, Decimal(0), 0))
            monthly[month] = MonthlyProfit(
                previous.first_session_open_ms, previous.net_profit_usd + pnl,
                previous.completed_sessions + int(bool(session.outcome.trades)),
            )
    if below_sessions:
        recoveries.append(RecoverySpell(peak_at, None, len(below_sessions)))
    profits = [item.net_profit_usd for item in daily if item.traded]
    daily_profits = [item.net_profit_usd for item in daily]
    wins, losses = [p for p in profits if p > 0], [p for p in profits if p < 0]
    net = sum((session.outcome.pnl_usd for session in sessions), Decimal(0))
    gross_win, gross_loss = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
    avg_win, avg_loss = _ratio(gross_win, len(wins)), _ratio(-gross_loss, len(losses))
    returns = [item.fixed_capital_return - risk_free for item in daily]
    sharpe, reason = _sharpe(returns, annualization)
    win_streak = loss_streak = max_win_streak = max_loss_streak = 0
    for pnl in profits:
        win_streak = win_streak + 1 if pnl > 0 else 0
        loss_streak = loss_streak + 1 if pnl < 0 else 0
        max_win_streak, max_loss_streak = max(max_win_streak, win_streak), max(max_loss_streak, loss_streak)
    capital_result = capital_evidence(capital_sessions, starting_cash=capital) if len(capital_sessions) == len(sessions) else None
    if capital_result is not None and capital_result.ended_by is not None:
        failures.append(f"{capital_result.ended_by.session_open_ms}:capital_invalid:{capital_result.ended_by.kind.value}")
    return QualificationMetrics(
        net_profit_usd=net, max_drawdown_usd=drawdown,
        max_drawdown_fraction=_ratio(drawdown, capital) or Decimal(0),
        sharpe=sharpe, sharpe_unavailable_reason=reason, annualization_sessions=annualization,
        daily_observation_count=len(daily), completed_traded_sessions=sum(item.traded for item in daily),
        max_recovery_sessions=max((spell.below_peak_sessions for spell in recoveries), default=0),
        ongoing_recovery=bool(below_sessions), technical_failures=tuple(failures),
        daily=tuple(daily), recoveries=tuple(recoveries), monthly=tuple(monthly.values()),
        winning_sessions=len(wins), losing_sessions=len(losses), breakeven_sessions=profits.count(Decimal(0)),
        no_trade_sessions=sum(session.outcome.status is SessionStatus.NO_TRADE for session in sessions),
        skipped_sessions=sum(session.outcome.status is SessionStatus.SKIPPED for session in sessions),
        refused_entries=sum(
            event.kind in ENTRY_REFUSAL_EVENTS.values() for session in sessions for event in session.outcome.events
        ),
        average_winning_session_usd=avg_win, average_losing_session_usd=avg_loss,
        expectancy_usd=_ratio(sum(profits, Decimal(0)), len(profits)),
        largest_winning_session_usd=max(wins, default=None), largest_losing_session_usd=min(losses, default=None),
        profit_factor=_ratio(gross_win, gross_loss),
        average_win_loss_ratio=None if avg_win is None or avg_loss is None else _ratio(avg_win, -avg_loss),
        longest_winning_streak=max_win_streak, longest_losing_streak=max_loss_streak,
        top_five_profit_contribution=_ratio(sum(sorted(wins, reverse=True)[:5], Decimal(0)), net) if net > 0 else None,
        profit_without_best_session_usd=None if not daily_profits else sum(daily_profits, Decimal(0)) - max(daily_profits),
        peak_relative_drawdown_fraction=peak_relative, session_end_drawdown_usd=end_drawdown,
        required_held_marks=required_marks, missing_held_marks=missing_marks,
        capital_evidence=capital_result,
        winning_session_rate=_ratio(Decimal(len(wins)), len(profits)),
        losing_session_rate=_ratio(Decimal(len(losses)), len(profits)),
        breakeven_session_rate=_ratio(Decimal(profits.count(Decimal(0))), len(profits)),
    )


def _sharpe(returns: Sequence[float], annualization: float) -> tuple[float | None, str | None]:
    if len(returns) < 2:
        return None, "insufficient_daily_observations"
    if not all(math.isfinite(value) for value in returns):
        return None, "numeric_range"
    deviation = stdev(returns)
    if deviation == 0:
        return None, "constant_daily_returns"
    value = math.sqrt(annualization) * fmean(returns) / deviation
    return (value, None) if math.isfinite(value) else (None, "numeric_range")


def evidence_to_json(evidence: SessionEvidence) -> dict[str, Any]:
    """Version-independent canonical facts; exact dollars serialize as decimal strings."""
    return {
        "session_open_ms": evidence.session_open_ms,
        "session_close_ms": evidence.session_close_ms,
        "cash_before_usd": str(evidence.cash_before_usd),
        "outcome": SESSION_OUTCOME_ADAPTER.dump_python(evidence.outcome, mode="json"),
        "observations": [MINUTE_OBSERVATION_ADAPTER.dump_python(point, mode="json") for point in evidence.observations],
    }


def evidence_from_json(payload: dict[str, Any]) -> SessionEvidence:
    """Read retained facts with strict JSON types before qualification recomputation."""
    return SessionEvidence(
        session_open_ms=payload["session_open_ms"], session_close_ms=payload["session_close_ms"],
        cash_before_usd=Decimal(payload["cash_before_usd"]),
        outcome=SESSION_OUTCOME_ADAPTER.validate_json(json.dumps(payload["outcome"], allow_nan=False), strict=True),
        observations=tuple(
            MINUTE_OBSERVATION_ADAPTER.validate_json(json.dumps(point, allow_nan=False), strict=True)
            for point in payload["observations"]
        ),
    )


def stitch_fold_evidence(
    folds: Sequence[Sequence[SessionEvidence]], *, starting_capital_usd: Decimal,
) -> tuple[SessionEvidence, ...]:
    """Translate reset-capital folds into the synthetic procedure equity path specified by #3332.

    Formula: synthetic equity_j(t) = C + Σ earlier fold P&L + (equity_j(t) - C).
    Reference: #3332 adversarial amendment, finding 2.
    Canonical implementation: this file.
    Validated against: tests/research/options_golden_search/test_metrics.py.
    """
    stitched: list[SessionEvidence] = []
    with localcontext(fills.EXACT):
        prior_profit = Decimal(0)
        for fold in folds:
            if not fold:
                raise ValueError("a missing fold cannot become a zero-return fold")
            if fold[0].cash_before_usd != starting_capital_usd:
                raise ValueError("each independent fold must start at the frozen capital")
            if stitched and fold[0].session_open_ms <= stitched[-1].session_close_ms:
                raise ValueError("forward folds must be chronological and disjoint")
            for session in fold:
                stitched.append(replace(
                    session, cash_before_usd=session.cash_before_usd + prior_profit,
                    observations=tuple(replace(point, cash_usd=point.cash_usd + prior_profit) for point in session.observations),
                ))
            prior_profit += sum((session.outcome.pnl_usd for session in fold), Decimal(0))
    return tuple(stitched)
