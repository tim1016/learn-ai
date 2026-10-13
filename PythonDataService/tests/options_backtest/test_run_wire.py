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
import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options_backtest.capital_evidence import CapitalInvalidKind
from app.options_backtest.data_check import (
    NOT_YET_KNOWN,
    ContractNeeds,
    DataKind,
    ProblemCode,
    SessionCheck,
    SessionVerdict,
)
from app.options_backtest.market import PickRefusal
from app.options_backtest.results import ExitReason, SessionStatus, SkipReason
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.saved_runs import (
    FailureItem,
    Headline,
    PresentedStatus,
    RunFailure,
    RunFigures,
    RunRecord,
    StoredRun,
    StoredStatus,
)
from app.options_backtest.setup import FeePolicy
from app.options_backtest.statistics import Drawdown, Ratio
from app.research.persistence.schema import DDL_V20
from app.schemas import options_backtest_runs as wire
from tests.options_backtest._a_selling_run import a_selling_run
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
        # The three words that are literals where they are made, not enums: each is read from the type that makes it.
        (
            wire.OptionsBacktestFillsBannerResponse.model_fields["fee_policy_kind"].annotation,
            list(get_args(get_type_hints(FeePolicy)["kind"])),
        ),
        (
            wire.OptionsBacktestDrawdownResponse.model_fields["measured_at"].annotation,
            list(get_args(get_type_hints(Drawdown)["measured_at"])),
        ),
        (wire.RunStatusWord, list(get_args(PresentedStatus))),
        (wire.CapitalInvalidKindWord, [member.value for member in CapitalInvalidKind]),
    ],
)
def test_each_closed_set_on_the_wire_is_exactly_the_members_of_the_enum_it_comes_from(
    words: object, members: list[str]
) -> None:
    assert list(get_args(words)) == members


def test_the_status_a_run_is_presented_with_is_the_four_the_table_stores_and_interrupted() -> None:
    stored_in_the_table = re.search(r"status\s+TEXT NOT NULL CHECK \(status IN \(([^)]*)\)\)", DDL_V20[0])
    assert stored_in_the_table is not None
    in_the_table = [word.strip().strip("'") for word in stored_in_the_table.group(1).split(",")]

    assert in_the_table == list(get_args(StoredStatus))
    assert list(get_args(PresentedStatus)) == [*in_the_table, "interrupted"]


def test_the_contract_state_of_a_session_is_known_exactly_when_the_check_counted_them() -> None:
    # The two words name the two states of ``ContractNeeds.known``, which is a yes or a no and cannot gain a third.
    assert list(get_args(wire.OptionsBacktestContractNeedsResponse.model_fields["state"].annotation)) == [
        "known",
        "not_yet_known",
    ]

    def checked(needs: ContractNeeds) -> wire.OptionsBacktestSessionCheckResponse:
        return wire.OptionsBacktestSessionCheckResponse.from_session(
            SessionCheck(
                session_open_ms=1,
                session_close_ms=2,
                verdict=SessionVerdict.NEEDS_DOWNLOAD,
                skip=None,
                missing=(),
                contracts=needs,
                problems=(),
                notes=(),
            )
        )

    assert checked(ContractNeeds(needed=6, missing=4)).contracts.model_dump() == {
        "state": "known",
        "needed": 6,
        "missing": 4,
    }
    assert checked(ContractNeeds(needed=0, missing=0)).contracts.state == "known"
    assert checked(NOT_YET_KNOWN).contracts.model_dump() == {"state": "not_yet_known", "needed": None, "missing": None}


# ── One complete run ─────────────────────────────────────────────────────


def test_a_run_whose_setup_cannot_be_read_is_sent_with_its_figures_the_reason_and_no_banner(result: RunResult) -> None:
    stored = _stored(result)
    unreadable = StoredRun(
        record=stored.record,
        setup=None,
        figures=stored.figures,
        setup_cannot_open_reason="lots: Extra inputs are not permitted",
    )

    detail = wire.OptionsBacktestRunDetailResponse.from_stored(unreadable)

    assert detail.setup is None and detail.setup_cannot_open_reason == "lots: Extra inputs are not permitted"
    assert detail.banner is None
    assert detail.figures is not None and detail.figures.net_profit_usd == "-69.25"
    assert detail.fee_policy == "Fees are $0.65 per contract traded." and detail.headline is not None


