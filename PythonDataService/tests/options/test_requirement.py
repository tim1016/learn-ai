"""An option structure's money beyond what golden fixture OSM-001 pins: fees, limits and refused inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from app.options.contract import OptionContract
from app.options.requirement import (
    EntrySize,
    LegQuote,
    StructureMoneyRefusal,
    StructureMoneyRefused,
    broker_requirement,
    entry_money,
    natural_net_price,
    round_trip_cost,
    round_trip_cost_is_over_limit,
)
from app.options.structure import ResolvedLeg, ResolvedStructure

_EXPIRY = date(2026, 12, 18)
_ONE_STRUCTURE = EntrySize(multiplier=100, units=1)


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


_SOLD_FOR_3_07 = {"sold": LegQuote(Decimal("5.20"), Decimal("5.26")), "bought": LegQuote(Decimal("2.09"), Decimal("2.13"))}


def test_the_fee_provision_is_set_aside_on_top_of_requirement_and_premium() -> None:
    money = entry_money(_sold_call_spread(), _ONE_STRUCTURE, net_price=Decimal("-3.07"), fee_provision_cents=132)

    assert money.entry_commitment_cents == 50_000 - 30_700 + 132
    # The provision is cash set aside, not part of what the structure can lose at expiry.
    assert money.loss_at_expiry_cents == 19_300


def test_a_fee_provision_cannot_rescue_prices_that_do_not_belong_together() -> None:
    """A $5-wide spread sold for $5.00 commits nothing before fees; the provision must not make it look admissible."""
    with pytest.raises(StructureMoneyRefused) as refused:
        entry_money(_sold_call_spread(), _ONE_STRUCTURE, net_price=Decimal("-5.00"), fee_provision_cents=132)

    assert refused.value.reason is StructureMoneyRefusal.COMMITMENT_NOT_POSITIVE


def test_the_price_actually_sent_is_what_is_set_aside_not_the_quoted_one() -> None:
    """Quoted, the spread sells for $3.07. Sent a nickel worse for a coarser price step, it commits $198.00."""
    spread = _sold_call_spread()

    assert natural_net_price(spread, _SOLD_FOR_3_07) == Decimal("-3.07")
    sent = entry_money(spread, _ONE_STRUCTURE, net_price=Decimal("-3.02"), fee_provision_cents=0)
    assert sent.entry_commitment_cents == 19_800


@pytest.mark.parametrize(
    ("net_price", "reason"),
    [
        pytest.param(Decimal("-3.07456"), "whole number of cents", id="a price finer than a cent a contract"),
        pytest.param(Decimal("NaN"), "finite", id="a price that is no number"),
        pytest.param(Decimal("-1E+5000000"), "everyday number", id="an exponent that would take seconds to write out"),
        pytest.param(-3.07, "exact decimal", id="a float, which has already lost the cents"),
    ],
)
def test_money_is_not_worked_out_from_a_price_it_cannot_be_exact_about(net_price: object, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        entry_money(_sold_call_spread(), _ONE_STRUCTURE, net_price=net_price, fee_provision_cents=0)  # type: ignore[arg-type]


@pytest.mark.parametrize("fee_provision", [-1, 1.5, True], ids=["negative", "a fraction of a cent", "a boolean"])
def test_a_fee_provision_is_a_whole_number_of_cents_zero_or_more(fee_provision: object) -> None:
    with pytest.raises(ValueError, match="fee provision"):
        entry_money(_sold_call_spread(), _ONE_STRUCTURE, net_price=Decimal("-3.07"), fee_provision_cents=fee_provision)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("multiplier", "units"),
    [(0, 1), (100, 0), (100.0, 1), (100, 2.5), (True, 1)],
    ids=["no shares a contract", "no structures", "a float multiplier", "half a structure", "a boolean multiplier"],
)
def test_an_entry_size_is_whole_shares_a_contract_and_whole_structures(multiplier: object, units: object) -> None:
    """Every figure is whole cents because these are whole numbers: 100.0 would make a requirement of 50000.0."""
    with pytest.raises(ValueError, match="whole number, one or more"):
        EntrySize(multiplier=multiplier, units=units)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("quotes", "reason"),
    [
        pytest.param({"sold": _SOLD_FOR_3_07["sold"]}, "one entry for each of the legs", id="a leg with no quote"),
        pytest.param(
            _SOLD_FOR_3_07 | {"stray": LegQuote(Decimal("1"), Decimal("1.02"))},
            "one entry for each of the legs",
            id="a quote for a leg the structure does not have",
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"sold": LegQuote(Decimal("5.26"), Decimal("5.20"))}, "no market to cross", id="a bid above its ask"
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"sold": LegQuote(Decimal("-0.01"), Decimal("5.20"))}, "no market to cross", id="a negative bid"
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"sold": LegQuote(Decimal("NaN"), Decimal("5.20"))}, "finite", id="a bid that is no number"
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"sold": LegQuote(Decimal("sNaN"), Decimal("5.20"))}, "finite", id="a signalling not-a-number"
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"bought": LegQuote(Decimal("2.09"), Decimal("Infinity"))}, "finite", id="an infinite ask"
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"sold": LegQuote(Decimal("1E+5000000"), Decimal("1E+5000001"))},
            "everyday number",
            id="a quote that would take seconds to write out",
        ),
        pytest.param(
            _SOLD_FOR_3_07 | {"bought": LegQuote(Decimal("0"), Decimal("0"))}, "no market to cross", id="an ask of nothing"
        ),
        pytest.param(_SOLD_FOR_3_07 | {"sold": LegQuote(5.20, 5.26)}, "exact decimal", id="floats, not decimals"),  # type: ignore[arg-type]
    ],
)
def test_neither_a_cost_nor_a_net_price_is_worked_out_from_quotes_that_are_not_a_market(
    quotes: dict[str, LegQuote], reason: str
) -> None:
    """Both read the same quotes, so both refuse the same ones, each as a ``ValueError``."""
    with pytest.raises(ValueError, match=reason):
        round_trip_cost(_sold_call_spread(), quotes, _ONE_STRUCTURE)
    with pytest.raises(ValueError, match=reason):
        natural_net_price(_sold_call_spread(), quotes)


def test_a_bid_of_zero_is_a_market() -> None:
    """A far out-of-the-money option is often bid at nothing and offered at a nickel; it can still be bought."""
    quotes = _SOLD_FOR_3_07 | {"bought": LegQuote(Decimal("0"), Decimal("0.05"))}

    assert round_trip_cost(_sold_call_spread(), quotes, _ONE_STRUCTURE) == 600 + 500
    assert natural_net_price(_sold_call_spread(), quotes) == Decimal("-5.15")


def test_a_net_price_is_exact_whatever_decimal_context_the_caller_is_in() -> None:
    """A quote one part in 10^31 too fine is kept, not rounded to a tidy price, so the entry is refused;
    and a caller working at three digits still gets every digit of an ordinary net price."""
    too_fine = _SOLD_FOR_3_07 | {"sold": LegQuote(Decimal("5.2000000000000000000000000000001"), Decimal("5.26"))}
    net_price = natural_net_price(_sold_call_spread(), too_fine)

    assert net_price != Decimal("-3.07")
    with pytest.raises(ValueError, match="whole number of cents"):
        entry_money(_sold_call_spread(), _ONE_STRUCTURE, net_price=net_price, fee_provision_cents=0)
    with localcontext() as three_digits:
        three_digits.prec = 3
        assert natural_net_price(_sold_call_spread(), _SOLD_FOR_3_07) == Decimal("-3.07")


def test_a_requirement_that_is_not_whole_cents_is_refused_not_rounded() -> None:
    """An eighth-dollar width on a one-share contract is 12.5 cents."""
    structure = ResolvedStructure(legs=(_leg("sold", "short", "call", "2.125"), _leg("bought", "long", "call", "2.25")))

    assert broker_requirement(structure, _ONE_STRUCTURE) == 1_250
    with pytest.raises(ValueError, match="not a whole number of cents"):
        broker_requirement(structure, EntrySize(multiplier=1, units=1))


def test_a_structure_that_cannot_lose_at_expiry_has_no_requirement_whatever_its_strikes() -> None:
    """Bought legs only: the requirement is zero, and no fraction of a cent in the payoff is in the way of saying so."""
    bought = ResolvedStructure(legs=(_leg("call", "long", "call", "2"), _leg("put", "long", "put", "2.125")))

    assert broker_requirement(bought, EntrySize(multiplier=1, units=1)) == 0


@pytest.mark.parametrize(
    ("round_trip", "commitment", "limit_percent", "over"),
    [
        pytest.param(800, 1_400, "10", True, id="$8 of a $14 commitment"),
        pytest.param(140, 1_400, "10", False, id="exactly the limit is not over it"),
        pytest.param(141, 1_400, "10", True, id="a cent past the limit"),
        pytest.param(35, 1_400, "2.5", False, id="a limit with a fraction, met exactly"),
        pytest.param(36, 1_400, "2.5", True, id="a limit with a fraction, passed by a cent"),
        pytest.param(1, 1_400, "0", True, id="a limit of zero admits no cost at all"),
        pytest.param(0, 1_400, "0", False, id="no cost is within any limit"),
    ],
)
def test_a_round_trip_cost_is_over_the_limit_only_when_it_is_more_than_that_percent_of_the_commitment(
    round_trip: int, commitment: int, limit_percent: str, over: bool
) -> None:
    assert (
        round_trip_cost_is_over_limit(
            cost_cents=round_trip, commitment_cents=commitment, limit_percent=Decimal(limit_percent)
        )
        is over
    )


@pytest.mark.parametrize(
    ("round_trip", "commitment", "limit_percent", "reason"),
    [
        pytest.param(0, 0, "10", "commitment is above zero", id="a commitment of nothing, which an entry never has"),
        pytest.param(0, -500, "10", "commitment is above zero", id="a negative commitment"),
        pytest.param(-1, 1_400, "10", "cost is zero or more", id="a negative cost"),
        pytest.param(100, 1_400, "-1", "zero or more", id="a negative limit"),
        pytest.param(100, 1_400, "NaN", "finite", id="a limit that is no number"),
        pytest.param(100, 1_400, "1E-5000000", "everyday number", id="a limit that would take seconds to write out"),
        pytest.param(35.5, 1_400, "10", "whole number of cents", id="a cost with a fraction of a cent"),
        pytest.param(100, True, "10", "whole number of cents", id="a boolean commitment"),
    ],
)
def test_a_cost_is_not_judged_against_figures_that_cannot_be_an_entrys(
    round_trip: object, commitment: object, limit_percent: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        round_trip_cost_is_over_limit(
            cost_cents=round_trip,  # type: ignore[arg-type]
            commitment_cents=commitment,  # type: ignore[arg-type]
            limit_percent=Decimal(limit_percent),
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
        pytest.param(
            tuple(_leg(f"leg_{index}", "long", "call", str(440 + 5 * index)) for index in range(5)),
            "at most 4",
            id="five legs, more than one order carries",
        ),
    ],
)
def test_a_structure_its_money_could_not_be_worked_out_for_is_refused(legs: tuple[ResolvedLeg, ...], reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        ResolvedStructure(legs=legs)
