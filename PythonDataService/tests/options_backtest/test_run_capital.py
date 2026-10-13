"""What a run says of its cash: capital evidence over many sessions, and the cash ladder (#3108).

Minutes are counted after the open and a setup enters at minute 5 and leaves at minute 60, so an order fills at the open
of minute 6 and a close at the open of minute 61. The spread is a credit put spread 5 wide, one contract: the 495 put
bought and the 500 put sold, so an opening group brings in 120.00 at 1.00 and 2.20 and pledges 500.00. There is no fee
and no slippage unless a scene says so. Every expectation is worked by hand and written as a literal.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.options_backtest import run as run_module
from app.options_backtest.capital_evidence import (
    CapitalEvidence,
    CapitalInvalidEvent,
    CapitalInvalidKind,
    CapitalSession,
)
from app.options_backtest.cash_ladder import DEFAULT_CASH_LADDER
from app.options_backtest.results import SessionStatus
from app.options_backtest.run import run_sessions
from app.options_backtest.run_stamps import RunStamps, current_stamps
from app.options_backtest.setup import MS_PER_MINUTE, FeePolicy, OptionsBacktestSetup
from app.options_backtest.statistics import summarize_run
from app.utils.session_anchors import et_midnight_ms
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._a_selling_run import a_selling_run, a_spread_that_closes, the_selling_setup
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import (
    FRI,
    HALF_DAY,
    OPEN_MS,
    THU,
    WED,
    Day,
    changed,
    close_ms,
    fake_data,
    flat,
    steady,
)

D = Decimal
CALL_500 = ("call", 500)
PUT_495, PUT_500 = ("put", 495), ("put", 500)


def _spread_setup(cash: str) -> OptionsBacktestSetup:
    return changed(the_selling_setup(), starting_cash_usd=cash)


def _a_losing_spread() -> Day:
    """Bought at 1.00 and sold at 0.60; sold at 2.20 and bought back at 3.00: -40.00 and -80.00, -120.00 in all."""
    return Day(
        underlying=steady("500.00"),
        options={
            PUT_495: {**flat("1.00", 5, 60), 61: ("0.60", "0.60")},
            PUT_500: {**flat("2.20", 5, 60), 61: ("3.00", "3.00")},
        },
    )


def _a_sold_leg_that_ran_away() -> Day:
    """The sold 500 put is 8.00 at the exit time, and the 495 put, which last traded at minute 59 at 6.00, has no bar
    then: the buy-back fills alone, with the cover held, and the cover is marked at 6.00."""
    return Day(
        underlying=steady("500.00"),
        options={
            PUT_495: {**flat("1.00", 5, 58), 59: ("1.00", "6.00")},
            PUT_500: {**flat("2.20", 5, 60), 61: ("8.00", "8.00")},
        },
    )


def _a_buy_back_with_the_cover_missing_for_a_minute() -> Day:
    """The cover has no bar at minute 61 and is sold at minute 62; the sold put is bought back at 61 for 2.20."""
    return Day(
        underlying=steady("500.00"),
        options={PUT_495: {**flat("1.00", 5, 60), **flat("1.00", 62, 70)}, PUT_500: flat("2.20", 5, 70)},
    )


# ── Capital evidence of a run ────────────────────────────────────────────


def test_a_run_whose_every_close_is_funded_is_funded_through_all_its_sessions_with_no_event() -> None:
    # Wednesday: 10,000.00 -> 10,120.00 -> 10,030.00. Thursday: 10,030.00 -> 10,150.00 -> 10,060.00.
    data = fake_data({WED: a_spread_that_closes(), THU: a_spread_that_closes()})

    result = run_sessions(the_selling_setup(), [WED, THU], data)

    assert result.summary.capital_evidence == CapitalEvidence(
        sessions_tested=2,
        sessions_funded_continuously=2,
        ended_by=None,
        most_collateral_held_usd=D("500.00"),
        # Wednesday's opening: 500.00 - 120.00 = 380.00. Thursday's: 500.00 - 150.00 = 350.00.
        cash_needed_usd=D("380.00"),
        cash_needed_is_incomplete=False,
        least_free_cash_usd=D("9620.00"),
        largest_assignment_obligation_usd=D("50000"),
        closes_relied_on_own_relief=0,
    )


def test_a_session_ending_with_a_position_marked_ends_the_evidence_at_that_session_and_its_money_is_hand_worked() -> (
    None
):
    # The spread of the selling run: Wednesday closes (+30.00). Thursday opens it (10,030.00 -> 10,150.00) and the sold
    # put stops trading before it can be bought back, so both legs are marked and the session is incomplete.
    result = a_selling_run()

    assert [session.status for session in result.sessions] == [SessionStatus.TRADED, SessionStatus.INCOMPLETE]
    assert result.summary.capital_evidence == CapitalEvidence(
        sessions_tested=2,
        sessions_funded_continuously=1,
        ended_by=CapitalInvalidEvent(CapitalInvalidKind.POSITION_MARKED, OPEN_MS[THU], None, None),
        most_collateral_held_usd=D("500.00"),
        # Wednesday's opening is the most: 500.00 - 120.00. Thursday's opening came before the mark: 500.00 - 150.00.
        cash_needed_usd=D("380.00"),
        cash_needed_is_incomplete=False,
        # 10,120.00 - 500.00 after Wednesday's opening is the least free cash either session saw.
        least_free_cash_usd=D("9620.00"),
        largest_assignment_obligation_usd=D("50000"),
        closes_relied_on_own_relief=0,
    )


def test_an_unfunded_close_names_its_session_and_minute_ends_the_evidence_and_leaves_every_later_session_in_the_totals() -> (
    None
):
    # 500.00 of cash. Thursday: the spread opens (500.00 -> 620.00, 500.00 pledged, 120.00 free). At the exit time the
    # sold put is 8.00 and the cover has no bar, so the buy-back fills alone: 620.00 - 800.00 = -180.00, and the bought
    # put alone pledges nothing. The cover is then marked at 6.00. Friday opens the spread with the 420.00 the session
    # carried (500.00 - 680.00 + 600.00), so it trades and is in the totals though the evidence ended on Thursday.
    data = fake_data({THU: _a_sold_leg_that_ran_away(), FRI: a_spread_that_closes()})

    result = run_sessions(_spread_setup("500"), [THU, FRI], data)

    assert result.sessions[1].cash_before_usd == D("420.00")
    assert [session.pnl_usd for session in result.sessions] == [D("-80.00"), D("30.00")]
    assert result.summary.capital_evidence == CapitalEvidence(
        sessions_tested=2,
        sessions_funded_continuously=0,
        ended_by=CapitalInvalidEvent(CapitalInvalidKind.UNFUNDED_CLOSE, OPEN_MS[THU], close_ms(THU, 61), D("-180.00")),
        most_collateral_held_usd=D("500.00"),
        # Thursday's opening only: 500.00 - 120.00. The unfunded close is not in the stretch.
        cash_needed_usd=D("380.00"),
        cash_needed_is_incomplete=False,
        least_free_cash_usd=D("-180.00"),
        largest_assignment_obligation_usd=D("50000"),
        # The buy-back cost 800.00 against 120.00 free, with its cover held. Friday's close sold its cover with it.
        closes_relied_on_own_relief=1,
    )
    # The totals keep every session, as before: Friday's +30.00 is in them.
    assert result.summary.net_profit_usd == D("-50.00")
    assert result.summary.ending_cash_usd == D("450.00")
    assert result.summary.trades == 4


@pytest.mark.parametrize(
    ("cash", "relied", "least_free"),
    [
        # 500.00: the opening leaves 120.00 free, the buy-back alone costs 220.00 (more), and free cash after it is
        # 400.00: relief, funded.
        ("500", 1, D("120.00")),
        # 10,000.00: 9,620.00 free before the buy-back, which cost 220.00: no relief.
        ("10000", 0, D("9620.00")),
    ],
)
def test_a_buy_back_alone_that_cost_more_than_the_free_cash_before_it_is_counted_and_does_not_end_the_evidence(
    cash: str, relied: int, least_free: Decimal
) -> None:
    data = fake_data({WED: _a_buy_back_with_the_cover_missing_for_a_minute()})

    result = run_sessions(_spread_setup(cash), [WED], data)

    evidence = result.summary.capital_evidence
    assert evidence is not None
    assert evidence.closes_relied_on_own_relief == relied
    assert evidence.ended_by is None
    assert (evidence.sessions_tested, evidence.sessions_funded_continuously) == (1, 1)
    assert evidence.least_free_cash_usd == least_free
    assert result.sessions[0].status is SessionStatus.TRADED


def test_a_bought_options_run_shows_no_collateral_and_a_cash_need_equal_to_what_it_paid_fees_included() -> None:
    # Bought at 2.00 for 200.00 and a 0.65 fee: 10,000.00 -> 9,799.35. Sold at 3.00, less 0.65: +299.35.
    paying = changed(setup(leg()), fees=FeePolicy(kind="per_contract", per_contract_usd=D("0.65")))
    day = Day(underlying=steady("500.00"), options={CALL_500: {**flat("2.00", 5, 60), 61: ("3.00", "3.00")}})

    result = run_sessions(paying, [WED], fake_data({WED: day}))

    assert result.summary.capital_evidence == CapitalEvidence(
        sessions_tested=1,
        sessions_funded_continuously=1,
        ended_by=None,
        most_collateral_held_usd=D("0"),
        cash_needed_usd=D("200.65"),
        cash_needed_is_incomplete=False,
        least_free_cash_usd=D("9799.35"),
        largest_assignment_obligation_usd=D("0"),
        closes_relied_on_own_relief=0,
    )
    # Its figures do not change by one cent: (3.00 - 2.00) x 100 less 0.65 twice.
    assert (result.summary.net_profit_usd, result.summary.ending_cash_usd) == (D("98.70"), D("10098.70"))


def test_the_totals_are_what_summarize_run_gives_and_capital_evidence_changes_none_of_them() -> None:
    for result in (a_selling_run(), a_busy_run()):
        unrecorded = replace(result.summary, capital_evidence=None, cash_ladder=None, stamps=None)

        assert unrecorded == summarize_run(result.sessions, starting_cash_usd=result.setup.starting_cash_usd)
        assert result.summary.capital_evidence is not None


def test_a_run_that_made_no_trade_is_funded_through_every_session_and_saw_its_starting_cash() -> None:
    skipped = Day(underlying=steady("500.00", 6, 390))  # no bar at the entry time: the session is skipped

    result = run_sessions(the_selling_setup(), [WED], fake_data({WED: skipped}))

    assert result.sessions[0].status is SessionStatus.SKIPPED
    assert result.summary.capital_evidence == CapitalEvidence(
        sessions_tested=1,
        sessions_funded_continuously=1,
        ended_by=None,
        most_collateral_held_usd=D("0"),
        cash_needed_usd=D("0"),
        cash_needed_is_incomplete=False,
        least_free_cash_usd=D("10000"),
        largest_assignment_obligation_usd=D("0"),
        closes_relied_on_own_relief=0,
    )


# ── An entry refused for cash, and the cash ladder ───────────────────────

# 499.00 of cash and a spread that loses 120.00 a day. Wednesday: 499.00 -> 619.00 (free 119.00) -> 619.00 - 300.00
# + 60.00 = 379.00. Thursday opens with 379.00: 379.00 + 120.00 = 499.00 is under the 500.00 pledged, so both legs
# are refused for cash and the session trades nothing.


def _the_losing_run(cash: str = "499", **keywords: object):
    data = fake_data({WED: _a_losing_spread(), THU: _a_losing_spread()})
    return run_sessions(_spread_setup(cash), [WED, THU], data, **keywords)  # type: ignore[arg-type]


def test_the_cash_this_path_needed_is_worked_by_hand_and_is_marked_incomplete_when_an_entry_was_refused_for_cash() -> (
    None
):
    result = _the_losing_run()

    evidence = result.summary.capital_evidence
    assert evidence is not None
    # Wednesday's opening: 500.00 - 120.00 = 380.00. Its close: 0 - (379.00 - 499.00) = 120.00. Thursday traded nothing.
    assert evidence.cash_needed_usd == D("380.00")
    assert evidence.cash_needed_is_incomplete is True
    assert (evidence.sessions_funded_continuously, evidence.ended_by) == (2, None)
    assert result.summary.entries_refused_for_cash == 2


def test_the_ladder_shows_a_level_too_small_to_enter_refused_and_a_larger_one_filled() -> None:
    # 250.00 of cash on Wednesday's spread, which makes +30.00 when it trades. The levels are 125.00, 250.00 (the
    # setup's own), 500.00 and 1,000.00. 125.00 + 120.00 and 250.00 + 120.00 are under the 500.00 pledged: both legs
    # refused. 500.00 + 120.00 is over it: both fill, and the spread is closed at +30.00.
    data = fake_data({WED: a_spread_that_closes()})

    result = run_sessions(_spread_setup("250"), [WED], data)

    ladder = result.summary.cash_ladder
    assert ladder is not None
    assert [
        (level.starting_cash_usd, level.is_setups_own, level.entries_filled, level.entries_refused_for_cash)
        for level in ladder
    ] == [
        (D("125"), False, 0, 2),
        (D("250"), True, 0, 2),
        (D("500"), False, 2, 0),
        (D("1000"), False, 2, 0),
    ]
    assert [level.net_profit_usd for level in ladder] == [D("0"), D("0"), D("30.00"), D("30.00")]
    assert [level.multiple for level in ladder] == [D("0.5"), D("1"), D("2"), D("4")]


def test_each_level_carries_its_own_cash_from_one_session_to_the_next_and_has_its_own_capital_evidence() -> None:
    result = _the_losing_run()

    ladder = result.summary.cash_ladder
    assert ladder is not None
    [half, own, double, four_times] = ladder
    # 249.50: 369.50 under the 500.00 pledged, so both legs are refused on both days: nothing is ever held.
    assert (half.starting_cash_usd, half.entries_filled, half.entries_refused_for_cash) == (D("249.5"), 0, 4)
    assert half.capital_evidence.cash_needed_usd == D("0")
    assert half.capital_evidence.cash_needed_is_incomplete is True
    assert half.capital_evidence.least_free_cash_usd == D("249.5")
    # 499.00 is the run: Wednesday trades (-120.00) and Thursday is refused.
    assert (own.entries_filled, own.entries_refused_for_cash, own.net_profit_usd) == (2, 2, D("-120.00"))
    assert own.capital_evidence == result.summary.capital_evidence
    assert own.capital_evidence.least_free_cash_usd == D("119.00")
    # 998.00: Wednesday 998.00 -> 1,118.00 -> 878.00; Thursday 878.00 -> 998.00 -> 758.00. Both days trade.
    assert (double.entries_filled, double.entries_refused_for_cash, double.net_profit_usd) == (4, 0, D("-240.00"))
    # Thursday's opening leaves the cash where it began (998.00): 500.00 - 0 = 500.00, more than Wednesday's 380.00.
    # The setup's own run could not show it: its Thursday was refused, and its figure is marked incomplete for that.
    assert double.capital_evidence.cash_needed_usd == D("500.00")
    assert double.capital_evidence.cash_needed_is_incomplete is False
    assert double.capital_evidence.least_free_cash_usd == D("498.00")
    # 1,996.00 trades the same path as 998.00, and needs the same.
    assert (four_times.entries_filled, four_times.net_profit_usd) == (4, D("-240.00"))
    assert four_times.capital_evidence.cash_needed_usd == D("500.00")
    assert four_times.capital_evidence.least_free_cash_usd == D("1496.00")
    for level in ladder:
        assert (level.capital_evidence.sessions_tested, level.capital_evidence.ended_by) == (2, None)


def test_the_level_at_the_setups_own_cash_is_the_run_and_is_not_replayed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    cash_of_each_replay: list[Decimal] = []
    real = run_module.replay_session

    def counting(*args: object, **kwargs: object):
        cash_of_each_replay.append(kwargs["cash_usd"])  # type: ignore[arg-type]
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(run_module, "replay_session", counting)
    data = fake_data({WED: a_spread_that_closes(), THU: a_spread_that_closes()})

    result = run_sessions(the_selling_setup(), [WED, THU], data, slippage_ladder=(D("0"),))

    # One replay at the setup's own cash and one at each of the other three levels, for each of the two sessions, and
    # each at its own carried cash: every level made +30.00 on Wednesday.
    assert len(cash_of_each_replay) == 8
    assert sorted(cash_of_each_replay[:4]) == [D("5000"), D("10000"), D("20000"), D("40000")]
    assert sorted(cash_of_each_replay[4:]) == [D("5030.00"), D("10030.00"), D("20030.00"), D("40030.00")]
    own = next(level for level in result.summary.cash_ladder or () if level.is_setups_own)
    assert (own.net_profit_usd, own.entries_filled) == (result.summary.net_profit_usd, result.summary.trades)
    assert own.entries_refused_for_cash == result.summary.entries_refused_for_cash
    assert own.starting_cash_usd == result.setup.starting_cash_usd


def test_the_ladder_replays_read_each_days_data_once() -> None:
    data = fake_data({WED: a_spread_that_closes(), THU: a_spread_that_closes()})

    run_sessions(the_selling_setup(), [WED, THU], data)

    # Underlying, listing and two puts, for two days: the levels share the loaded days.
    assert len(data.asked) == len(set(data.asked)) == 8


def test_a_session_the_window_does_not_fit_reads_nothing_and_is_skipped_at_every_level() -> None:
    # A half day closes at 13:00, 210 minutes after the open, and the exit time is 250 minutes after it.
    too_late = changed(the_selling_setup(), exit={"anchor": "open", "offset_ms": 250 * MS_PER_MINUTE})
    data = fake_data({})

    result = run_sessions(too_late, [HALF_DAY], data)

    assert result.sessions[0].status is SessionStatus.SKIPPED
    assert data.asked == []
    ladder = result.summary.cash_ladder
    assert ladder is not None
    assert [level.entries_filled for level in ladder] == [0, 0, 0, 0]
    assert result.summary.capital_evidence is not None
    assert result.summary.capital_evidence.sessions_funded_continuously == 1


def test_a_session_whose_underlying_did_not_trade_at_the_entry_time_is_skipped_at_every_level() -> None:
    skipped = Day(underlying=steady("500.00", 6, 390))

    result = run_sessions(the_selling_setup(), [WED], fake_data({WED: skipped}))

    ladder = result.summary.cash_ladder
    assert ladder is not None
    assert [(level.entries_filled, level.entries_refused_for_cash, level.net_profit_usd) for level in ladder] == [
        (0, 0, D("0"))
    ] * 4
    assert [level.capital_evidence.least_free_cash_usd for level in ladder] == [
        D("5000"),
        D("10000"),
        D("20000"),
        D("40000"),
    ]


def test_the_default_ladder_is_half_the_same_double_and_four_times_and_the_levels_are_the_callers_to_set() -> None:
    assert (D("0.5"), D("1"), D("2"), D("4")) == DEFAULT_CASH_LADDER
    data = fake_data({WED: a_spread_that_closes()})

    only_three_times = run_sessions(the_selling_setup(), [WED], data, cash_ladder=(D("3"),))
    spelled_twice = run_sessions(the_selling_setup(), [WED], data, cash_ladder=(D("1.0"), D("1"), D("2")))

    assert only_three_times.summary.cash_ladder is not None
    assert [level.starting_cash_usd for level in only_three_times.summary.cash_ladder] == [D("10000"), D("30000")]
    assert [level.is_setups_own for level in only_three_times.summary.cash_ladder] == [True, False]
    assert spelled_twice.summary.cash_ladder is not None
    assert [level.starting_cash_usd for level in spelled_twice.summary.cash_ladder] == [D("10000"), D("20000")]


@pytest.mark.parametrize("bad", [D("0"), D("-1"), D("NaN"), D("Infinity")])
def test_a_level_that_is_not_a_multiple_above_zero_is_refused_before_anything_is_read(bad: Decimal) -> None:
    data = fake_data({WED: a_spread_that_closes()})

    with pytest.raises(ValueError, match="multiple of the starting cash above zero"):
        run_sessions(the_selling_setup(), [WED], data, cash_ladder=(D("2"), bad))

    assert data.asked == []


# ── Stamps ───────────────────────────────────────────────────────────────


def test_a_new_run_is_stamped_with_the_version_of_the_rules_and_the_name_and_date_of_the_requirement_rule() -> None:
    result = a_selling_run()

    assert result.summary.stamps == RunStamps(
        replay_rules_version=1,
        requirement_rule="alpaca_covered_positions",
        # New York midnight of 2026-10-12, when Alpaca's page was read: EDT, so 04:00 UTC, 20,738 days after the epoch.
        requirement_rule_read_at_ms=1_791_777_600_000,
    )
    assert result.summary.stamps == current_stamps()
    assert current_stamps().requirement_rule_read_at_ms == et_midnight_ms(date(2026, 10, 12))


# ── What is stored ───────────────────────────────────────────────────────


def test_capital_evidence_the_ladder_and_the_stamps_ride_in_the_stored_summary_and_come_back_equal() -> None:
    from app.options_backtest import run_json

    summary = _the_losing_run().summary

    stored = json.loads(json.dumps(run_json.summary_to_json(summary)))

    assert {"capital_evidence", "cash_ladder", "stamps"} <= stored.keys()
    assert run_json.summary_from_json(stored) == summary


def test_a_run_stored_before_this_change_reads_as_not_recorded_and_its_figures_are_unchanged() -> None:
    from app.options_backtest import run_json

    summary = _the_losing_run().summary
    old = json.loads(json.dumps(run_json.summary_to_json(summary)))
    for key in ("capital_evidence", "cash_ladder", "stamps"):
        del old[key]

    read = run_json.summary_from_json(old)

    assert (read.capital_evidence, read.cash_ladder, read.stamps) == (None, None, None)
    # Every figure the run had is what it was.
    assert read == replace(summary, capital_evidence=None, cash_ladder=None, stamps=None)
    assert read.net_profit_usd == D("-120.00") and read.entries_refused_for_cash == 2
    # And a summary with none of the three writes none of them: its JSON is the JSON it always was.
    assert run_json.summary_to_json(read) == old


# ── A session read back from storage is never asked ──────────────────────


def test_a_session_read_back_with_trades_and_no_money_steps_is_refused_and_not_read_as_funded_through() -> None:
    # A saved run keeps no money steps (#3106), so a traded session read back has none, as one that filled nothing has.
    # Asked, it would answer "funded through every session, needing nothing": it is refused instead.
    replayed = a_selling_run().sessions[0].outcome
    assert replayed.trades and replayed.money_steps
    read_back = replace(replayed, money_steps=())

    assert CapitalSession.of_outcome(OPEN_MS[WED], replayed).money_steps == replayed.money_steps
    with pytest.raises(ValueError, match="read back from storage"):
        CapitalSession.of_outcome(OPEN_MS[WED], read_back)


def test_a_session_that_made_no_trade_has_no_money_steps_and_is_not_refused() -> None:
    skipped = run_sessions(the_selling_setup(), [WED], fake_data({WED: Day(underlying=steady("500.00", 6, 390))}))
    outcome = skipped.sessions[0].outcome
    assert outcome.trades == () and outcome.money_steps == ()

    assert CapitalSession.of_outcome(OPEN_MS[WED], outcome) == CapitalSession(
        OPEN_MS[WED], SessionStatus.SKIPPED, (), ()
    )
