"""The figures a preview shows for a resolution: every leg, the money, and what stops at nothing.

Resolutions come from the resolver's own scenes, with the quotes of the legs
that matter set by hand so that every expected figure can be worked out.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.options.american import AmericanIvStatus
from app.options.deadlines import CloseKind, CloseTerms
from app.options.decay import DecayNotEstimated, LegDecay, picks_daily_decay
from app.options.entry import prepare_entry
from app.options.lean import LeanDirection
from app.options.named_structures import NamedStructure
from app.options.preview import (
    FEE_RATE_NOT_PINNED,
    StructureFigures,
    TooLateToEnter,
    bid_ask_gap_percent_of_mid,
    structure_figures,
)
from app.options.requirement import StructureMoneyRefusal, StructureMoneyRefused
from app.options.resolver import DividendOn, ResolverRefusal
from app.options.structure_check import StructureRefusal
from app.schemas.action_plan import ActionPlan

# The resolver's own scenes: SPY with its bar closed at 585 on a dollar-wide ladder.
from tests.options import test_resolver as scenes

_AT_THE_MONEY = {"selector": "atm"}
# When the scene's contracts are picked: a Tuesday at 10:31:02, hours before any entry cutoff.
_PICKED_AT = scenes._at(scenes._TUESDAY, 10, 31, 2)
_TERMS = CloseTerms()


def _spread(*, sold_from_bought: float = 5.0, name: NamedStructure | None = None) -> ActionPlan:
    """The 585 call bought, and a call sold ``sold_from_bought`` dollars from it."""
    plan = scenes._plan(
        scenes._leg("long_call", "call", _AT_THE_MONEY),
        scenes._leg(
            "short_call", "call", {"selector": "leg_distance", "leg_id": "long_call", "dollars": sold_from_bought}, position="short"
        ),
    )
    return plan.model_copy(update={"structure": name})


def _resolved(plan: ActionPlan, **markets: tuple[str, str]) -> scenes.resolver.Resolution:
    """``plan`` resolved in the scene with a call's bid and ask set by its strike: ``c585=("4.08", "4.12")``."""
    scene = scenes._scene()
    for named, (bid, ask) in markets.items():
        scene = scene.holding(scenes._contract("call", int(named[1:])), scenes._quote(bid, ask, scene.selection_ms - 400))
    return scene.resolve(plan)


def _figures(
    plan: ActionPlan,
    *,
    fees: int | None = 0,
    units: int = 1,
    model_is_known: bool = True,
    cost_limit: str = "10",
    gap_limit: str = "8",
    with_decay: bool = False,
    at_ms: int = _PICKED_AT,
    terms: CloseTerms = _TERMS,
    **markets: tuple[str, str],
) -> StructureFigures:
    resolution = _resolved(plan, **markets)
    return structure_figures(
        plan,
        resolution,
        daily_decay=picks_daily_decay(resolution) if with_decay else None,
        units=units,
        leg_gap_limit_percent=Decimal(gap_limit),
        cost_limit_percent=Decimal(cost_limit),
        fee_provision=lambda _structure, _markets, _size: fees,
        model_is_known=model_is_known,
        at_ms=at_ms,
        close_terms=terms,
    )


def test_a_gap_is_a_percent_of_the_mid_and_exact() -> None:
    """3.10 by 3.16: six cents on a mid of 3.13 is 600/313 percent, 1.9169 and a bit."""
    assert bid_ask_gap_percent_of_mid(Decimal("3.10"), Decimal("3.16")) == Fraction(600, 313)
    # Nothing bid and two cents asked: the gap is twice the mid.
    assert bid_ask_gap_percent_of_mid(Decimal(0), Decimal("0.02")) == 200
    assert bid_ask_gap_percent_of_mid(Decimal("1.50"), Decimal("1.50")) == 0


@pytest.mark.parametrize(
    ("bid", "ask"),
    [("2.30", "2.22"), ("0", "0"), ("-0.01", "0.02"), ("NaN", "1.00")],
    ids=["crossed", "nothing asked", "a bid below zero", "no number"],
)
def test_a_gap_is_not_read_from_a_market_that_cannot_be_crossed(bid: str, ask: str) -> None:
    with pytest.raises(ValueError, match="market that can be crossed"):
        bid_ask_gap_percent_of_mid(Decimal(bid), Decimal(ask))


