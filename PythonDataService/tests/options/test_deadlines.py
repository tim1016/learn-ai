"""The deadlines an option position is held to, on a full day and a half day."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.options.deadlines import (
    CloseKind,
    CloseTerms,
    DueClose,
    ScheduledClose,
    close_deadline_ms,
    close_due_on,
    close_missed_at,
    close_owed_at,
    entry_cutoff_ms,
    entry_cutoff_on,
    last_session_close_ms,
    past_last_session,
    sold_leg_close_ms,
)
from app.options.resolver import ContractsNotResolved, ResolverLimits, ResolverRefusal
from app.options.structure import ResolvedLeg, ResolvedStructure

# The resolver's own scenes, for the one place the two must agree.
from tests.options import test_resolver as scenes

_FRIDAY = date(2026, 10, 9)
_NEXT_FRIDAY = date(2026, 10, 16)
# The day after Thanksgiving: the session closes at 13:00 New York time.
_HALF_DAY = date(2026, 11, 27)
_THANKSGIVING = date(2026, 11, 26)
_TERMS = CloseTerms()


def _structure(*legs: tuple[str, str, date]) -> ResolvedStructure:
    """A structure of calls, each ``(leg id, long or short, expiry)``, a dollar apart from 585 up."""
    return ResolvedStructure(
        legs=tuple(
            ResolvedLeg(leg_id=leg_id, contract=scenes._contract("call", 585 + place, expiry), position=position, ratio=1)
            for place, (leg_id, position, expiry) in enumerate(legs)
        )
    )


@pytest.mark.parametrize(
    ("day", "closes_at"),
    [pytest.param(_FRIDAY, 16, id="a full day"), pytest.param(_HALF_DAY, 13, id="a half day")],
)
def test_each_deadline_is_counted_back_from_that_days_own_session_close(day: date, closes_at: int) -> None:
    """With the starting terms: closed 45 minutes before the close on the expiry day; no entry in the minute and
    twenty seconds before that, which is the entry's 20 second wait, 30 seconds to prove a cancel, and 30 of
    margin; a sold leg closed 10 minutes before the close."""
    assert close_deadline_ms(day, _TERMS) == scenes._at(day, closes_at - 1, 15)
    # 45 minutes and 80 seconds before the close: 15:13:40 on a full day, 12:13:40 on a half day.
    assert entry_cutoff_ms(day, _TERMS) == scenes._at(day, closes_at - 1, 13, 40)
    assert sold_leg_close_ms(day, _TERMS) == scenes._at(day, closes_at - 1, 50)


def test_a_day_with_no_session_has_no_deadline() -> None:
    with pytest.raises(LookupError):
        close_deadline_ms(_THANKSGIVING, _TERMS)
    with pytest.raises(LookupError):
        sold_leg_close_ms(_THANKSGIVING, _TERMS)


def test_the_owners_terms_move_the_deadlines_and_the_cutoff_follows_the_close_deadline() -> None:
    terms = replace(_TERMS, expiry_close_lead_ms=90 * 60_000, entry_wait_ms=120_000)

    assert close_deadline_ms(_FRIDAY, terms) == scenes._at(_FRIDAY, 14, 30)
    # Ninety minutes, then a two-minute wait, thirty seconds to prove a cancel and thirty of margin.
    assert entry_cutoff_ms(_FRIDAY, terms) == scenes._at(_FRIDAY, 14, 27)
    assert terms.entry_cutoff_lead_ms == 93 * 60_000


@pytest.mark.parametrize("term", ["expiry_close_lead_ms", "sold_leg_close_lead_ms", "entry_wait_ms", "cancel_allowance_ms", "entry_margin_ms"])
def test_a_term_below_zero_is_the_callers_mistake(term: str) -> None:
    with pytest.raises(ValueError, match=term):
        replace(_TERMS, **{term: -1})


def test_the_resolver_given_the_terms_cutoff_refuses_from_the_cutoff_instant_on() -> None:
    """The cutoff here and the resolver's refusal are the same instant: nothing is opened from it on, and a
    selection one millisecond before it still resolves."""
    limits = ResolverLimits(max_quote_age_ms=5_000, entry_cutoff_lead_ms=_TERMS.entry_cutoff_lead_ms)
    at_the_cutoff = scenes._scene(day=_FRIDAY, selected_at=(15, 13, 40), limits=limits)
    assert at_the_cutoff.selection_ms == entry_cutoff_ms(_FRIDAY, _TERMS)
    plan = scenes._one("call", {"selector": "atm"})

    with pytest.raises(ContractsNotResolved) as refused:
        at_the_cutoff.resolve(plan)
    just_before = scenes._scene(day=_FRIDAY, selected_at=(15, 13, 39), limits=limits)

    assert refused.value.reason is ResolverRefusal.PAST_ENTRY_CUTOFF
    assert just_before.resolve(plan).picked["only"] == scenes._contract("call", 585)


# ---- When a contract stops trading ---------------------------------------------


@pytest.mark.parametrize(
    ("expiry", "last_trades_until"),
    [
        pytest.param(_FRIDAY, (_FRIDAY, 16), id="a full day: four o'clock on its expiry date"),
        pytest.param(_HALF_DAY, (_HALF_DAY, 13), id="a half day: one o'clock, not the expiry anchor's four"),
        pytest.param(_THANKSGIVING, (date(2026, 11, 25), 16), id="a date with no session: the close of the session before"),
    ],
)
def test_a_contract_stops_trading_at_the_close_of_its_last_session(
    expiry: date, last_trades_until: tuple[date, int]
) -> None:
    day, hour = last_trades_until
    close = scenes._at(day, hour, 0)

    assert last_session_close_ms(expiry) == close
    # Still trading one millisecond before that close; stopped from the close itself, and for good.
    assert not past_last_session(expiry, close - 1)
    assert past_last_session(expiry, close)
    assert past_last_session(expiry, close + 3 * 86_400_000)


# ---- What a held structure owes on a session ----------------------------------


@pytest.mark.parametrize(
    ("legs", "session", "owes"),
    [
        pytest.param(
            [("bought", "long", _NEXT_FRIDAY)], _FRIDAY, None, id="bought only, expiring another day: nothing"
        ),
        pytest.param(
            [("bought", "long", _FRIDAY)],
            _FRIDAY,
            DueClose(CloseKind.EXPIRY_DAY, scenes._at(_FRIDAY, 15, 15)),
            id="bought only, expiring today: the expiry-day close",
        ),
        pytest.param(
            [("bought", "long", _NEXT_FRIDAY), ("sold", "short", _NEXT_FRIDAY)],
            _FRIDAY,
            DueClose(CloseKind.SOLD_LEG, scenes._at(_FRIDAY, 15, 50)),
            id="a sold leg, expiring another day: the sold-leg close",
        ),
        pytest.param(
            [("bought", "long", _FRIDAY), ("sold", "short", _FRIDAY)],
            _FRIDAY,
            DueClose(CloseKind.EXPIRY_DAY, scenes._at(_FRIDAY, 15, 15)),
            id="a sold leg, expiring today: the expiry-day close comes first",
        ),
        pytest.param(
            [("bought", "long", _NEXT_FRIDAY), ("sold", "short", _FRIDAY)],
            _FRIDAY,
            DueClose(CloseKind.EXPIRY_DAY, scenes._at(_FRIDAY, 15, 15)),
            id="one leg of several expires today: the whole structure is closed",
        ),
        pytest.param(
            [("bought", "long", date(2026, 12, 18)), ("sold", "short", date(2026, 12, 18))],
            _HALF_DAY,
            DueClose(CloseKind.SOLD_LEG, scenes._at(_HALF_DAY, 12, 50)),
            id="a sold leg on a half day: ten minutes before one o'clock",
        ),
    ],
)
def test_a_held_structure_owes_the_earlier_of_the_closes_that_apply_to_it(
    legs: list[tuple[str, str, date]], session: date, owes: DueClose | None
) -> None:
    assert close_due_on(_structure(*legs), session, _TERMS) == owes


def test_when_the_sold_leg_close_is_set_earlier_than_the_expiry_days_it_is_the_one_owed() -> None:
    terms = replace(_TERMS, sold_leg_close_lead_ms=60 * 60_000)
    expiring_today = _structure(("bought", "long", _FRIDAY), ("sold", "short", _FRIDAY))

    assert close_due_on(expiring_today, _FRIDAY, terms) == DueClose(CloseKind.SOLD_LEG, scenes._at(_FRIDAY, 15, 0))


# ---- From when no entry is opened on a structure ------------------------------


@pytest.mark.parametrize(
    ("legs", "session", "no_entry_from"),
    [
        pytest.param([("bought", "long", _NEXT_FRIDAY)], _FRIDAY, None, id="expiring another day: no cutoff today"),
        pytest.param(
            [("bought", "long", _FRIDAY)], _FRIDAY, (_FRIDAY, 15, 13, 40), id="expiring today: 80 seconds before 15:15"
        ),
        pytest.param(
            [("bought", "long", _HALF_DAY)], _HALF_DAY, (_HALF_DAY, 12, 13, 40), id="expiring on a half day: before 12:15"
        ),
        pytest.param(
            [("bought", "long", _NEXT_FRIDAY), ("sold", "short", _NEXT_FRIDAY)],
            _FRIDAY,
            (_FRIDAY, 15, 48, 40),
            id="a sold leg: 80 seconds before the sold-leg close at 15:50",
        ),
    ],
)
def test_no_entry_is_opened_from_the_close_its_structure_would_owe_less_the_entrys_own_life(
    legs: list[tuple[str, str, date]], session: date, no_entry_from: tuple[date, int, int, int] | None
) -> None:
    cutoff = entry_cutoff_on(_structure(*legs), session, _TERMS)

    assert cutoff == (None if no_entry_from is None else scenes._at(*no_entry_from))


def test_a_bought_contracts_cutoff_on_its_expiry_day_is_the_one_the_resolver_is_given() -> None:
    """One instant, asked two ways: of the contract's expiry, and of the structure on that session."""
    assert entry_cutoff_on(_structure(("bought", "long", _FRIDAY)), _FRIDAY, _TERMS) == entry_cutoff_ms(_FRIDAY, _TERMS)
    longer_wait = replace(_TERMS, entry_wait_ms=60_000)
    assert entry_cutoff_on(_structure(("bought", "long", _FRIDAY)), _FRIDAY, longer_wait) == scenes._at(_FRIDAY, 15, 13, 0)


