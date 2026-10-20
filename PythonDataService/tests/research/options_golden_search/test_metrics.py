"""Hand-worked fixed-capital/recovery/statistical receipts; no expectations derive from metrics.py."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.options_backtest.results import SessionOutcome, SessionStatus, SkipReason
from app.research.options_golden_search.metrics import (
    calendar_annualization,
    evidence_from_json,
    evidence_to_json,
    paired_daily_comparison,
    stitch_fold_evidence,
    summarize_evidence,
)
from tests.options_backtest._run_data import FRI, THU, WED, open_ms
from tests.research.options_golden_search._evidence import session_evidence

D = Decimal


def test_drawdown_uses_whole_minute_path_and_fixed_capital_and_recovery_counts_sessions_underwater() -> None:
    first = session_evidence(WED, exit_price="3", trough="0.5")
    second = session_evidence(THU, cash="10200", exit_price="2", trough="0.2")
    result = summarize_evidence((first, second), starting_capital_usd=D("10000"), annualization_sessions=251)
    # Day 1: capital 10000 -> 9950 -> 10200. Day 2: 10200 -> 10120 -> 10300.
    assert result.net_profit_usd == D("300"), "drawdown uses the whole minute path and fixed capital: net profit"
    assert result.max_drawdown_usd == D("80"), "drawdown uses the whole minute path and fixed capital: max drawdown usd"
    assert result.max_drawdown_fraction == D("0.008"), "drawdown uses the whole minute path and fixed capital: max drawdown fraction"
    assert float(result.peak_relative_drawdown_fraction) == pytest.approx(80 / 10200, abs=1e-12, rel=0), (
        "drawdown uses the whole minute path and fixed capital: peak-relative drawdown fraction"
    )
    assert result.session_end_drawdown_usd == 0, "drawdown is not the daily profit or peak: session-end drawdown"
    assert not result.technical_failures, "drawdown uses the whole minute path and fixed capital: no technical failures"
    assert [spell.below_peak_sessions for spell in result.recoveries] == [1, 1], "drawdown spells: sessions below peak"
    assert all(spell.recovered_at_ms is not None for spell in result.recoveries), "drawdown spells: both recovered"

    evidence = (
        session_evidence(WED, exit_price="0.5"),
        session_evidence(THU, cash="9950", exit_price="1", no_trade=True),
        session_evidence(FRI, cash="9950", exit_price="1.2"),
    )
    result = summarize_evidence(evidence, starting_capital_usd=D("10000"), annualization_sessions=251)
    assert result.ongoing_recovery, "recovery keeps an ongoing lower bound: the recovery is ongoing"
    assert result.max_recovery_sessions == 3, "recovery counts canonical sessions underwater: max recovery sessions"
    assert result.recoveries[-1].recovered_at_ms is None, "recovery keeps an ongoing lower bound: last spell not recovered"
    assert result.recoveries[-1].below_peak_sessions == 3, "recovery counts canonical sessions underwater: last spell sessions below peak"
    assert result.net_profit_usd == -30, "recovery keeps an ongoing lower bound: net profit"


def test_sharpe_daily_sample_includes_no_trade_and_streaks_count_completed_traded_sessions() -> None:
    evidence = (
        session_evidence(WED, exit_price="2"),
        session_evidence(THU, cash="10100", exit_price="1", no_trade=True),
        session_evidence(FRI, cash="10100", exit_price="0.5"),
    )
    result = summarize_evidence(evidence, starting_capital_usd=D("10000"), annualization_sessions=250)
    # Fixed returns .01, 0, -.005. mean=1/600, sample variance=7/120000.
    expected = math.sqrt(250) * (1 / 600) / math.sqrt(7 / 120000)
    assert result.sharpe == pytest.approx(expected, abs=1e-12, rel=0)
    assert result.daily_observation_count == 3
    assert result.completed_traded_sessions == 2
    assert result.monthly[0].completed_sessions == 2
    assert result.no_trade_sessions == 1
    assert (result.winning_sessions, result.losing_sessions, result.breakeven_sessions) == (1, 1, 0)
    assert result.average_winning_session_usd == D("100")
    assert result.average_losing_session_usd == D("-50")
    assert result.expectancy_usd == D("25")
    assert result.profit_factor == 2
    assert result.average_win_loss_ratio == 2
    assert result.profit_without_best_session_usd == -50


def test_constant_and_insufficient_returns_are_unavailable_and_zero_drawdown_is_measured() -> None:
    first = session_evidence(WED, no_trade=True)
    second = session_evidence(THU, no_trade=True)
    one = summarize_evidence((first,), starting_capital_usd=D("10000"), annualization_sessions=251)
    two = summarize_evidence((first, second), starting_capital_usd=D("10000"), annualization_sessions=251)
    assert one.sharpe is None and one.sharpe_unavailable_reason == "insufficient_daily_observations"
    assert two.sharpe is None and two.sharpe_unavailable_reason == "constant_daily_returns"
    assert two.max_drawdown_usd == two.max_drawdown_fraction == 0
    assert two.profit_factor is None
    assert two.expectancy_usd is None


def test_evidence_roundtrip_is_exact_for_a_skip_counted_session_and_for_a_traded_one() -> None:
    session = session_evidence(WED, no_trade=True)
    session = replace(session, outcome=SessionOutcome(SessionStatus.SKIPPED, SkipReason.WINDOW_DOES_NOT_FIT, (), (), D(0)))
    restored = evidence_from_json(evidence_to_json(session))
    assert restored == session, "skip-counted session: the evidence roundtrip is exact"
    assert restored.observations[0].cash_usd == D("10000"), "skip-counted session: the restored first observation keeps its cash"
    result = summarize_evidence((restored,), starting_capital_usd=D("10000"), annualization_sessions=251)
    assert result.daily_observation_count == result.skipped_sessions == 1, "skip-counted session is a daily zero observation"
    assert result.completed_traded_sessions == 0, "skip-counted session is not a traded session"
    assert not result.technical_failures, "skip-counted session: no technical failures"

    session = session_evidence(WED)
    payload = evidence_to_json(session)
    assert isinstance(payload["cash_before_usd"], str), "traded evidence roundtrip: capital is serialised as a string"
    assert isinstance(payload["outcome"]["trades"][0]["entry"]["filled"], str), "traded evidence roundtrip: fills are serialised as strings"
    restored = evidence_from_json(payload)
    assert restored == session, "traded evidence roundtrip preserves fills, fees and marks"
    assert restored.outcome.money_steps == session.outcome.money_steps, "traded evidence roundtrip preserves capital money steps"
    original = summarize_evidence((session,), starting_capital_usd=D("10000"), annualization_sessions=251)
    assert summarize_evidence((restored,), starting_capital_usd=D("10000"), annualization_sessions=251) == original, (
        "traded evidence roundtrip: the restored session summarises identically"
    )


def test_missing_trace_and_missing_marks_never_disappear_behind_profitable_final_equity() -> None:
    session = session_evidence(WED)
    point = session.observations[7]
    missing = replace(point, held=tuple(replace(mark, close=None) for mark in point.held))
    session = replace(session, observations=(*session.observations[:7], missing, *session.observations[8:]))
    result = summarize_evidence((session,), starting_capital_usd=D("10000"), annualization_sessions=251)
    assert result.net_profit_usd == 100
    assert result.missing_held_marks == 1
    assert any("missing_held_mark" in item for item in result.technical_failures)
    truncated = replace(session, observations=session.observations[:-1])
    result = summarize_evidence((truncated,), starting_capital_usd=D("10000"), annualization_sessions=251)
    assert any("incomplete_minute_trace" in item for item in result.technical_failures)


def test_fold_stitching_adds_prior_pnl_and_never_concatenates_reset_equity_levels() -> None:
    first = session_evidence(WED, exit_price="3")
    second = session_evidence(THU, exit_price="0.5")
    stitched = stitch_fold_evidence(((first,), (second,)), starting_capital_usd=D("10000"))
    assert stitched[1].cash_before_usd == D("10200")
    assert stitched[1].observations[-1].equity_usd == D("10150")
    metrics = summarize_evidence(stitched, starting_capital_usd=D("10000"), annualization_sessions=251)
    assert metrics.net_profit_usd == 150
    assert metrics.max_drawdown_usd == 50
    assert not metrics.technical_failures
    with pytest.raises(ValueError, match="missing fold"):
        stitch_fold_evidence(((first,), ()), starting_capital_usd=D("10000"))
    with pytest.raises(ValueError, match="chronological"):
        stitch_fold_evidence(((second,), (first,)), starting_capital_usd=D("10000"))


def test_calendar_factor_counts_whole_years_with_the_canonical_calendar() -> None:
    anchors = (open_ms(WED), session_windows_ms_utc(date(2027, 1, 4), date(2027, 1, 4))[0].open_ms_utc)
    expected = (len(session_windows_ms_utc(date(2026, 1, 1), date(2026, 12, 31))) + len(session_windows_ms_utc(date(2027, 1, 1), date(2027, 12, 31)))) / 2
    assert calendar_annualization(anchors) == pytest.approx(expected, abs=0, rel=0)


def test_paired_daily_differences_use_only_common_complete_dates_and_no_improvement_hurdle() -> None:
    candidate = summarize_evidence((session_evidence(WED, exit_price="2"),), starting_capital_usd=D("10000"), annualization_sessions=251)
    baseline = summarize_evidence((session_evidence(WED, exit_price="1.5"),), starting_capital_usd=D("10000"), annualization_sessions=251)
    compared = paired_daily_comparison(candidate, baseline)
    assert compared.paired_common_sessions == 1
    assert compared.paired_net_difference_usd == D("50")
    assert compared.candidate_beats_baseline is True
    assert paired_daily_comparison(candidate, candidate).candidate_beats_baseline is False
    assert paired_daily_comparison(baseline, candidate).candidate_beats_baseline is False
    extra = summarize_evidence((session_evidence(THU, exit_price="10"),), starting_capital_usd=D("10000"), annualization_sessions=251)
    assert paired_daily_comparison(candidate, extra).reason == "no_common_complete_sessions"
    partial = replace(candidate, daily=(*candidate.daily, *extra.daily))
    intersected = paired_daily_comparison(partial, baseline)
    assert intersected.paired_net_difference_usd == D("50")
    assert intersected.candidate_beats_baseline is None
    assert intersected.reason == "incomplete_paired_session_coverage"
    invalid = replace(candidate, technical_failures=("missing_held_mark",))
    assert paired_daily_comparison(invalid, baseline).candidate_beats_baseline is None
