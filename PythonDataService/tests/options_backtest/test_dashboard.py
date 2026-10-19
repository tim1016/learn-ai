"""Independent session receipts, plus parity with Options Golden Search's daily convention."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.options_backtest.dashboard import dashboard_statistics, profit_histogram
from app.options_backtest.results import SessionStatus
from app.options_backtest.run import run_sessions
from app.options_backtest.statistics import DayRow, Drawdown, Ratio, RunSummary
from app.research.options_golden_search.metrics import summarize_evidence
from app.schemas.options_backtest_runs import OptionsBacktestDashboardResponse
from tests.options_backtest._run_data import FRI, THU, WED, fake_data
from tests.options_backtest.test_service import SESSION_OF_THE_RANGE, the_scene, the_setup
from tests.research.options_golden_search._evidence import session_evidence

D = Decimal


@pytest.fixture(scope="module")
def base() -> RunSummary:
    return run_sessions(the_setup(), SESSION_OF_THE_RANGE, fake_data(the_scene())).summary


def summary_with(base: RunSummary, profits: tuple[str, ...]) -> RunSummary:
    windows = session_windows_ms_utc(date(2026, 10, 5), date(2026, 10, 16))
    rows = tuple(DayRow(
        window.open_ms_utc, SessionStatus.NO_TRADE if value == "0" else SessionStatus.TRADED,
        None, 0 if value == "0" else 2, D(value), D(0), int(D(value) > 0), int(D(value) < 0), 0,
    ) for window, value in zip(windows[:len(profits)], profits, strict=True))
    return replace(base, by_day=rows, sessions_tested=len(rows), starting_cash_usd=D("10000"),
                   net_profit_usd=sum((row.net_profit_usd for row in rows), D(0)), drawdown_by_dollars=None)


def test_fixed_capital_sharpe_includes_zero_days_and_has_hand_worked_sample_variance(base: RunSummary) -> None:
    summary = summary_with(base, ("100", "0", "-50"))
    dashboard = dashboard_statistics(summary)
    # Returns .01, 0, -.005: mean 1/600; sample variance 7/120000.
    expected = math.sqrt(dashboard.annualization_sessions) * (1 / 600) / math.sqrt(7 / 120000)
    assert dashboard.annualized_sharpe == pytest.approx(expected, abs=1e-12, rel=0)
    assert dashboard.annualized_sortino == pytest.approx(
        math.sqrt(dashboard.annualization_sessions) * (1 / 600) / math.sqrt(.005**2 / 3), abs=1e-12, rel=0,
    )
    assert dashboard.average_winning_session_usd == D("100.00")
    assert dashboard.average_losing_session_usd == D("-50.00")
    assert dashboard.average_session_usd == D("25.00")
    assert dashboard.session_profit_factor == "2.00"
    assert dashboard.daily_observations == 3


def test_report_and_search_share_the_same_sharpe_kernel_and_calendar(base: RunSummary) -> None:
    evidence = (session_evidence(WED, exit_price="2"),
                session_evidence(THU, cash="10100", exit_price="1", no_trade=True),
                session_evidence(FRI, cash="10100", exit_price="0.5"))
    summary = summary_with(base, ("100", "0", "-50"))
    rows = tuple(replace(row, session_open_ms=day.session_open_ms)
                 for row, day in zip(summary.by_day, evidence, strict=True))
    report = dashboard_statistics(replace(summary, by_day=rows))
    search = summarize_evidence(evidence, starting_capital_usd=D("10000"), annualization_sessions=report.annualization_sessions)
    assert report.annualized_sharpe == search.sharpe


def test_missing_daily_observation_never_becomes_a_zero_return(base: RunSummary) -> None:
    summary = summary_with(base, ("100", "0", "-50"))
    missing = replace(summary, by_day=(summary.by_day[0], summary.by_day[2]), sessions_tested=2)
    result = dashboard_statistics(missing)
    assert result.annualized_sharpe is result.annualized_sortino is None
    assert "every scheduled session" in result.risk_unavailable_reason
    assert len(result.daily) == 2
    first_missing = replace(summary, by_day=summary.by_day[1:], sessions_tested=2)
    result = dashboard_statistics(first_missing, first_session_open_ms=summary.by_day[0].session_open_ms)
    assert result.risk_unavailable_reason is not None
    last_missing = replace(summary, by_day=summary.by_day[:-1], sessions_tested=2)
    result = dashboard_statistics(last_missing, last_session_open_ms=summary.by_day[-1].session_open_ms)
    assert result.annualized_sharpe is None
    assert result.risk_unavailable_reason is not None


def test_saved_run_with_different_calendar_does_not_recalculate_annualized_risk(base: RunSummary) -> None:
    summary = summary_with(base, ("100", "-100", "0", "100"))
    fall = Drawdown(D("10100"), D("10000"), D("100"), Ratio(D("100"), D("10100")),
                    summary.by_day[0].session_open_ms, summary.by_day[1].session_open_ms)
    summary = replace(summary, drawdown_by_dollars=fall)
    result = dashboard_statistics(summary, recorded_calendar=("NYSE", "a-different-version"))
    assert result.annualization_sessions is None
    assert result.annualized_sharpe is result.annualized_sortino is result.annualized_volatility_percent is None
    assert all(day.rolling_sharpe is None for day in result.daily)
    assert "different trading calendar" in result.annualization_unavailable_reason
    assert result.risk_unavailable_reason is None
    assert result.recovery_sessions == 2
    assert result.longest_underwater_sessions == 2


def test_constant_and_incomplete_paths_are_explicitly_unavailable(base: RunSummary) -> None:
    summary = summary_with(base, ("0", "0"))
    result = dashboard_statistics(summary)
    assert result.annualized_sharpe is None
    assert result.sharpe_unavailable_reason == "constant_daily_returns"
    assert result.annualized_volatility_percent == 0
    assert result.average_winning_session_usd is result.average_losing_session_usd is None
    assert result.session_profit_factor is None
    marked = replace(summary.by_day[1], status=SessionStatus.INCOMPLETE)
    result = dashboard_statistics(replace(summary, by_day=(summary.by_day[0], marked)))
    assert result.annualized_sharpe is None
    assert "unclosed" in result.risk_unavailable_reason


def test_recovery_uses_the_recorded_peak_and_counts_no_trade_sessions(base: RunSummary) -> None:
    summary = summary_with(base, ("100", "-100", "0", "100"))
    fall = Drawdown(D("10100"), D("10000"), D("100"), Ratio(D("100"), D("10100")),
                    summary.by_day[0].session_open_ms, summary.by_day[1].session_open_ms)
    result = dashboard_statistics(replace(summary, drawdown_by_dollars=fall))
    assert result.recovery_sessions == 2
    assert result.recovered_session_open_ms == summary.by_day[3].session_open_ms
    assert result.longest_underwater_sessions == 2
    assert [day.underwater_usd for day in result.daily] == [0, -100, -100, 0]
    result = dashboard_statistics(replace(summary, by_day=summary.by_day[:3], sessions_tested=3, drawdown_by_dollars=fall))
    assert result.recovery_sessions is result.recovered_session_open_ms is None
    assert result.ongoing_underwater
    missing = replace(summary, by_day=(summary.by_day[0], summary.by_day[1], summary.by_day[3]),
                      sessions_tested=3, drawdown_by_dollars=fall)
    result = dashboard_statistics(missing)
    assert result.recovery_sessions is result.recovered_session_open_ms is None
    assert result.risk_unavailable_reason is not None


def test_exact_means_and_histogram_keep_money_beyond_float_precision(base: RunSummary) -> None:
    result = dashboard_statistics(summary_with(base, ("9007199254740993.01", "9007199254740993.02")))
    assert result.average_winning_session_usd == D("9007199254740993.02")  # half-even, exact .015 tie
    bins = profit_histogram([D("9007199254740993.01"), D("9007199254740993.02")])
    assert bins[0].lower_usd == D("9007199254740993.01")
    assert bins[-1].upper_usd == D("9007199254740993.02")
    assert bins[0].count == bins[-1].count == 1
    assert sum(bin.count for bin in bins) == 2
    sent = OptionsBacktestDashboardResponse.from_dashboard(result).model_dump(mode="json")
    assert sent["average_winning_session_usd"] == "9007199254740993.02"
    assert isinstance(sent["annualization_sessions"], str)


def test_rolling_sharpe_has_no_fabricated_warmup(base: RunSummary) -> None:
    windows = session_windows_ms_utc(date(2026, 9, 1), date(2026, 10, 16))[:21]
    template = summary_with(base, ("100",)).by_day[0]
    rows = tuple(replace(template, session_open_ms=window.open_ms_utc, net_profit_usd=D(100 if i % 2 else -50))
                 for i, window in enumerate(windows))
    result = dashboard_statistics(replace(base, by_day=rows, sessions_tested=len(rows), starting_cash_usd=D("10000")))
    assert all(day.rolling_sharpe is None for day in result.daily[:19])
    assert all(day.rolling_sharpe is not None for day in result.daily[19:])


def test_zero_capital_and_a_single_day_do_not_create_ratios(base: RunSummary) -> None:
    one_day = summary_with(base, ("100",))
    result = dashboard_statistics(one_day)
    assert result.annualized_sharpe is result.annualized_sortino is result.annualized_volatility_percent is None
    assert result.sharpe_unavailable_reason == "insufficient_daily_observations"
    result = dashboard_statistics(replace(one_day, starting_cash_usd=D(0)))
    assert result.annualized_sharpe is result.return_percent is None
    assert "Positive starting cash" in result.risk_unavailable_reason


def test_no_downside_keeps_sortino_undefined_and_negative_sharpe_stays_negative(base: RunSummary) -> None:
    result = dashboard_statistics(summary_with(base, ("100", "200")))
    assert result.annualized_sortino is None
    assert result.annualized_sharpe > 0
    result = dashboard_statistics(summary_with(base, ("-100", "-200")))
    assert result.annualized_sharpe < 0
    assert result.annualized_sortino < 0
    assert result.average_winning_session_usd is None
    assert result.session_profit_factor == "0.00"
