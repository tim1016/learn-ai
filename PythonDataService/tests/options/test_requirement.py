"""An option structure's money beyond what golden fixture OSM-001 pins: fees, limits and refused inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.options.contract import OptionContract
from app.options.requirement import (
    LegQuote,
    broker_requirement_cents,
    entry_money,
    net_entry_premium_cents,
    round_trip_cost_cents,
    round_trip_cost_is_over_limit,
)
from app.options.structure import ResolvedLeg, ResolvedStructure

_EXPIRY = date(2026, 12, 18)


def _leg(leg_id: str, position: str, right: str, strike: str, *, ratio: int = 1, root: str = "SPY") -> ResolvedLeg:
    return ResolvedLeg(
        leg_id=leg_id,
        contract=OptionContract.of(root=root, expiry_date=_EXPIRY, right=right, strike=Decimal(strike)),
        position=position,
        ratio=ratio,
    )


def _sold_call_spread() -> ResolvedStructure:
    """A $5-wide call spread: requirement $500.00 a structure."""
    return ResolvedStructure(legs=(_leg("sold", "short", "call", "450"), _leg("bought", "long", "call", "455")))


def test_the_fee_provision_is_set_aside_on_top_of_requirement_and_premium() -> None:
    money = entry_money(_sold_call_spread(), net_entry_premium_cents=-30_700, fee_provision_cents=132, multiplier=100)

    assert money.entry_commitment_cents == 50_000 - 30_700 + 132
    # The provision is cash set aside, not part of what the structure can lose at expiry.
    assert money.loss_at_expiry_cents == 19_300


def test_a_fee_provision_cannot_rescue_prices_that_do_not_belong_together() -> None:
    """A $5-wide spread sold for $5.00 commits nothing before fees; the provision must not make it look admissible."""
    with pytest.raises(ValueError, match="do not belong together"):
        entry_money(_sold_call_spread(), net_entry_premium_cents=-50_000, fee_provision_cents=132, multiplier=100)


@pytest.mark.parametrize(
    ("leg_prices", "reason"),
    [
        pytest.param({"sold": Decimal("5.20")}, "one entry for each of the legs", id="a leg with no price"),
        pytest.param(
            {"sold": Decimal("5.20"), "bought": Decimal("2.13"), "stray": Decimal("1")},
            "one entry for each of the legs",
            id="a price for a leg the structure does not have",
        ),
        pytest.param(
            {"sold": Decimal("5.20456"), "bought": Decimal("2.13")}, "not a whole number of cents", id="a price finer than a cent a contract"
        ),
        pytest.param({"sold": Decimal("-5.20"), "bought": Decimal("2.13")}, "zero or more", id="a negative price"),
        pytest.param({"sold": Decimal("NaN"), "bought": Decimal("2.13")}, "finite", id="a price that is no number"),
    ],
)
def test_a_premium_is_not_worked_out_from_prices_it_cannot_be_exact_about(
    leg_prices: dict[str, Decimal], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        net_entry_premium_cents(_sold_call_spread(), leg_prices, multiplier=100)


def test_a_crossed_quote_has_no_round_trip_cost() -> None:
    quotes = {"sold": LegQuote(Decimal("5.26"), Decimal("5.20")), "bought": LegQuote(Decimal("2.09"), Decimal("2.13"))}

    with pytest.raises(ValueError, match=r"bid 5\.26 above its ask 5\.20"):
        round_trip_cost_cents(_sold_call_spread(), quotes, multiplier=100)


def test_a_requirement_that_is_not_whole_cents_is_refused_not_rounded() -> None:
    """An eighth-dollar width on a one-share contract is 12.5 cents."""
    structure = ResolvedStructure(legs=(_leg("sold", "short", "call", "2.125"), _leg("bought", "long", "call", "2.25")))

    assert broker_requirement_cents(structure, multiplier=100) == 1_250
    with pytest.raises(ValueError, match="not a whole number of cents"):
        broker_requirement_cents(structure, multiplier=1)


@pytest.mark.parametrize(
    ("round_trip", "commitment", "limit_percent", "over"),
    [
        pytest.param(800, 1_400, "10", True, id="$8 of a $14 commitment"),
        pytest.param(140, 1_400, "10", False, id="exactly the limit is not over it"),
        pytest.param(141, 1_400, "10", True, id="a cent past the limit"),
        pytest.param(35, 1_400, "2.5", False, id="a limit with a fraction, met exactly"),
        pytest.param(36, 1_400, "2.5", True, id="a limit with a fraction, passed by a cent"),
        pytest.param(1, 1_400, "0", True, id="a limit of zero admits no cost at all"),
    ],
)
def test_a_round_trip_cost_is_over_the_limit_only_when_it_is_more_than_that_percent_of_the_commitment(
    round_trip: int, commitment: int, limit_percent: str, over: bool
) -> None:
    assert (
        round_trip_cost_is_over_limit(
            round_trip_cost_cents=round_trip, entry_commitment_cents=commitment, limit_percent=Decimal(limit_percent)
        )
        is over
    )


@pytest.mark.parametrize(
    ("legs", "reason"),
    [
        pytest.param(
            (_leg("a", "long", "call", "450"), _leg("b", "short", "call", "455", root="QQQ")),
            "one underlying",
            id="legs on two underlyings, whose payoffs cannot be summed over one price",
        ),
        pytest.param(
            (_leg("a", "long", "call", "450"), _leg("a", "short", "call", "455")),
            "names each leg once",
            id="two legs under one id, whose prices could not be told apart",
        ),
    ],
)
def test_a_structure_its_money_could_not_be_worked_out_for_is_refused(legs: tuple[ResolvedLeg, ...], reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        ResolvedStructure(legs=legs)
