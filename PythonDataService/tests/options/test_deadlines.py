"""The deadlines an option position is held to, on a full day and a half day."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.options.deadlines import (
    CloseKind,
    CloseTerms,
    DueClose,
    close_deadline_ms,
    close_due_on,
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