# ---- The close owed at an instant ---------------------------------------------

_MONDAY = date(2026, 10, 5)
_THURSDAY = date(2026, 10, 8)
_SATURDAY = date(2026, 10, 10)
_NEXT_MONDAY = date(2026, 10, 12)
_FAR = date(2026, 12, 18)
_WITH_A_SOLD_LEG = [("bought", "long", _FAR), ("sold", "short", _FAR)]


@pytest.mark.parametrize(
    ("day", "closes_at"),
    [pytest.param(_FRIDAY, 16, id="a full day"), pytest.param(_HALF_DAY, 13, id="a half day")],
)
def test_a_contract_expiring_today_owes_its_close_from_45_minutes_before_the_sessions_close_until_that_close(
    day: date, closes_at: int
) -> None:
    structure = _structure(("bought", "long", day))
    due, session_close = scenes._at(day, closes_at - 1, 15), scenes._at(day, closes_at, 0)

    owed = close_owed_at(structure, _TERMS, at_ms=due - 1)

    # 15:15 on a full day and 12:15 on a half day, in the session that opened at 9:30.
    assert owed == ScheduledClose(CloseKind.EXPIRY_DAY, due, scenes._at(day, 9, 30), session_close)
    assert not owed.due(due - 1) and owed.due(due) and owed.due(session_close - 1)
    assert not owed.due(session_close)
    # Before the open it is the close of the session to come. From the session's close on, none is owed.
    assert close_owed_at(structure, _TERMS, at_ms=scenes._at(day, 8, 0)) == owed
    assert close_owed_at(structure, _TERMS, at_ms=session_close) is None


