"""The backtest's picking rules agree with the ones bots use, `app/options/resolver.py`, where both have a rule.

The resolver demands live two-sided quotes for every rule, which a past date does not have, so the backtest
picks on its own types. These tests run both on the same inputs so the two cannot drift apart unseen.

Expiry rules are compared through ``expiry_a_rule_comes_to``, over every session of two years and several
shapes of what is listed. The strike rules are compared through ``resolve_contracts``, whose money and step
helpers are private, over a small chain that every contract has a usable quote in.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import (
    Chain,
    ContractsNotResolved,
    FeedState,
    Listing,
    ModelInputs,
    ResolverLimits,
    ResolverRefusal,
    UnderlyingObservation,
    WatchedContract,
    expiry_a_rule_comes_to,
    resolve_contracts,
)
from app.options_backtest.picking import expiry_for, strike_for
from app.options_backtest.setup import (
    AtTheMoney,
    ExpiryRule,
    MinDaysExpiryRule,
    SameDayExpiryRule,
    StepsFromTheMoney,
    ThisWeekExpiryRule,
)
from app.schemas.action_plan import ActionPlan, OptionEntryLeg
from app.utils.timestamps import to_ms_utc

_NY = ZoneInfo("America/New_York")

# ---- Expiry rules --------------------------------------------------------------

_FIRST_DAY = date(2024, 10, 1)
_LAST_DAY = date(2026, 10, 9)
_HORIZON_DAYS = 70
_SESSIONS = tuple(expected_sessions(_FIRST_DAY, _LAST_DAY + timedelta(days=_HORIZON_DAYS)))
_SESSION_SET = frozenset(_SESSIONS)
_TRADING_DATES = tuple(day for day in _SESSIONS if day <= _LAST_DAY)


def _week_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


_LAST_SESSION_OF_WEEK = frozenset(
    max(day for day in _SESSIONS if _week_of(day) == week) for week in {_week_of(day) for day in _SESSIONS}
)


def _every_calendar_day(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


# What a chain lists, from one trading date: each is a function of the first and last date it may list.
_LISTED: dict[str, Callable[[date, date], list[date]]] = {
    "every session": lambda first, last: [day for day in _SESSIONS if first <= day <= last],
    "every calendar day, weekends and holidays too": _every_calendar_day,
    "each week's last session": lambda first, last: [day for day in _LAST_SESSION_OF_WEEK if first <= day <= last],
    "every third session": lambda first, last: [day for day in _SESSIONS if first <= day <= last][::3],
    "mondays and wednesdays": lambda first, last: [
        day for day in _SESSIONS if first <= day <= last and day.weekday() in (0, 2)
    ],
    "nothing": lambda first, last: [],
}


def _leg_with(expiry: dict[str, object]) -> OptionEntryLeg:
    return OptionEntryLeg.model_validate(
        {
            "leg_id": "leg",
            "instrument": {"kind": "option", "underlying": "SPY"},
            "position": "long",
            "qty_ratio": 1,
            "right": "call",
            "strike": {"selector": "atm"},
            "expiry": expiry,
        }
    )


def _agree_on_every_session(
    shape: str,
    ours: ExpiryRule,
    theirs: dict[str, object],
    *,
    expected: Callable[[date, Sequence[date], date | None], date | None] = lambda _day, _listed, theirs: theirs,
) -> int:
    """Run both rules for every trading date over what ``shape`` lists; return how many dates the resolver answered.

    ``expected`` says what the backtest should answer given what the resolver answered; by default the same.
    """
    leg = _leg_with(theirs)
    answered = 0
    for trading_date in _TRADING_DATES:
        listed = _LISTED[shape](trading_date - timedelta(days=3), trading_date + timedelta(days=_HORIZON_DAYS))
        theirs_answer = expiry_a_rule_comes_to(leg, listed, trading_date)
        ours_answer = expiry_for(ours, listed=listed, trading_date=trading_date)

        assert ours_answer == expected(trading_date, listed, theirs_answer), f"{shape}, {trading_date}"
        answered += theirs_answer is not None
    return answered


# Shapes that list nothing a rule can meet, so the resolver answers None on every date and there is nothing
# to compare but that.
_NEVER_MET_BY_THIS_WEEK = ("nothing", "mondays and wednesdays")


def _assert_the_comparison_had_something_to_compare(answered: int, *, nothing_to_meet: bool = False) -> None:
    """A rule that answers None on every date would agree with anything."""
    if nothing_to_meet:
        assert answered == 0
    else:
        assert answered > len(_TRADING_DATES) // 10, f"the resolver answered on only {answered} dates"


def test_the_sweep_covers_the_holiday_weeks_it_is_meant_to() -> None:
    """A Thursday before a holiday Friday, a Friday before a holiday Monday, and a week with a Thursday closed."""
    for day in (date(2025, 4, 17), date(2025, 7, 3), date(2026, 4, 2), date(2025, 5, 23), date(2025, 12, 24)):
        assert day in _SESSION_SET and day in _TRADING_DATES
    for day in (date(2025, 4, 18), date(2025, 7, 4), date(2026, 7, 3), date(2025, 1, 9)):
        assert day not in _SESSION_SET


@pytest.mark.parametrize("shape", list(_LISTED))
def test_same_day_agrees_with_the_resolver_on_every_session(shape: str) -> None:
    answered = _agree_on_every_session(shape, SameDayExpiryRule(kind="same_day"), {"selector": "same_day"})

    _assert_the_comparison_had_something_to_compare(answered, nothing_to_meet=shape == "nothing")


@pytest.mark.parametrize("days", [1, 2, 3, 5, 7, 14, 30])
@pytest.mark.parametrize("shape", list(_LISTED))
def test_at_least_n_days_agrees_with_the_resolver_on_every_session(shape: str, days: int) -> None:
    answered = _agree_on_every_session(
        shape, MinDaysExpiryRule(kind="min_days", days=days), {"selector": "min_dte", "days": days}
    )

    _assert_the_comparison_had_something_to_compare(answered, nothing_to_meet=shape == "nothing")


@pytest.mark.parametrize("shape", list(_LISTED))
def test_this_week_agrees_with_the_resolvers_nearest_weekly_except_on_a_weeks_last_session(shape: str) -> None:
    """The resolver's rule is strictly after the decision's date; the backtest's setup counts today.

    Everywhere else they are the same rule, holiday weeks included. On a week's last session, the backtest
    takes today's expiry when it is listed, and otherwise what the resolver takes.
    """

    def today_counts_when_it_ends_its_week(
        trading_date: date, listed: Sequence[date], theirs: date | None
    ) -> date | None:
        return trading_date if trading_date in _LAST_SESSION_OF_WEEK and trading_date in listed else theirs

    answered = _agree_on_every_session(
        shape,
        ThisWeekExpiryRule(kind="this_week"),
        {"selector": "nearest_weekly"},
        expected=today_counts_when_it_ends_its_week,
    )

    _assert_the_comparison_had_something_to_compare(answered, nothing_to_meet=shape in _NEVER_MET_BY_THIS_WEEK)


# ---- Strike rules --------------------------------------------------------------

_TRADING_DAY = date(2025, 10, 13)
_EXPIRY = date(2025, 10, 17)
_STRIKES = tuple(range(570, 601, 5))
_GENERATION = 7
_MODEL = ModelInputs(rate=0.045, rate_source="test", dividends=(), dividends_source="test: none", tree_steps=101)
_LIMITS = ResolverLimits(max_quote_age_ms=5_000, entry_cutoff_lead_ms=60 * 60_000)


def _at(day: date, hour: int, minute: int, second: int = 0) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=_NY))


def _held(bid: str, ask: str, received_ms: int) -> HeldQuote:
    return HeldQuote(
        bid=QuoteSide(Decimal(bid), received_ms),
        ask=QuoteSide(Decimal(ask), received_ms),
        data_type=QuoteDataType.LIVE,
        generation=_GENERATION,
    )


def _resolver_strike(price: str, strike_rule: dict[str, object]) -> int | None:
    """The strike, in thousandths, that the resolver picks for a call at ``strike_rule``, or None when it refuses."""
    selected_ms = _at(_TRADING_DAY, 10, 31, 2)
    received_ms = selected_ms - 400
    money = Decimal(price)
    listings, watched = [], {}
    for strike in _STRIKES:
        contract = OptionContract.of(root="SPY", expiry_date=_EXPIRY, right="call", strike=strike)
        listings.append(Listing(contract, multiplier=100, shares_delivered=100, exercise="american"))
        watched[contract.occ_symbol] = WatchedContract(_held("3.00", "3.04", received_ms), vendor_delta=None)
    leg = {
        "leg_id": "only",
        "instrument": {"kind": "option", "underlying": "SPY"},
        "position": "long",
        "qty_ratio": 1,
        "right": "call",
        "strike": strike_rule,
        "expiry": {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(_EXPIRY)},
    }
    plan = ActionPlan.model_validate({"on_enter": [leg], "on_exit": [{"kind": "close_leg", "entry_leg_id": "only"}]})
    try:
        resolution = resolve_contracts(
            plan,
            chain=Chain(listings=tuple(listings), watched=watched),
            underlying=UnderlyingObservation(
                pricing_bar_close=money,
                quote=_held(str(money - Decimal("0.01")), str(money + Decimal("0.01")), received_ms),
            ),
            feed=FeedState(generation=_GENERATION, unhealthy=None),
            model=_MODEL,
            limits=_LIMITS,
            decision_bar_close_ms=_at(_TRADING_DAY, 10, 31),
            selection_ms=selected_ms,
        )
    except ContractsNotResolved as refused:
        assert refused.reason is ResolverRefusal.STRIKE_NOT_LISTED
        return None
    return resolution.picked["only"].strike_thousandths


_PRICES = ["585.00", "587.50", "587.49", "587.51", "582.50", "569.00", "650.00", "572.50", "600.00"]
_LADDER = tuple(strike * 1000 for strike in _STRIKES)


@pytest.mark.parametrize("price", _PRICES)
def test_at_the_money_agrees_with_the_resolver_including_the_lower_of_two_as_near(price: str) -> None:
    ours = strike_for(AtTheMoney(kind="atm"), ladder_thousandths=_LADDER, underlying_price=Decimal(price))
    theirs = _resolver_strike(price, {"selector": "atm"})

    assert ours == theirs


@pytest.mark.parametrize("steps", [-9, -3, -1, 1, 2, 3, 7, 9])
@pytest.mark.parametrize("price", _PRICES)
def test_steps_from_the_money_agree_with_the_resolver_including_off_the_end(price: str, steps: int) -> None:
    ours = strike_for(
        StepsFromTheMoney(kind="atm_offset", steps=steps), ladder_thousandths=_LADDER, underlying_price=Decimal(price)
    )
    theirs = _resolver_strike(price, {"selector": "atm_offset", "offset": steps})

    assert ours == theirs