def test_a_run_carries_its_setup_and_its_banner_together_or_the_reason_it_has_neither(result: RunResult) -> None:
    readable = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result)).model_dump()
    stored = _stored(result)
    unreadable = wire.OptionsBacktestRunDetailResponse.from_stored(
        StoredRun(stored.record, None, stored.figures, setup_cannot_open_reason="why")
    ).model_dump()
    wire.OptionsBacktestRunDetailResponse.model_validate(readable)
    wire.OptionsBacktestRunDetailResponse.model_validate(unreadable)

    for broken in (
        {**readable, "setup_cannot_open_reason": "why"},  # the setup and a reason why it is not there
        {**readable, "setup": None},  # neither the setup nor a reason
        {**readable, "banner": None},  # a setup without the banner that states what it says
        {**unreadable, "banner": readable["banner"]},  # a banner without a setup to state it from
        {**unreadable, "setup_cannot_open_reason": None},  # neither
    ):
        with pytest.raises(ValidationError):
            wire.OptionsBacktestRunDetailResponse.model_validate(broken)
    with pytest.raises(ValueError):
        StoredRun(stored.record, stored.setup, stored.figures, setup_cannot_open_reason="why")
    with pytest.raises(ValueError):
        StoredRun(stored.record, None, stored.figures)


def test_the_setup_a_run_carries_has_its_money_in_plain_digits_however_it_was_spelled(result: RunResult) -> None:
    # A setup spelled 1e5 holds Decimal("1E+5"), which a model would send as "1E+5". It is a hundred thousand dollars.
    stored = _stored(result)
    spelled_in_exponent_form = result.setup.model_copy(update={"starting_cash_usd": Decimal("1E+5")})
    assert str(spelled_in_exponent_form.starting_cash_usd) == "1E+5"

    detail = wire.OptionsBacktestRunDetailResponse.from_stored(
        StoredRun(stored.record, spelled_in_exponent_form, stored.figures)
    )

    assert detail.model_dump(mode="json")["setup"]["starting_cash_usd"] == "100000"
    assert detail.setup is not None and detail.setup.starting_cash_usd == Decimal("100000")


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
        ("closed_with_its_protection", 0),
        ("exit_time", 2),
        ("marked_not_sold", 1),
        ("marked_not_bought_back", 0),
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


def test_capital_evidence_the_cash_ladder_and_the_stamps_are_sent_as_exact_strings_apart_from_every_total() -> None:
    selling = a_selling_run()

    figures = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(selling)).figures

    assert figures is not None
    capital = figures.capital_evidence
    assert capital is not None
    # The spread of the selling run: Wednesday closes, Thursday ends with both legs marked.
    assert (capital.sessions_tested, capital.sessions_funded_continuously) == (2, 1)
    assert capital.ended_by == wire.OptionsBacktestCapitalInvalidEventResponse(
        kind="position_marked", session_open_ms=selling.sessions[1].session_open_ms
    )
    assert (capital.most_collateral_held_usd, capital.cash_needed_usd, capital.least_free_cash_usd) == (
        "500.00",
        "380.00",
        "9620.00",
    )
    assert Decimal(capital.largest_assignment_obligation_usd) == D("50000")
    assert (capital.cash_needed_is_incomplete, capital.closes_relied_on_own_relief) == (False, 0)
    ladder = figures.cash_ladder
    assert ladder is not None
    assert [(level.multiple, Decimal(level.starting_cash_usd), level.is_setups_own) for level in ladder] == [
        ("0.5", D("5000"), False),
        ("1", D("10000"), True),
        ("2", D("20000"), False),
        ("4", D("40000"), False),
    ]
    assert ladder[1].capital_evidence == capital
    assert figures.stamps == wire.OptionsBacktestRunStampsResponse(
        replay_rules_version=2,
        requirement_rule="alpaca_covered_positions",
        requirement_rule_read_at_ms=1_791_777_600_000,
    )
    assert figures.net_profit_usd == "10.00"


def test_a_run_stored_before_capital_evidence_is_sent_with_each_of_the_three_as_not_recorded_and_its_figures_as_they_were(
    result: RunResult,
) -> None:
    recorded = _stored(result)
    old = StoredRun(
        record=recorded.record,
        setup=recorded.setup,
        figures=RunFigures(
            replace(result.summary, capital_evidence=None, cash_ladder=None, stamps=None),
            result.slippage,
            result.slippage_tolerance,
        ),
    )

    now = wire.OptionsBacktestRunDetailResponse.from_stored(recorded).figures
    then = wire.OptionsBacktestRunDetailResponse.from_stored(old).figures

    assert now is not None and then is not None
    assert (then.capital_evidence, then.cash_ladder, then.stamps) == (None, None, None)
    assert now.capital_evidence is not None and now.cash_ladder is not None and now.stamps is not None
    assert then.model_dump(exclude={"capital_evidence", "cash_ladder", "stamps"}) == now.model_dump(
        exclude={"capital_evidence", "cash_ladder", "stamps"}
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


def test_a_run_is_sent_with_no_date_in_it_its_sessions_are_their_opens(result: RunResult) -> None:
    sent = wire.OptionsBacktestRunDetailResponse.from_stored(_stored(result)).model_dump_json()

    assert not re.search(r"\d{4}-\d{2}-\d{2}", sent)
    assert "trading_date" not in sent and "peak_date" not in sent and "trough_date" not in sent