@pytest.mark.parametrize("day", [pytest.param(_THANKSGIVING, id="a holiday"), pytest.param(_SATURDAY, id="a weekend")])
def test_no_close_is_owed_on_a_day_with_no_session(day: date) -> None:
    assert close_owed_at(_structure(*_WITH_A_SOLD_LEG), _TERMS, at_ms=scenes._at(day, 12, 0)) is None


def test_the_day_a_close_is_owed_on_is_new_yorks_and_not_the_day_it_already_is_in_utc() -> None:
    """Between eight in the evening and midnight in New York the UTC date is already the next day's."""
    expiring_friday = _structure(("bought", "long", _FRIDAY))
    thursday_at_nine_in_the_evening = scenes._at(_THURSDAY, 21, 0)
    friday_half_past_midnight = scenes._at(_FRIDAY, 0, 30)

    # Thursday evening it is still Thursday: that session has ended and Friday's close is not owed yet.
    assert close_owed_at(expiring_friday, _TERMS, at_ms=thursday_at_nine_in_the_evening) is None
    assert close_owed_at(_structure(*_WITH_A_SOLD_LEG), _TERMS, at_ms=thursday_at_nine_in_the_evening) is None
    # From midnight in New York it is Friday, and the close of the session to come is the one owed.
    owed = close_owed_at(expiring_friday, _TERMS, at_ms=friday_half_past_midnight)
    assert owed is not None and owed.due_ms == scenes._at(_FRIDAY, 15, 15)
    # Friday evening the session has ended, though in UTC it is already Saturday.
    assert close_owed_at(expiring_friday, _TERMS, at_ms=scenes._at(_FRIDAY, 20, 30)) is None


