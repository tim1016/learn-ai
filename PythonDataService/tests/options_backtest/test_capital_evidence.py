"""How far the cash provably carried a run: the classes of a close, the event that ends the evidence, and each figure.

Every figure is worked by hand from the rules and written as a literal, and every comparison is exact (cents are
whole numbers of cents): there is no tolerance because there is nothing to tolerate. A scene is built from the money
steps a replay states, never from a replay, so that this file proves the arithmetic and ``test_run_capital.py``
proves that a replay feeds it.

The spread every scene is built on is a credit put spread 5 wide, one contract: the 500 put is sold at 2.20 and the 495
put bought at 1.00, so the opening group brings in 120.00 and pledges 500.00 (5 wide x 100), and the sold leg is
50,000.00 of assignment obligation (500 x 100). Closed whole, the 500 is bought back at 1.50 and the 495 sold at 0.60:
-90.00, leaving the cash 30.00 above where it began.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.options_backtest.capital_evidence import (
    CapitalInvalidEvent,
    CapitalInvalidKind,
    CapitalSession,
    capital_evidence,
)
from app.options_backtest.results import Event, EventKind, FillGroup, MoneyStep, SessionOutcome, SessionStatus

D = Decimal
OPEN_1, OPEN_2, OPEN_3 = 1_000_000, 2_000_000, 3_000_000
MINUTE = 60_000


def _opening(
    at: int, cash_before: str, cash_after: str, *, collateral: str = "500.00", obligation: str = "50000"
) -> MoneyStep:
    return MoneyStep(at, FillGroup.OPENING, D(cash_before), D(cash_after), D("0"), D(collateral), D(obligation), False)


def _closing(
    at: int,
    cash_before: str,
    cash_after: str,
    collateral_before: str,
    collateral_after: str,
    *,
    under_cover: bool = False,
    obligation: str = "0",
) -> MoneyStep:
    return MoneyStep(
        at,
        FillGroup.CLOSING,
        D(cash_before),
        D(cash_after),
        D(collateral_before),
        D(collateral_after),
        D(obligation),
        under_cover,
    )


def _session(
    open_ms: int,
    steps: tuple[MoneyStep, ...] = (),
    *,
    status: SessionStatus = SessionStatus.TRADED,
    refused_at: tuple[int, ...] = (),
) -> CapitalSession:
    return CapitalSession(open_ms, status, steps, refused_at)


def _spread_closed_whole(open_ms: int, cash: str) -> CapitalSession:
    """The spread opened and closed whole in one session, from ``cash``: +120.00 then -90.00."""
    start = D(cash)
    opened, closed = start + D("120.00"), start + D("30.00")
    return _session(
        open_ms,
        (
            _opening(open_ms + 6 * MINUTE, cash, str(opened)),
            _closing(open_ms + 61 * MINUTE, str(opened), str(closed), "500.00", "0"),
        ),
    )


# ── A run whose every close is funded ────────────────────────────────────


def test_a_run_whose_every_close_is_funded_is_funded_through_all_its_sessions_and_has_no_event() -> None:
    # Session 1 from 10,000.00: opens at +120.00 (free cash 9,620.00), closes at 10,030.00.
    # Session 2 from 10,030.00: opens at 10,150.00 (free cash 9,650.00), closes at 10,060.00.
    sessions = [_spread_closed_whole(OPEN_1, "10000.00"), _spread_closed_whole(OPEN_2, "10030.00")]

    evidence = capital_evidence(sessions, starting_cash=D("10000.00"))

    assert (evidence.sessions_tested, evidence.sessions_funded_continuously) == (2, 2)
    assert evidence.ended_by is None
    assert evidence.most_collateral_held_usd == D("500.00")
    assert evidence.least_free_cash_usd == D("9620.00")
    assert evidence.largest_assignment_obligation_usd == D("50000")
    assert evidence.closes_relied_on_own_relief == 0
    assert evidence.cash_needed_is_incomplete is False


def test_the_cash_this_path_needed_is_the_largest_collateral_less_the_cash_flow_over_the_stretch() -> None:
    sessions = [_spread_closed_whole(OPEN_1, "10000.00"), _spread_closed_whole(OPEN_2, "10030.00")]

    evidence = capital_evidence(sessions, starting_cash=D("10000.00"))

    # Collateral after each step less the cash flow by then (cash after less 10,000.00):
    #   session 1 opening: 500.00 - 120.00 = 380.00;   session 1 closing: 0 - 30.00 = -30.00;
    #   session 2 opening: 500.00 - 150.00 = 350.00;   session 2 closing: 0 - 60.00 = -60.00.
    # The most is 380.00: with 380.00 of starting cash free cash is exactly nothing at the first opening.
    assert evidence.cash_needed_usd == D("380.00")


def test_a_run_that_made_no_trade_needs_nothing_holds_nothing_and_saw_its_starting_cash() -> None:
    sessions = [_session(OPEN_1, status=SessionStatus.NO_TRADE), _session(OPEN_2, status=SessionStatus.SKIPPED)]

    evidence = capital_evidence(sessions, starting_cash=D("10000.00"))

    assert (evidence.sessions_tested, evidence.sessions_funded_continuously, evidence.ended_by) == (2, 2, None)
    assert evidence.cash_needed_usd == D("0")
    assert evidence.most_collateral_held_usd == D("0")
    assert evidence.largest_assignment_obligation_usd == D("0")
    assert evidence.least_free_cash_usd == D("10000.00")
    assert evidence.cash_needed_is_incomplete is False


def test_a_run_of_no_sessions_is_funded_through_none_of_none() -> None:
    evidence = capital_evidence([], starting_cash=D("10000.00"))

    assert (evidence.sessions_tested, evidence.sessions_funded_continuously, evidence.ended_by) == (0, 0, None)


# ── A bought option: no collateral, and the cash needed is what it paid ──


def test_a_bought_options_run_shows_no_collateral_and_a_cash_need_equal_to_what_it_paid_fees_included() -> None:
    # One call bought at 2.00 (200.00) with a 0.65 fee: 10,000.00 -> 9,799.35. Sold at 3.00 (300.00) less 0.65 -> 10,098.70.
    session = _session(
        OPEN_1,
        (
            MoneyStep(OPEN_1 + 6 * MINUTE, FillGroup.OPENING, D("10000"), D("9799.35"), D("0"), D("0"), D("0"), False),
            MoneyStep(
                OPEN_1 + 61 * MINUTE, FillGroup.CLOSING, D("9799.35"), D("10098.70"), D("0"), D("0"), D("0"), False
            ),
        ),
    )

    evidence = capital_evidence([session], starting_cash=D("10000"))

    assert evidence.most_collateral_held_usd == D("0")
    assert evidence.largest_assignment_obligation_usd == D("0")
    # 0 - (9,799.35 - 10,000) = 200.65 at the buy, then 0 - 98.70 = -98.70.
    assert evidence.cash_needed_usd == D("200.65")
    assert evidence.least_free_cash_usd == D("9799.35")
    assert evidence.ended_by is None


# ── An unfunded close ends the evidence ──────────────────────────────────

# Starting cash 600.00. Session 1 is the spread opened (720.00) and closed whole (630.00). Session 2 opens it again
# (750.00, free cash 250.00) and then buys the sold leg back alone, at 9.00, while the 495 put stays held: -900.00, so
# cash 750.00 -> -150.00, and the bought 495 alone pledges nothing: 0. Free cash after it: -150.00.
_UNFUNDED_AT = OPEN_2 + 61 * MINUTE


def _a_run_with_an_unfunded_close() -> list[CapitalSession]:
    second = _session(
        OPEN_2,
        (
            _opening(OPEN_2 + 6 * MINUTE, "630.00", "750.00"),
            _closing(_UNFUNDED_AT, "750.00", "-150.00", "500.00", "0", under_cover=True),
        ),
        status=SessionStatus.INCOMPLETE,
    )
    # A later session still ran: it could not open anything with cash under the collateral, so it holds nothing.
    return [_spread_closed_whole(OPEN_1, "600.00"), second, _session(OPEN_3, status=SessionStatus.NO_TRADE)]


def test_an_unfunded_close_is_named_with_its_session_and_minute_and_the_evidence_ends_there() -> None:
    evidence = capital_evidence(_a_run_with_an_unfunded_close(), starting_cash=D("600.00"))

    assert evidence.ended_by == CapitalInvalidEvent(
        CapitalInvalidKind.UNFUNDED_CLOSE, OPEN_2, _UNFUNDED_AT, D("-150.00")
    )
    # Session 1 ended before the event; session 2 holds it; session 3 is later and still ran.
    assert (evidence.sessions_tested, evidence.sessions_funded_continuously) == (3, 1)


def test_the_cash_needed_is_worked_over_the_stretch_alone_and_the_unfunded_step_is_not_in_it() -> None:
    evidence = capital_evidence(_a_run_with_an_unfunded_close(), starting_cash=D("600.00"))

    # Session 1 opening: 500.00 - 120.00 = 380.00; its closing: 0 - 30.00 = -30.00.
    # Session 2 opening, which came before the event: 500.00 - 150.00 = 350.00.
    # The unfunded close (0 - (-150.00 - 600.00) = 750.00) is the event, and not funded: it is not in the stretch.
    assert evidence.cash_needed_usd == D("380.00")


def test_the_other_four_figures_are_the_whole_runs_and_the_least_free_cash_goes_below_zero() -> None:
    evidence = capital_evidence(_a_run_with_an_unfunded_close(), starting_cash=D("600.00"))

    # Free cash seen: 600.00 at the start, 220.00 (720.00 - 500.00), 130.00 and 630.00 in session 1, 250.00 after
    # session 2's opening, and -150.00 after the close.
    assert evidence.least_free_cash_usd == D("-150.00")
    assert evidence.most_collateral_held_usd == D("500.00")
    assert evidence.largest_assignment_obligation_usd == D("50000")
    # The close cost 900.00 against 250.00 of free cash before it, with its cover held: it relied on its own relief too.
    assert evidence.closes_relied_on_own_relief == 1


def test_an_unfunded_close_in_the_first_session_is_funded_through_none() -> None:
    sessions = [
        _session(
            OPEN_1,
            (
                _opening(OPEN_1 + 6 * MINUTE, "500.00", "620.00"),
                _closing(OPEN_1 + 61 * MINUTE, "620.00", "-180.00", "500.00", "0", under_cover=True),
            ),
            status=SessionStatus.INCOMPLETE,
        ),
        _session(OPEN_2),
    ]

    evidence = capital_evidence(sessions, starting_cash=D("500.00"))

    assert (evidence.sessions_tested, evidence.sessions_funded_continuously) == (2, 0)
    assert evidence.ended_by == CapitalInvalidEvent(
        CapitalInvalidKind.UNFUNDED_CLOSE, OPEN_1, OPEN_1 + 61 * MINUTE, D("-180.00")
    )


def test_a_close_that_leaves_the_cash_exactly_at_the_collateral_is_funded_and_one_cent_under_is_not() -> None:
    def a_close_leaving(cash_after: str) -> CapitalSession:
        # 800.00 in cash and 500.00 pledged; the close costs enough to leave ``cash_after`` and keeps 500.00 pledged.
        return _session(OPEN_1, (_closing(OPEN_1 + 61 * MINUTE, "800.00", cash_after, "500.00", "500.00"),))

    exactly = capital_evidence([a_close_leaving("500.00")], starting_cash=D("800.00"))
    one_cent_under = capital_evidence([a_close_leaving("499.99")], starting_cash=D("800.00"))

    assert exactly.ended_by is None
    assert one_cent_under.ended_by is not None
    assert one_cent_under.ended_by.free_cash_after_usd == D("-0.01")


# ── A close that relied on its own relief is counted and does not end the evidence ─


def _a_buy_back_alone(cash_before: str, cash_after: str) -> CapitalSession:
    """The spread opened with free cash 120.00 (cash 620.00 against 500.00), then the sold leg bought back alone with
    its cover held: the collateral goes from 500.00 to nothing, since a bought put alone pledges none."""
    return _session(
        OPEN_1,
        (
            _opening(OPEN_1 + 6 * MINUTE, "500.00", cash_before),
            _closing(OPEN_1 + 61 * MINUTE, cash_before, cash_after, "500.00", "0", under_cover=True),
        ),
    )


def test_a_buy_back_alone_that_cost_more_than_the_free_cash_before_it_and_left_free_cash_at_or_above_zero_relied() -> (
    None
):
    # Cost 220.00 against 120.00 of free cash before it; free cash after is 400.00.
    evidence = capital_evidence([_a_buy_back_alone("620.00", "400.00")], starting_cash=D("500.00"))

    assert evidence.closes_relied_on_own_relief == 1
    assert evidence.ended_by is None
    assert evidence.sessions_funded_continuously == 1


def test_a_buy_back_that_cost_no_more_than_the_free_cash_before_it_did_not_rely_on_relief() -> None:
    # Cost 120.00 against 120.00 of free cash: exactly the free cash, which is not more than it.
    exactly = capital_evidence([_a_buy_back_alone("620.00", "500.00")], starting_cash=D("500.00"))
    one_cent_more = capital_evidence([_a_buy_back_alone("620.00", "499.99")], starting_cash=D("500.00"))

    assert exactly.closes_relied_on_own_relief == 0
    assert one_cent_more.closes_relied_on_own_relief == 1


@pytest.mark.parametrize(("cover_stayed_held", "relied"), [(False, 0), (True, 1)])
def test_a_close_relied_on_its_own_relief_only_when_the_cover_stayed_held_whatever_it_cost(
    cover_stayed_held: bool, relied: int
) -> None:
    # 500.00 of cash; the spread opens to 620.00 with 500.00 pledged, so 120.00 is free. The close takes 620.00 to
    # 380.00: 240.00, which is more than the 120.00 free before it either way. It is funded (380.00 is not under 0).
    # Whole, the cover is sold in the same group (it goes with the sold leg) so none stayed held and the close did not
    # rely on its own relief. Had the cover stayed held, a cost of 240.00 against 120.00 free would have.
    session = _session(
        OPEN_1,
        (
            _opening(OPEN_1 + 6 * MINUTE, "500.00", "620.00"),
            _closing(OPEN_1 + 61 * MINUTE, "620.00", "380.00", "500.00", "0", under_cover=cover_stayed_held),
        ),
    )

    evidence = capital_evidence([session], starting_cash=D("500.00"))

    assert evidence.closes_relied_on_own_relief == relied
    assert evidence.ended_by is None


# ── A session ending with a position marked ends the evidence at that session ──


def test_a_session_that_ended_with_a_position_marked_ends_the_evidence_at_that_session() -> None:
    # Session 2 opens the spread (10,030.00 -> 10,050.00: only +20.00 comes in) and ends marked, with no close.
    # Session 3 would need more (cash 10,000.00 -> 10,010.00 and 500.00 pledged: 490.00) but is not in the stretch.
    sessions = [
        _spread_closed_whole(OPEN_1, "10000.00"),
        _session(OPEN_2, (_opening(OPEN_2 + 6 * MINUTE, "10030.00", "10050.00"),), status=SessionStatus.INCOMPLETE),
        _session(OPEN_3, (_opening(OPEN_3 + 6 * MINUTE, "10000.00", "10010.00"),)),
    ]

    evidence = capital_evidence(sessions, starting_cash=D("10000.00"))

    assert evidence.ended_by == CapitalInvalidEvent(CapitalInvalidKind.POSITION_MARKED, OPEN_2, None, None)
    assert (evidence.sessions_tested, evidence.sessions_funded_continuously) == (3, 1)
    # The marked session's own opening is in the stretch (500.00 - 50.00 = 450.00); session 3's (490.00) is not.
    assert evidence.cash_needed_usd == D("450.00")
    # What the replay held is the whole run's: session 3 pledged 500.00 too, and saw 9,510.00 free.
    assert evidence.least_free_cash_usd == D("9510.00")


def test_a_run_that_ends_marked_in_its_last_session_is_funded_through_the_session_before_it() -> None:
    sessions = [
        _spread_closed_whole(OPEN_1, "10000.00"),
        _session(OPEN_2, (_opening(OPEN_2 + 6 * MINUTE, "10030.00", "10150.00"),), status=SessionStatus.INCOMPLETE),
    ]

    evidence = capital_evidence(sessions, starting_cash=D("10000.00"))

    assert (evidence.sessions_tested, evidence.sessions_funded_continuously) == (2, 1)
    assert evidence.ended_by is not None and evidence.ended_by.kind is CapitalInvalidKind.POSITION_MARKED


def test_a_bought_leg_marked_alone_ends_the_evidence_too() -> None:
    # A bought option that was never sold: nothing was pledged, and the session still ended with it marked.
    session = _session(
        OPEN_1,
        (MoneyStep(OPEN_1 + 6 * MINUTE, FillGroup.OPENING, D("10000"), D("9800"), D("0"), D("0"), D("0"), False),),
        status=SessionStatus.INCOMPLETE,
    )

    evidence = capital_evidence([session], starting_cash=D("10000"))

    assert evidence.ended_by == CapitalInvalidEvent(CapitalInvalidKind.POSITION_MARKED, OPEN_1, None, None)
    assert evidence.sessions_funded_continuously == 0
    assert evidence.cash_needed_usd == D("200")


def test_the_first_event_of_the_run_is_the_one_named_even_when_a_later_session_has_another() -> None:
    marked_first = _session(
        OPEN_1, (_opening(OPEN_1 + 6 * MINUTE, "10000.00", "10120.00"),), status=SessionStatus.INCOMPLETE
    )
    unfunded_after = _session(
        OPEN_2,
        (_closing(OPEN_2 + 61 * MINUTE, "100.00", "-50.00", "500.00", "0", under_cover=True),),
    )

    evidence = capital_evidence([marked_first, unfunded_after], starting_cash=D("10000.00"))

    assert evidence.ended_by is not None and evidence.ended_by.kind is CapitalInvalidKind.POSITION_MARKED
    assert evidence.sessions_funded_continuously == 0


def test_an_unfunded_close_before_the_end_of_a_marked_session_is_the_event_and_not_the_mark() -> None:
    both = _session(
        OPEN_1,
        (
            _opening(OPEN_1 + 6 * MINUTE, "500.00", "620.00"),
            _closing(OPEN_1 + 61 * MINUTE, "620.00", "-180.00", "500.00", "0", under_cover=True),
        ),
        status=SessionStatus.INCOMPLETE,
    )

    evidence = capital_evidence([both], starting_cash=D("500.00"))

    assert evidence.ended_by is not None and evidence.ended_by.kind is CapitalInvalidKind.UNFUNDED_CLOSE


# ── An entry refused for cash makes the cash needed incomplete ───────────


def test_an_entry_refused_for_cash_in_the_stretch_marks_the_cash_needed_incomplete_and_leaves_its_value() -> None:
    refused = _session(OPEN_2, status=SessionStatus.NO_TRADE, refused_at=(OPEN_2 + 6 * MINUTE,))
    sessions = [_spread_closed_whole(OPEN_1, "499.00"), refused]

    evidence = capital_evidence(sessions, starting_cash=D("499.00"))

    assert evidence.cash_needed_is_incomplete is True
    assert evidence.ended_by is None
    # It is still the cash the recorded path needed: 500.00 - 120.00 = 380.00.
    assert evidence.cash_needed_usd == D("380.00")


def test_a_run_with_no_refusal_for_cash_in_the_stretch_is_complete() -> None:
    assert (
        capital_evidence(
            [_spread_closed_whole(OPEN_1, "10000.00")], starting_cash=D("10000.00")
        ).cash_needed_is_incomplete
        is False
    )


def test_a_refusal_after_the_event_is_not_in_the_stretch_and_one_before_or_at_its_minute_is() -> None:
    def run(refused_at: tuple[int, ...]) -> bool:
        session = _session(
            OPEN_1,
            (
                _opening(OPEN_1 + 6 * MINUTE, "500.00", "620.00"),
                _closing(OPEN_1 + 61 * MINUTE, "620.00", "-180.00", "500.00", "0", under_cover=True),
            ),
            refused_at=refused_at,
        )
        return capital_evidence([session], starting_cash=D("500.00")).cash_needed_is_incomplete

    assert run(()) is False
    assert run((OPEN_1 + 30 * MINUTE,)) is True
    # Entries fill before closes in a minute, so a refusal in the minute of the unfunded close came before it.
    assert run((OPEN_1 + 61 * MINUTE,)) is True
    assert run((OPEN_1 + 62 * MINUTE,)) is False


def test_a_refusal_in_a_session_that_ended_marked_is_in_the_stretch_whole() -> None:
    session = _session(
        OPEN_1,
        (_opening(OPEN_1 + 6 * MINUTE, "10000.00", "10120.00"),),
        status=SessionStatus.INCOMPLETE,
        refused_at=(OPEN_1 + 200 * MINUTE,),
    )

    assert capital_evidence([session], starting_cash=D("10000.00")).cash_needed_is_incomplete is True


# ── Reading an outcome ───────────────────────────────────────────────────


def test_a_replayed_outcome_gives_its_status_its_steps_and_only_the_minutes_of_entries_refused_for_cash() -> None:
    steps = (_opening(OPEN_1 + 6 * MINUTE, "10000.00", "10120.00"),)
    events = (
        Event(EventKind.ENTRY_REFUSED_FOR_CASH, OPEN_1 + 6 * MINUTE, "long", 0),
        Event(EventKind.ENTRY_REFUSED_FOR_COVER, OPEN_1 + 7 * MINUTE, "short", 0),
        Event(EventKind.ENTRY_REFUSED_FOR_CASH, OPEN_1 + 8 * MINUTE, "short", 0),
    )
    outcome = SessionOutcome(SessionStatus.TRADED, None, (), events, D("120.00"), steps)

    session = CapitalSession.of_outcome(OPEN_1, outcome)

    assert session == CapitalSession(OPEN_1, SessionStatus.TRADED, steps, (OPEN_1 + 6 * MINUTE, OPEN_1 + 8 * MINUTE))


# ── Exactness ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cents", [1, 33, 99, 100_001])
def test_figures_are_exact_to_the_cent_whatever_the_amount(cents: int) -> None:
    # An amount that is not a binary fraction survives: 0.33 and its sums are whole cents, not floats.
    start = D("10000.00")
    flow = D(cents) / D(100)
    session = _session(
        OPEN_1,
        (MoneyStep(OPEN_1 + MINUTE, FillGroup.OPENING, start, start - flow, D("0"), D("0"), D("0"), False),),
    )

    evidence = capital_evidence([session], starting_cash=start)

    assert evidence.cash_needed_usd == flow
    assert evidence.least_free_cash_usd == start - flow