def test_a_bought_spread_has_every_figure() -> None:
    """Bought at the 585's ask of 4.12 and sold at the 590's bid of 2.18, with 37 cents of fees."""
    shown = _figures(_spread(name=NamedStructure.BULL_CALL_SPREAD), fees=37, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert shown.not_as_built is None
    assert (shown.net_price, shown.fee_provision_cents) == (Decimal("1.94"), 37)
    assert (shown.money.net_entry_premium_cents, shown.money.broker_requirement_cents) == (19_400, 0)
    assert (shown.money.entry_commitment_cents, shown.money.loss_at_expiry_cents) == (19_437, 19_400)
    assert (shown.round_trip_cost_cents, shown.round_trip_cost_percent) == (800, Fraction(80_000, 19_437))
    assert shown.over_cost_limit is False
    assert (shown.at_expiry.gain_cents, shown.at_expiry.break_evens) == (30_600, (Fraction("586.94"),))
    bought, sold = shown.legs
    assert (bought.leg_id, bought.position, bought.ratio) == ("long_call", "long", 1)
    assert (bought.contract, sold.contract) == (scenes._contract("call", 585), scenes._contract("call", 590))
    assert (bought.gap_percent_of_mid, sold.gap_percent_of_mid) == (Fraction(400, 410), Fraction(400, 220))
    assert all(leg.priced.status is AmericanIvStatus.SOLVED for leg in shown.legs)
    # The bought call is nearer the money than the sold one, so its delta is the larger.
    assert shown.lean.net_delta == pytest.approx(bought.priced.delta - sold.priced.delta, abs=1e-12, rel=0)
    assert shown.lean.direction is LeanDirection.UP


def test_a_cost_over_the_limit_is_said_and_stops_nothing() -> None:
    """Mids of $5.00 and $4.90, four cents wide each: $8.00 of a $14.00 commitment. An entry refuses this; a
    preview shows it."""
    shown = _figures(_spread(), c585=("4.98", "5.02"), c590=("4.88", "4.92"))

    assert shown.not_as_built is None
    assert (shown.money.entry_commitment_cents, shown.round_trip_cost_cents) == (1_400, 800)
    assert (shown.round_trip_cost_percent, shown.over_cost_limit) == (Fraction(400, 7), True)
    assert shown.at_expiry.gain_cents == 48_600


@pytest.mark.parametrize(
    ("cost_limit", "over"),
    [
        pytest.param("4.11", True, id="a limit a hair under the cost"),
        pytest.param("4.12", False, id="a limit a hair over it"),
        pytest.param("50", False, id="a loose limit"),
        pytest.param("0", True, id="no cost allowed at all"),
    ],
)
def test_a_preview_judges_the_cost_by_the_owners_own_limit_and_shows_what_an_entry_would_prepare(
    cost_limit: str, over: bool
) -> None:
    """Two structures: $16.00 to cross both markets, of the $388.37 set aside, which is 4.1198%. The entry's own
    preparation is asked the same question of the same picks. Where it refuses, the preview says over; where it
    prepares, their figures agree."""
    plan = _spread(name=NamedStructure.BULL_CALL_SPREAD)
    markets = {"c585": ("4.08", "4.12"), "c590": ("2.18", "2.22")}
    resolution = _resolved(plan, **markets)

    shown = _figures(plan, fees=37, units=2, cost_limit=cost_limit, **markets)

    def prepared():
        return prepare_entry(
            plan,
            picked=resolution.picked,
            multipliers=resolution.multipliers,
            quotes=resolution.quotes,
            units=2,
            fee_provision_cents=37,
            cost_limit_percent=Decimal(cost_limit),
            # As wide as a gap can be: the entry's own gap limit is not what this test asks about.
            leg_gap_limit_percent=Decimal(200),
        )

    assert shown.over_cost_limit is over
    if over:
        with pytest.raises(StructureMoneyRefused) as refused:
            prepared()
        assert refused.value.reason is StructureMoneyRefusal.COST_OVER_LIMIT
    else:
        entry = prepared()
        assert (shown.size, shown.net_price, shown.money, shown.round_trip_cost_cents) == (
            entry.size, entry.net_limit_price, entry.money, entry.round_trip_cost_cents,
        )
    # Whatever the limit, what it would cost is shown.
    assert (shown.round_trip_cost_cents, shown.money.entry_commitment_cents) == (1_600, 38_837)


def test_picks_that_are_not_the_shape_named_keep_their_figures_and_name_the_rule() -> None:
    """The sold call five dollars below the bought one: a spread that pays, under a name that means one paid for."""
    shown = _figures(_spread(sold_from_bought=-5.0, name=NamedStructure.BULL_CALL_SPREAD))

    assert shown.not_as_built.reason == StructureRefusal.STRIKES_NOT_AS_NAMED
    assert set(shown.not_as_built.leg_ids) == {"long_call", "short_call"}
    assert shown.money.broker_requirement_cents == 50_000
    assert shown.net_price < 0


def test_the_same_picks_under_no_name_are_as_built() -> None:
    assert _figures(_spread(sold_from_bought=-5.0)).not_as_built is None


def test_two_legs_on_one_contract_show_their_legs_and_no_money() -> None:
    shown = _figures(_spread(sold_from_bought=0.0))

    assert shown.not_as_built.reason == StructureRefusal.SAME_CONTRACT
    assert [leg.contract for leg in shown.legs] == [scenes._contract("call", 585)] * 2
    assert (shown.size, shown.net_price, shown.money, shown.round_trip_cost_cents, shown.at_expiry, shown.lean) == (
        None, None, None, None, None, None,
    )


def test_prices_that_do_not_belong_together_are_named_by_the_moneys_own_refusal() -> None:
    """A spread bought for nothing: the 585's ask is the 590's bid."""
    shown = _figures(_spread(), c585=("2.10", "2.18"), c590=("2.18", "2.22"))

    assert shown.not_as_built.reason == StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE
    assert (shown.net_price, shown.money, shown.at_expiry, shown.over_cost_limit) == (Decimal(0), None, None, None)
    # What it would cost to cross both markets is still known.
    assert shown.round_trip_cost_cents == 1_200


def test_with_no_fee_rate_pinned_nothing_is_set_aside_and_that_is_the_reason() -> None:
    shown = _figures(_spread(), fees=None, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert shown.not_as_built.reason == FEE_RATE_NOT_PINNED
    assert (shown.fee_provision_cents, shown.money) == (None, None)
    assert shown.net_price == Decimal("1.94")


def test_a_shape_that_fails_is_named_ahead_of_money_that_fails() -> None:
    """Named a bull call spread, bought for nothing: it is not paid for, and its money is refused too."""
    shown = _figures(_spread(name=NamedStructure.BULL_CALL_SPREAD), c585=("2.10", "2.18"), c590=("2.18", "2.22"))

    assert shown.not_as_built.reason == StructureRefusal.PREMIUM_NOT_PAID
    assert shown.money is None


def test_a_shape_that_fails_is_named_ahead_of_a_day_with_no_fee_rate_and_both_are_told() -> None:
    """The sold call below the bought one, under a name that means the other way, on a day with no pinned rate."""
    shown = _figures(_spread(sold_from_bought=-5.0, name=NamedStructure.BULL_CALL_SPREAD), fees=None)

    assert shown.not_as_built.reason == StructureRefusal.STRIKES_NOT_AS_NAMED
    assert [refusal.reason for refusal in shown.entry_refusals] == [StructureRefusal.STRIKES_NOT_AS_NAMED, FEE_RATE_NOT_PINNED]
    assert shown.money is None


def test_a_wide_leg_is_told_ahead_of_a_day_with_no_fee_rate_as_an_entry_would_ask_them() -> None:
    """Forty cents on a mid of 4.10 is 9.76%, over 8%. The gap is asked before anything is set aside."""
    shown = _figures(_spread(), fees=None, c585=("3.90", "4.30"), c590=("2.18", "2.22"))

    assert [refusal.reason for refusal in shown.entry_refusals] == [
        StructureMoneyRefusal.LEG_GAP_OVER_LIMIT, FEE_RATE_NOT_PINNED,
    ]
    # A wide market passes by itself. No rate pinned is why nothing can be set aside.
    assert shown.not_as_built.reason == FEE_RATE_NOT_PINNED
    assert [leg.over_gap_limit for leg in shown.legs] == [True, False]


# ---- Too late for an entry to be seen through --------------------------------------------------

# A sold leg is closed 10 minutes before Tuesday's 16:00 close. An entry needs its 20 second wait, 30 seconds
# to prove a cancel and 30 of margin before that: none is opened from 15:48:40.
_SOLD_LEG_CUTOFF = scenes._at(scenes._TUESDAY, 15, 48, 40)
_MARKETS = {"c585": ("4.08", "4.12"), "c590": ("2.18", "2.22")}


def test_a_structure_with_a_sold_leg_is_too_late_to_enter_from_its_cutoff_on_and_keeps_every_figure() -> None:
    """The spread expires on Friday. It owes a close on Tuesday all the same, for its sold leg."""
    in_time = _figures(_spread(), at_ms=_SOLD_LEG_CUTOFF - 1, **_MARKETS)
    too_late = _figures(_spread(), at_ms=_SOLD_LEG_CUTOFF, **_MARKETS)

    assert (in_time.too_late_to_enter, in_time.entry_refusals) == (None, ())
    assert too_late.too_late_to_enter == TooLateToEnter(CloseKind.SOLD_LEG, close_lead_ms=10 * 60_000)
    (refusal,) = too_late.entry_refusals
    assert (refusal.reason, refusal.leg_ids) == (ResolverRefusal.PAST_ENTRY_CUTOFF, ())
    # The time passes by itself: the picks are still the structure built, and every figure stands.
    assert too_late.not_as_built is None
    assert (too_late.money, too_late.net_price, too_late.at_expiry) == (in_time.money, in_time.net_price, in_time.at_expiry)
    assert too_late.money.net_entry_premium_cents == 19_400


def test_the_sold_legs_close_is_counted_back_by_the_terms_handed_in() -> None:
    """Closed an hour before the close, with a wait of 5 seconds: no entry from 14:58:55."""
    terms = replace(_TERMS, sold_leg_close_lead_ms=60 * 60_000, entry_wait_ms=5_000)
    cutoff = scenes._at(scenes._TUESDAY, 14, 58, 55)

    assert _figures(_spread(), at_ms=cutoff - 1, terms=terms, **_MARKETS).too_late_to_enter is None
    assert _figures(_spread(), at_ms=cutoff, terms=terms, **_MARKETS).too_late_to_enter == TooLateToEnter(
        CloseKind.SOLD_LEG, close_lead_ms=60 * 60_000
    )


def test_a_bought_contract_owes_a_close_only_on_its_expiry_day() -> None:
    """A call bought for Friday. On Tuesday nothing is owed, however late. On Friday it is closed 45 minutes
    before the close, and no entry is opened from 15:13:40."""
    bought = scenes._one("call", _AT_THE_MONEY)
    on_its_expiry_day = scenes._at(scenes._FRIDAY, 15, 13, 40)

    assert _figures(bought, at_ms=scenes._at(scenes._TUESDAY, 15, 59, 59)).entry_refusals == ()
    assert _figures(bought, at_ms=on_its_expiry_day - 1).too_late_to_enter is None
    late = _figures(bought, at_ms=on_its_expiry_day)
    assert late.too_late_to_enter == TooLateToEnter(CloseKind.EXPIRY_DAY, close_lead_ms=45 * 60_000)
    assert [refusal.reason for refusal in late.entry_refusals] == [ResolverRefusal.PAST_ENTRY_CUTOFF]


def test_the_day_a_close_is_owed_on_is_new_yorks_own() -> None:
    """Tuesday at 20:30 in New York is Wednesday already by the UTC clock. It is Tuesday's close the entry is
    too late for. Before Wednesday's open there is time again."""
    that_evening = _figures(_spread(), at_ms=scenes._at(scenes._TUESDAY, 20, 30), **_MARKETS)
    next_morning = _figures(_spread(), at_ms=scenes._at(scenes._WEDNESDAY, 8, 0), **_MARKETS)

    assert that_evening.too_late_to_enter == TooLateToEnter(CloseKind.SOLD_LEG, close_lead_ms=10 * 60_000)
    assert (next_morning.too_late_to_enter, next_morning.entry_refusals) == (None, ())


def test_too_late_is_told_last_as_the_clerk_asks_it_after_the_entrys_own_questions() -> None:
    """A wide leg and a cost over its limit, then a wide leg on a day with no fee rate, each past the cutoff."""
    wide = {"c585": ("3.90", "4.30"), "c590": ("2.18", "2.22")}

    costly = _figures(_spread(), at_ms=_SOLD_LEG_CUTOFF, **wide)
    no_rate = _figures(_spread(), fees=None, at_ms=_SOLD_LEG_CUTOFF, **wide)

    assert [refusal.reason for refusal in costly.entry_refusals] == [
        StructureMoneyRefusal.LEG_GAP_OVER_LIMIT, StructureMoneyRefusal.COST_OVER_LIMIT, ResolverRefusal.PAST_ENTRY_CUTOFF,
    ]
    # Each of the three passes by itself: none says the picks are another structure.
    assert costly.not_as_built is None
    assert [refusal.reason for refusal in no_rate.entry_refusals] == [
        StructureMoneyRefusal.LEG_GAP_OVER_LIMIT, FEE_RATE_NOT_PINNED, ResolverRefusal.PAST_ENTRY_CUTOFF,
    ]
    assert no_rate.not_as_built.reason == FEE_RATE_NOT_PINNED


def test_on_a_day_with_no_session_no_close_is_owed_and_none_is_asked() -> None:
    """A Saturday at noon. The calendar has no close to count back from, and that is no error."""
    shown = _figures(_spread(), at_ms=scenes._at(date(2026, 10, 10), 12, 0), **_MARKETS)

    assert (shown.too_late_to_enter, shown.entry_refusals) == (None, ())


def test_inputs_that_are_not_known_show_no_delta_and_no_lean() -> None:
    shown = _figures(_spread(), model_is_known=False, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert [leg.priced for leg in shown.legs] == [None, None]
    assert (shown.lean, shown.lean_unknown_leg_id) == (None, "long_call")
    assert shown.money.net_entry_premium_cents == 19_400


def test_a_pick_the_model_cannot_price_has_no_delta_and_the_structure_no_lean() -> None:
    """The 590 call quoted far above anything a volatility explains. A rule by strike still picks it."""
    shown = _figures(_spread(), c585=("4.08", "4.12"), c590=("400.00", "400.04"))

    bought, sold = shown.legs
    assert bought.priced.status is AmericanIvStatus.SOLVED
    assert (sold.priced.status is AmericanIvStatus.SOLVED, sold.priced.delta) == (False, None)
    assert (shown.lean, shown.lean_unknown_leg_id) == (None, "short_call")


def test_every_structure_asked_for_is_counted() -> None:
    shown = _figures(_spread(), units=3, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (shown.size.units, shown.money.net_entry_premium_cents, shown.round_trip_cost_cents) == (3, 58_200, 2_400)
    assert shown.at_expiry.gain_cents == 91_800
    assert shown.lean.shares == pytest.approx(3 * shown.lean.shares_per_structure, abs=1e-12, rel=0)



# ---- What a day takes -------------------------------------------------------------------------


def test_the_structures_decay_is_its_legs_own_bought_less_sold_for_every_structure_asked() -> None:
    markets = {"c585": ("4.08", "4.12"), "c590": ("2.18", "2.22")}
    shown = _figures(_spread(), units=3, with_decay=True, **markets)

    # Each pick's own decay, worked out apart from the same resolution: the bought call's, then the sold call's.
    bought, sold = (leg.worked.decay for leg in picks_daily_decay(_resolved(_spread(), **markets)).values())
    assert bought < 0 and sold < 0
    assert shown.decay.per_share == pytest.approx(bought - sold, abs=1e-12, rel=0)
    assert shown.decay.per_structure == pytest.approx(100 * (bought - sold), abs=1e-9, rel=0)
    assert shown.decay.total == pytest.approx(300 * (bought - sold), abs=1e-9, rel=0)
    # The call nearer the money loses more in a day, so the spread that bought it loses.
    assert shown.decay.total < 0


def test_a_leg_with_no_decay_leaves_the_structure_with_none() -> None:
    """The 590 call quoted far above anything a volatility explains: no volatility, so no decay for it."""
    shown = _figures(_spread(), with_decay=True, c585=("4.08", "4.12"), c590=("400.00", "400.04"))

    assert (shown.decay.estimated, shown.decay.why_not, shown.decay.legs_without) == (
        False, DecayNotEstimated.NO_VOLATILITY, ("short_call",),
    )
    assert (shown.decay.per_share, shown.decay.total) == (None, None)


def test_inputs_that_are_not_known_show_no_decay_even_when_one_was_worked_out() -> None:
    """A decay priced on dividends nobody could look up is not shown, as no delta is."""
    shown = _figures(_spread(), model_is_known=False, with_decay=True, c585=("4.08", "4.12"), c590=("2.18", "2.22"))

    assert (shown.decay.estimated, shown.decay.why_not) == (False, DecayNotEstimated.INPUTS_NOT_KNOWN)
    assert shown.decay.legs_without == ("long_call", "short_call")
    assert (shown.decay.per_share, shown.decay.total) == (None, None)


def test_figures_nobody_asked_a_decay_for_carry_none_and_picks_that_are_no_structure_have_none_either() -> None:
    assert _figures(_spread(), c585=("4.08", "4.12"), c590=("2.18", "2.22")).decay is None
    assert _figures(_spread(sold_from_bought=0.0), with_decay=True).decay is None


def test_a_decay_handed_in_for_the_wrong_legs_is_the_callers_mistake() -> None:
    plan = _spread()
    resolution = _resolved(plan)
    one_leg_only = {"long_call": LegDecay(None, DecayNotEstimated.NOT_PRICED)}

    with pytest.raises(ValueError, match="one decay for each of the legs"):
        structure_figures(
            plan, resolution, units=1, leg_gap_limit_percent=Decimal(8), cost_limit_percent=Decimal(10),
            fee_provision=lambda *_: 0, model_is_known=True, at_ms=_PICKED_AT, close_terms=_TERMS,
            daily_decay=one_leg_only,
        )


@pytest.mark.parametrize(
    ("sold_strike", "named"),
    [
        pytest.param(585.0, ("sold",), id="sold at 585 with the stock at 585.11: in the money by eleven cents"),
        pytest.param(586.0, (), id="sold at 586: out of the money"),
    ],
)
def test_a_sold_call_is_named_at_an_ex_date_by_the_price_the_picks_were_priced_against(
    sold_strike: float, named: tuple[str, ...]
) -> None:
    """$1.75 goes ex at tomorrow's open, inside the day. The picks are priced against SPY's mid of 585.11, and
    that is the price a sold call is in the money by."""
    going_ex = (DividendOn(ex_ms=session_open_ms_utc(scenes._WEDNESDAY), cash=1.75),)
    plan = scenes._plan(
        scenes._leg("sold", "call", {"selector": "absolute", "strike": sold_strike}, position="short"),
        scenes._leg("bought", "call", {"selector": "absolute", "strike": 590.0}),
    )
    resolution = scenes._scene(model=replace(scenes._MODEL, dividends=going_ex)).resolve(plan)

    shown = structure_figures(
        plan, resolution, units=1, leg_gap_limit_percent=Decimal(8), cost_limit_percent=Decimal(10),
        fee_provision=lambda *_: 0, model_is_known=True, at_ms=_PICKED_AT, close_terms=_TERMS,
        daily_decay=picks_daily_decay(resolution),
    )

    assert resolution.evidence.model_spot == 585.11
    assert shown.decay.estimated and shown.decay.sold_calls_in_the_money_at_an_ex_date == named