def test_a_bought_contract_expiring_another_day_owes_no_close_today() -> None:
    assert close_owed_at(_structure(("bought", "long", _NEXT_FRIDAY)), _TERMS, at_ms=scenes._at(_FRIDAY, 15, 30)) is None


@pytest.mark.parametrize(
    ("day", "closes_at"),
    [
        pytest.param(_MONDAY, 16, id="Monday"),
        pytest.param(_THURSDAY, 16, id="Thursday"),
        pytest.param(_FRIDAY, 16, id="Friday"),
        pytest.param(_HALF_DAY, 13, id="a half day"),
    ],
)
def test_a_structure_with_a_sold_leg_owes_a_close_10_minutes_before_every_sessions_close(
    day: date, closes_at: int
) -> None:
    owed = close_owed_at(_structure(*_WITH_A_SOLD_LEG), _TERMS, at_ms=scenes._at(day, 10, 0))

    assert owed == ScheduledClose(
        CloseKind.SOLD_LEG, scenes._at(day, closes_at - 1, 50), scenes._at(day, 9, 30), scenes._at(day, closes_at, 0)
    )


# ---- The close a session ended on ---------------------------------------------


def test_a_contract_still_held_after_its_last_session_missed_its_close_however_long_ago() -> None:
    structure = _structure(("bought", "long", _FRIDAY))
    held_since = scenes._at(_MONDAY, 12, 0)
    last_close = scenes._at(_FRIDAY, 16, 0)
    missed = ScheduledClose(CloseKind.EXPIRY_DAY, scenes._at(_FRIDAY, 15, 15), scenes._at(_FRIDAY, 9, 30), last_close)

    assert close_missed_at(structure, _TERMS, held_since_ms=held_since, at_ms=last_close - 1) is None
    assert close_missed_at(structure, _TERMS, held_since_ms=held_since, at_ms=last_close) == missed
    # The weekend after, and ten days on: it is the same close that was missed.
    assert close_missed_at(structure, _TERMS, held_since_ms=held_since, at_ms=scenes._at(_SATURDAY, 12, 0)) == missed
    assert close_missed_at(structure, _TERMS, held_since_ms=held_since, at_ms=scenes._at(date(2026, 10, 19), 11, 0)) == missed


def test_a_contract_that_has_not_expired_and_has_no_sold_leg_missed_nothing() -> None:
    structure = _structure(("bought", "long", _NEXT_FRIDAY))

    assert close_missed_at(
        structure, _TERMS, held_since_ms=scenes._at(_MONDAY, 12, 0), at_ms=scenes._at(_NEXT_MONDAY, 11, 0)
    ) is None


