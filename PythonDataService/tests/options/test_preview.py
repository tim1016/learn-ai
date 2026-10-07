"""The figures a preview shows for a resolution: every leg, the money, and what stops at nothing.

Resolutions come from the resolver's own scenes, with the quotes of the legs
that matter set by hand so that every expected figure can be worked out.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from app.options.american import AmericanIvStatus
from app.options.entry import prepare_entry
from app.options.lean import LeanDirection
from app.options.named_structures import NamedStructure
from app.options.preview import (
    FEE_RATE_NOT_PINNED,
    StructureFigures,
    bid_ask_gap_percent_of_mid,
    percent_of,
    structure_figures,
)
from app.options.requirement import StructureMoneyRefusal, StructureMoneyRefused
from app.options.structure_check import StructureRefusal
from app.schemas.action_plan import ActionPlan

# The resolver's own scenes: SPY with its bar closed at 585 on a dollar-wide ladder.
from tests.options import test_resolver as scenes

_AT_THE_MONEY = {"selector": "atm"}


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
    **markets: tuple[str, str],
) -> StructureFigures:
    return structure_figures(
        plan,
        _resolved(plan, **markets),
        units=units,
        cost_limit_percent=Decimal(cost_limit),
        fee_provision=lambda _structure, _markets, _size: fees,
        model_is_known=model_is_known,
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


def test_a_cost_is_a_percent_of_what_is_set_aside_and_exact() -> None:
    assert percent_of(800, 1_400) == Fraction(400, 7)
    with pytest.raises(ValueError):
        percent_of(800, 0)


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
    assert (bought.leg_id, bought.position, bought.ratio, bought.multiplier) == ("long_call", "long", 1, 100)
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
