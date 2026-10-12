"""What the page is sent of a run: exact strings, rates in three parts, and words tied to the enums they come from (#3030).

The run is the hand-worked one of ``test_service.py`` (four sessions of one same-day call, net -69.25), replayed here
over dictionaries instead of a lake. Hand-worked figures:

* The profit factor is gross profit over gross loss, 36.70 / 105.95 = 3670 / 10595 = 734 / 2119 = 0.3463898..., which
  is 0.346390 to six places (the seventh digit, 8, rounds the sixth up).
* A third is 0.333333 (the daily win rate: one profitable session of three that traded; the trade win rate: one won
  trade of three).
* The largest fall is 69.25 of a peak of 10,000.00 (the starting cash): 69.25 / 10,000 = 0.006925 exactly.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import get_args

import pytest

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.data_check import DataKind, ProblemCode, SessionVerdict
from app.options_backtest.market import PickRefusal
from app.options_backtest.results import ExitReason, SessionStatus, SkipReason
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.saved_runs import FailureItem, Headline, RunFailure, RunFigures, RunRecord, StoredRun
from app.options_backtest.statistics import Ratio
from app.schemas import options_backtest_runs as wire
from tests.options_backtest._run_data import fake_data
from tests.options_backtest.test_service import SESSION_OF_THE_RANGE, THU, TUE, the_scene, the_setup

D = Decimal


@pytest.fixture(scope="module")
def result() -> RunResult:
    return run_sessions(the_setup(), SESSION_OF_THE_RANGE, fake_data(the_scene()))


def _stored(result: RunResult, *, status: str = "complete") -> StoredRun:
    summary = result.summary
    record = RunRecord(
        id=12,
        created_at_ms=1_791_800_000_000,
        finished_at_ms=1_791_800_100_000,
        job_id="job-12",
        status=status,  # type: ignore[arg-type]
        setup_name=None,
        underlying="SPY",
        first_session_open_ms=session_window_for_date(TUE).open_ms_utc,
        last_session_open_ms=session_window_for_date(date(2026, 10, 9)).open_ms_utc,
        sessions_requested=4,
        fee_policy_text="Fees are $0.65 per contract traded.",
        calendar_identity="NYSE",
        calendar_package_version="5.1.0",
        failure=None,
        headline=Headline(
            sessions_tested=summary.sessions_tested,
            sessions_traded=summary.sessions_traded,
            trades=summary.trades,
            trades_won=summary.trades_won,
            net_profit_usd=summary.net_profit_usd,
            ending_cash_usd=summary.ending_cash_usd,
        ),
    )
    return StoredRun(
        record=record,
        setup=result.setup,
        figures=RunFigures(summary, result.slippage, result.slippage_tolerance),
    )


# ── The words are the enums' ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("words", "members"),
    [
        (wire.SessionStatusWord, [member.value for member in SessionStatus]),
        (wire.SkipReasonWord, [member.value for member in SkipReason]),
        (wire.ExitReasonWord, [member.value for member in ExitReason]),
        (wire.PickRefusalWord, [member.value for member in PickRefusal]),
        (wire.VerdictWord, [member.value for member in SessionVerdict]),
        (wire.ProblemCodeWord, [member.value for member in ProblemCode]),
        (wire.MissingWord, [member.value for member in DataKind]),
        (wire.RunStatusWord, ["running", "complete", "failed", "cancelled", "interrupted"]),
    ],
)
def test_each_closed_set_on_the_wire_is_exactly_the_members_of_the_enum_it_comes_from(
    words: object, members: list[str]
) -> None:
    assert list(get_args(words)) == members


# ── One complete run ─────────────────────────────────────────────────────


def test_the_figures_are_exact_strings_and_rates_come_in_three_parts_with_one_rounded(result: RunResult) -> None:
    detail = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result))

    figures = detail.figures
    assert figures is not None
    assert (figures.starting_cash_usd, figures.net_profit_usd, figures.ending_cash_usd) == (
        "10000",
        "-69.25",
        "9930.75",
    )
    assert (figures.gross_profit_usd, figures.gross_loss_usd) == ("36.70", "105.95")
    assert (figures.total_fees_usd, figures.slippage_paid_usd) == ("3.25", "6.00")
    assert figures.profit_factor is not None
    assert (
        figures.profit_factor.numerator_usd,
        figures.profit_factor.denominator_usd,
        figures.profit_factor.rounded_to_6_places,
    ) == ("36.70", "105.95", "0.346390")
    for rate in (figures.daily_win_rate, figures.trade_win_rate):
        assert rate is not None
        assert (rate.numerator, rate.denominator, rate.rounded_to_6_places) == ("1", "3", "0.333333")
    drawdown = figures.drawdown_by_fraction
    assert drawdown is not None
    assert (drawdown.peak_equity_usd, drawdown.trough_equity_usd, drawdown.fall_usd) == ("10000", "9930.75", "69.25")
    assert drawdown.fall_ratio.rounded_to_6_places == "0.006925"
    assert drawdown.peak_session_open_ms is None, "the peak is the starting cash, before any session"
    assert drawdown.trough_session_open_ms == session_window_for_date(THU).open_ms_utc


def test_the_counts_and_the_by_reason_lists_list_every_member_in_the_enums_order(result: RunResult) -> None:
    figures = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result)).figures
    assert figures is not None

    assert [(each.reason, each.count) for each in figures.exits_by_reason] == [
        ("stop", 0),
        ("square_off", 0),
        ("exit_time", 2),
        ("marked_not_sold", 1),
    ]
    assert [(each.reason, each.count) for each in figures.skipped_by_reason] == [
        ("window_does_not_fit", 0),
        ("underlying_did_not_trade", 0),
    ]
    assert [each.reason for each in figures.picks_refused_by_reason] == [member.value for member in PickRefusal]
    assert (figures.entries_dropped, figures.sessions_incomplete, figures.sessions_no_trade) == (1, 1, 1)


def test_the_day_and_month_tables_have_a_row_for_each_session_and_the_month(result: RunResult) -> None:
    figures = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result)).figures
    assert figures is not None

    assert [(row.session_open_ms, row.status, row.net_profit_usd) for row in figures.by_day] == [
        (session_window_for_date(day).open_ms_utc, status, profit)
        for day, status, profit in (
            (TUE, "incomplete", "-52.65"),
            (date(2026, 10, 7), "traded", "36.70"),
            (THU, "traded", "-53.30"),
            (date(2026, 10, 9), "no_trade", "0"),
        )
    ]
    [october] = figures.by_month
    assert (october.year, october.month, october.sessions, october.sessions_traded, october.trades) == (
        2026,
        10,
        4,
        3,
        3,
    )
    assert october.net_profit_usd == "-69.25"
    assert [(each.status, each.count) for each in october.sessions_by_status] == [
        ("traded", 2),
        ("no_trade", 1),
        ("skipped", 0),
        ("incomplete", 1),
    ]


def test_the_ladder_goes_lowest_first_and_the_tolerance_says_a_loss_at_no_slippage_survives_none(
    result: RunResult,
) -> None:
    detail = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result))

    assert detail.slippage[0].slippage_percent == "0" and detail.slippage[0].net_profit_usd == "-63.25"
    assert [point.slippage_percent for point in detail.slippage] == sorted(
        (point.slippage_percent for point in detail.slippage), key=float
    )
    assert [point.slippage_percent for point in detail.slippage if point.is_setups_own] == ["0.3"]
    tolerance = detail.slippage_tolerance
    assert tolerance is not None
    assert (tolerance.highest_profitable_percent, tolerance.first_unprofitable_percent, tolerance.nothing_traded) == (
        None,
        "0",
        False,
    )


def test_the_banner_states_the_facts_the_result_must_not_hide(result: RunResult) -> None:
    banner = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result)).banner

    assert banner.fills_are_simulated_from_trade_prices is True
    assert banner.bid_and_ask_are_available is False
    assert (banner.slippage_percent, banner.price_step_usd) == ("0.3", "0.01")
    assert (banner.fee_policy_kind, banner.fee_per_contract_usd) == ("per_contract", "0.65")
    assert banner.fee_policy == "Fees are $0.65 per contract traded."


def test_the_run_carries_its_setup_whole_and_no_trades(result: RunResult) -> None:
    detail = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result))

    assert detail.setup == result.setup
    assert "trades" not in detail.model_dump(), "another route serves a run's trades"


def test_everything_the_page_is_sent_is_an_integer_a_string_a_bool_or_a_list_and_no_float(result: RunResult) -> None:
    sent = json.loads(wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result)).model_dump_json())

    def walk(value: object, path: str) -> None:
        assert not isinstance(value, float), f"{path} is a float"
        if isinstance(value, dict):
            for key, each in value.items():
                walk(each, f"{path}.{key}")
        elif isinstance(value, list):
            for at, each in enumerate(value):
                walk(each, f"{path}[{at}]")

    walk(sent, "run")


# ── A run that is not complete ───────────────────────────────────────────


def test_a_failed_run_has_its_code_reason_and_items_and_no_figures(result: RunResult) -> None:
    stored = _stored(result, status="failed")
    stored = StoredRun(
        record=RunRecord(
            **{
                **stored.record.__dict__,
                "headline": None,
                "failure": RunFailure(
                    "DOWNLOAD_FAILED",
                    "The data could not be fetched.",
                    (FailureItem(12345, "provider_api_error", "x"),),
                ),
            }
        ),
        setup=stored.setup,
        figures=None,
    )

    detail = wire.OptionsBacktestRunDetailResponse.from_stored(stored)

    assert detail.status == "failed" and detail.headline is None
    assert detail.figures is None and detail.slippage == [] and detail.slippage_tolerance is None
    assert detail.failure is not None and detail.failure.code == "DOWNLOAD_FAILED"
    assert [(item.session_open_ms, item.code) for item in detail.failure.items] == [(12345, "provider_api_error")]


def test_the_history_row_has_the_headline_figures_as_strings(result: RunResult) -> None:
    item = wire.OptionsBacktestRunListItemResponse.from_record(_stored(result).record)

    assert item.headline is not None
    assert (item.headline.trades, item.headline.trades_won) == (3, 1)
    assert (item.headline.net_profit_usd, item.headline.ending_cash_usd) == ("-69.25", "9930.75")
    assert item.fee_policy == "Fees are $0.65 per contract traded."
    assert item.status == "complete" and item.id == 12


def test_a_decimal_is_written_in_plain_digits_never_in_exponent_form() -> None:
    from decimal import Decimal

    assert wire.exact(Decimal("1E+2")) == "100" and wire.exact(Decimal("0E-8")) == "0.00000000"
    assert wire.exact(Decimal("0.0000001")) == "0.0000001"


def test_a_rate_whose_quotient_ends_is_still_given_to_six_places_in_both_kinds_of_rate() -> None:
    # One half is exact, and the rounded figure is still written to six places: 0.500000.
    half = Ratio(D("1"), D("2"))

    assert wire.OptionsBacktestRateResponse.from_ratio(half).rounded_to_6_places == "0.500000"
    assert (
        wire.OptionsBacktestDollarRateResponse.from_ratio(Ratio(D("1.00"), D("2.00"))).rounded_to_6_places == "0.500000"
    )