def test_a_sold_leg_still_held_after_a_sessions_close_missed_the_close_of_the_last_session_that_ended() -> None:
    structure = _structure(*_WITH_A_SOLD_LEG)
    held_since = scenes._at(_THURSDAY, 12, 0)
    fridays = ScheduledClose(
        CloseKind.SOLD_LEG, scenes._at(_FRIDAY, 15, 50), scenes._at(_FRIDAY, 9, 30), scenes._at(_FRIDAY, 16, 0)
    )

    def missed(at_ms: int) -> ScheduledClose | None:
        return close_missed_at(structure, _TERMS, held_since_ms=held_since, at_ms=at_ms)

    # Thursday's close passed with it held: that is the one missed until Friday's has passed too.
    assert missed(scenes._at(_FRIDAY, 15, 59)) == ScheduledClose(
        CloseKind.SOLD_LEG, scenes._at(_THURSDAY, 15, 50), scenes._at(_THURSDAY, 9, 30), scenes._at(_THURSDAY, 16, 0)
    )
    assert missed(scenes._at(_FRIDAY, 16, 0)) == fridays
    # Over the weekend, and through Monday's session, the last session that ended is still Friday's.
    assert missed(scenes._at(_SATURDAY, 12, 0)) == fridays
    assert missed(scenes._at(_NEXT_MONDAY, 11, 0)) == fridays
    assert missed(scenes._at(_NEXT_MONDAY, 16, 0)) == ScheduledClose(
        CloseKind.SOLD_LEG, scenes._at(_NEXT_MONDAY, 15, 50), scenes._at(_NEXT_MONDAY, 9, 30), scenes._at(_NEXT_MONDAY, 16, 0)
    )


@pytest.mark.parametrize(
    ("held_since", "at"),
    [
        pytest.param((_FRIDAY, 15, 50), (_FRIDAY, 16, 0), id="taken at the instant the close was due"),
        pytest.param((_NEXT_MONDAY, 10, 0), (_NEXT_MONDAY, 11, 0), id="taken the morning after"),
    ],
)
def test_a_structure_taken_once_a_sessions_close_was_due_missed_nothing_that_session(
    held_since: tuple[date, int, int], at: tuple[date, int, int]
) -> None:
    structure = _structure(*_WITH_A_SOLD_LEG)

    assert close_missed_at(structure, _TERMS, held_since_ms=scenes._at(*held_since), at_ms=scenes._at(*at)) is None
    # Held one millisecond sooner than Friday's close was due, it missed that close.
    if held_since[0] == _FRIDAY:
        assert close_missed_at(
            structure, _TERMS, held_since_ms=scenes._at(*held_since) - 1, at_ms=scenes._at(*at)
        ) is not None


def test_when_a_leg_expired_and_a_sold_leg_was_held_past_a_later_close_the_earlier_close_is_the_one_told() -> None:
    structure = _structure(("bought", "long", _NEXT_FRIDAY), ("sold", "short", _FRIDAY))

    missed = close_missed_at(
        structure, _TERMS, held_since_ms=scenes._at(_MONDAY, 12, 0), at_ms=scenes._at(_NEXT_MONDAY, 16, 30)
    )

    assert missed == ScheduledClose(
        CloseKind.EXPIRY_DAY, scenes._at(_FRIDAY, 15, 15), scenes._at(_FRIDAY, 9, 30), scenes._at(_FRIDAY, 16, 0)
    )


def test_a_contract_whose_expiry_date_has_no_session_missed_its_close_in_the_session_before() -> None:
    """Its last session is the one before the date. No session's own close is owed on the date itself
    (``close_due_on`` asks for a contract expiring on the session), so such a contract is only ever told as
    missed."""
    structure = _structure(("bought", "long", _THANKSGIVING))
    wednesday = date(2026, 11, 25)

    missed = close_missed_at(
        structure, _TERMS, held_since_ms=scenes._at(wednesday, 10, 0), at_ms=scenes._at(wednesday, 16, 0)
    )

    assert close_owed_at(structure, _TERMS, at_ms=scenes._at(wednesday, 15, 30)) is None
    assert missed == ScheduledClose(
        CloseKind.EXPIRY_DAY, scenes._at(wednesday, 15, 15), scenes._at(wednesday, 9, 30), scenes._at(wednesday, 16, 0)
    )
