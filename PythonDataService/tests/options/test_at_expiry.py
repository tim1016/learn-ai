"""A structure's figures at expiry: the most it gains, where its results hold, and where it breaks even.

Every expected value is worked out by hand from the exercise payoff, in the
case's own docstring or comment.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from fractions import Fraction

import pytest

from app.options.at_expiry import PriceRange, ResultAt, at_expiry
from app.options.contract import OptionContract
from app.options.requirement import EntrySize, StructureMoneyRefusal, StructureMoneyRefused, entry_money
from app.options.structure import ResolvedLeg, ResolvedStructure

_FRIDAY = date(2026, 10, 9)
_ONE = EntrySize(multiplier=100, units=1)


def _leg(leg_id: str, position: str, right: str, strike: int, *, ratio: int = 1, expiry: date = _FRIDAY) -> ResolvedLeg:
    return ResolvedLeg(
        leg_id=leg_id,
        contract=OptionContract.of(root="SPY", expiry_date=expiry, right=right, strike=strike),
        position=position,
        ratio=ratio,
    )


def _of(*legs: ResolvedLeg) -> ResolvedStructure:
    return ResolvedStructure(legs=legs)


def _between(low: str, high: str | None) -> PriceRange:
    return PriceRange(Decimal(low), None if high is None else Decimal(high))


def test_a_call_spread_bought_for_1_94_loses_up_to_194_and_gains_up_to_306() -> None:
    """The 585 call bought and the 590 sold. Worth nothing at 585 or below and $5.00 at 590 or above, so it
    loses the $1.94 paid below 585, gains $5.00 - $1.94 = $3.06 above 590, and is even at 585 + 1.94."""
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    money = entry_money(spread, _ONE, net_price=Decimal("1.94"), fee_provision_cents=0)
    figures = at_expiry(spread, _ONE, net_price=Decimal("1.94"))

    assert (money.loss_at_expiry_cents, figures.gain_cents) == (19_400, 30_600)
    assert figures.loss_holds == (_between("0", "585"),)
    assert figures.gain_holds == (_between("590", None),)
    assert figures.break_evens == (Fraction("586.94"),)


def test_a_call_spread_sold_for_3_07_gains_what_it_received_and_loses_the_width_less_that() -> None:
    """The 585 call sold and the 590 bought, $5 wide. It keeps the $3.07 at 585 or below, owes $5.00 at 590 or
    above for a loss of $1.93, and is even at 585 + 3.07. The requirement is the $500.00, the commitment $193.00."""
    spread = _of(_leg("sold", "short", "call", 585), _leg("bought", "long", "call", 590))

    money = entry_money(spread, _ONE, net_price=Decimal("-3.07"), fee_provision_cents=0)
    figures = at_expiry(spread, _ONE, net_price=Decimal("-3.07"))

    assert (money.broker_requirement_cents, money.entry_commitment_cents) == (50_000, 19_300)
    assert (money.loss_at_expiry_cents, figures.gain_cents) == (19_300, 30_700)
    assert figures.gain_holds == (_between("0", "585"),)
    assert figures.loss_holds == (_between("590", None),)
    assert figures.break_evens == (Fraction("588.07"),)


def test_a_bought_call_gains_without_limit_and_a_bought_put_gains_most_at_zero() -> None:
    """A 585 call for $4.12 is even at 589.12 and keeps gaining. A 585 put for $3.90 is worth $585.00 with
    the stock at zero, a gain of $581.10 a share, and is even at 585 - 3.90."""
    call = at_expiry(_of(_leg("only", "long", "call", 585)), _ONE, net_price=Decimal("4.12"))
    put = at_expiry(_of(_leg("only", "long", "put", 585)), _ONE, net_price=Decimal("3.90"))

    assert (call.gain_cents, call.gain_holds) == (None, ())
    assert (call.loss_holds, call.break_evens) == ((_between("0", "585"),), (Fraction("589.12"),))
    assert (put.gain_cents, put.gain_holds) == (5_811_000, (_between("0", "0"),))
    assert (put.loss_holds, put.break_evens) == ((_between("585", None),), (Fraction("581.10"),))


def test_a_straddle_is_at_its_lowest_at_one_price_and_breaks_even_either_side_of_it() -> None:
    """A 585 call and a 585 put for $8.00 together: worth nothing only at 585, even at 577 and at 593."""
    straddle = _of(_leg("call", "long", "call", 585), _leg("put", "long", "put", 585))

    figures = at_expiry(straddle, _ONE, net_price=Decimal("8.00"))

    assert figures.gain_cents is None
    assert figures.loss_holds == (_between("585", "585"),)
    assert figures.break_evens == (Fraction(577), Fraction(593))


def test_an_iron_condor_keeps_what_it_received_between_its_sold_strikes_and_loses_most_outside_its_wings() -> None:
    """Puts 570 bought and 575 sold, calls 595 sold and 600 bought, $1.50 received. It owes nothing from 575
    to 595, and $5.00 at 570 or below and at 600 or above. Even where it owes $1.50: 573.50 and 596.50."""
    condor = _of(
        _leg("long_put", "long", "put", 570),
        _leg("short_put", "short", "put", 575),
        _leg("short_call", "short", "call", 595),
        _leg("long_call", "long", "call", 600),
    )

    money = entry_money(condor, _ONE, net_price=Decimal("-1.50"), fee_provision_cents=0)
    figures = at_expiry(condor, _ONE, net_price=Decimal("-1.50"))

    assert (money.loss_at_expiry_cents, figures.gain_cents) == (35_000, 15_000)
    assert figures.gain_holds == (_between("575", "595"),)
    assert figures.loss_holds == (_between("0", "570"), _between("600", None))
    assert figures.break_evens == (Fraction("573.50"), Fraction("596.50"))


def test_a_butterfly_gains_most_at_its_body_and_loses_what_it_cost_outside_its_wings() -> None:
    """Calls 580 and 590 bought, two 585 sold, for $1.20. Worth $5.00 at 585 and nothing outside 580 to 590:
    a gain of $3.80 at 585, and even at 580 + 1.20 and 590 - 1.20."""
    butterfly = _of(
        _leg("lower", "long", "call", 580), _leg("body", "short", "call", 585, ratio=2), _leg("upper", "long", "call", 590)
    )

    figures = at_expiry(butterfly, _ONE, net_price=Decimal("1.20"))

    assert (figures.gain_cents, figures.gain_holds) == (38_000, (_between("585", "585"),))
    assert figures.loss_holds == (_between("0", "580"), _between("590", None))
    assert figures.break_evens == (Fraction("581.20"), Fraction("588.80"))


def test_a_break_even_is_exact_where_no_cent_is() -> None:
    """Three 580 calls bought, two 585 and one 590 sold, for $10.00. Up to 585 it gains $3 for each $1 the
    stock rises above 580, so it is even at 580 + 10/3. It is worth $20.00 from 590 up: a gain of $10.00."""
    ratio = _of(
        _leg("bought", "long", "call", 580, ratio=3), _leg("near", "short", "call", 585, ratio=2), _leg("far", "short", "call", 590)
    )

    figures = at_expiry(ratio, _ONE, net_price=Decimal("10.00"))

    assert figures.break_evens == (Fraction(1750, 3),)
    assert (figures.gain_cents, figures.gain_holds) == (100_000, (_between("590", None),))


def test_two_bought_calls_break_even_above_the_higher_strike_where_they_gain_two_for_one() -> None:
    """Calls bought at 580 and at 590 for $12.00 together. At 590 they are worth $10.00, still $2.00 short. From
    there they gain $2 for each $1 the stock rises, so they are even $1 higher, at 591, and keep gaining."""
    two_calls = _of(_leg("lower", "long", "call", 580), _leg("upper", "long", "call", 590))

    figures = at_expiry(two_calls, _ONE, net_price=Decimal("12.00"))

    assert figures.break_evens == (Fraction(591),)
    assert (figures.gain_cents, figures.gain_holds) == (None, ())
    assert figures.loss_holds == (_between("0", "580"),)


def test_a_contract_of_ten_shares_gains_and_loses_a_tenth_of_what_one_of_a_hundred_does() -> None:
    """The $1.94 spread gains $3.06 a share and loses $1.94: $30.60 and $19.40 on ten shares a contract. Its
    break-even is a price of the stock, and the same."""
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))
    of_ten_shares = EntrySize(multiplier=10, units=1)

    money = entry_money(spread, of_ten_shares, net_price=Decimal("1.94"), fee_provision_cents=0)
    figures = at_expiry(spread, of_ten_shares, net_price=Decimal("1.94"))

    assert (money.loss_at_expiry_cents, figures.gain_cents) == (1_940, 3_060)
    assert figures.break_evens == (Fraction("586.94"),)


def test_a_put_structure_can_be_at_its_lowest_only_with_the_stock_at_zero() -> None:
    """A 450 put bought against two 440 puts sold. At zero it is worth 450 - 2 x 440 = -$430.00; at 440 it is
    worth $10.00, its most. The lowest holds at zero alone, which checking the strikes would never find."""
    puts = _of(_leg("bought", "long", "put", 450), _leg("sold", "short", "put", 440, ratio=2))

    figures = at_expiry(puts, _ONE, net_price=Decimal("1.00"))

    assert figures.loss_holds == (_between("0", "0"),)
    assert (figures.gain_cents, figures.gain_holds) == (90_000, (_between("440", "440"),))
    # Even where it is worth the $1.00 paid: 431 on the way up to 440, and 449 on the way down from it.
    assert figures.break_evens == (Fraction(431), Fraction(449))


def test_a_spread_bought_for_more_than_it_can_be_worth_gains_nothing_and_never_breaks_even() -> None:
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    figures = at_expiry(spread, _ONE, net_price=Decimal("6.00"))

    assert (figures.gain_cents, figures.break_evens) == (0, ())


def test_a_spread_bought_for_its_whole_width_breaks_even_at_its_upper_strike_and_nowhere_else() -> None:
    """The 585 call bought and the 590 sold for $5.00. It is worth $5.00 only from 590 up, where it is level:
    it first gets even exactly at 590, a turning point."""
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    figures = at_expiry(spread, _ONE, net_price=Decimal("5.00"))

    assert (figures.gain_cents, figures.break_evens) == (0, (Fraction(590),))


def test_every_structure_of_an_entry_gains_its_own_share() -> None:
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    figures = at_expiry(spread, EntrySize(multiplier=100, units=3), net_price=Decimal("1.94"))

    assert figures.gain_cents == 3 * 30_600
    assert figures.break_evens == (Fraction("586.94"),)


def test_legs_that_do_not_share_an_expiry_have_nothing_estimated() -> None:
    calendar = _of(_leg("near", "long", "call", 585), _leg("far", "long", "call", 590, expiry=date(2026, 10, 16)))

    assert at_expiry(calendar, _ONE, net_price=Decimal("7.00")) is None


def test_a_structure_that_keeps_losing_as_the_price_rises_is_refused_as_the_requirement_refuses_it() -> None:
    with pytest.raises(StructureMoneyRefused) as refused:
        at_expiry(_of(_leg("sold", "short", "call", 585)), _ONE, net_price=Decimal("-4.00"))

    assert refused.value.reason is StructureMoneyRefusal.LOSS_UNBOUNDED


@pytest.mark.parametrize(
    "net_price",
    [pytest.param(Decimal("1.94123"), id="finer than a cent a contract"), pytest.param(Decimal("NaN"), id="no number")],
)
def test_a_net_price_that_is_no_amount_of_money_is_the_callers_mistake(net_price: Decimal) -> None:
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    with pytest.raises(ValueError):
        at_expiry(spread, _ONE, net_price=net_price)


# ---- The line to draw ----------------------------------------------------------------------------


def _line(*points: tuple[str, int]) -> tuple[ResultAt, ...]:
    return tuple(ResultAt(Decimal(price), cents) for price, cents in points)


def test_a_bought_call_spreads_line_runs_from_below_its_strikes_to_above_them_through_each() -> None:
    """585 bought and 590 sold for $1.94, even at 586.94. The strikes and the break-even span 585 to 590, a
    quarter of which is $1.25, so the line starts $1.25 below 585 rounded down to 583, and ends above 590 at
    592. It is level at -$194.00 up to 585, rises to +$306.00 at 590, and is level again."""
    spread = _of(_leg("bought", "long", "call", 585), _leg("sold", "short", "call", 590))

    figures = at_expiry(spread, _ONE, net_price=Decimal("1.94"))

    assert figures.sketch == _line(("583", -19_400), ("585", -19_400), ("590", 30_600), ("592", 30_600))


def test_one_bought_calls_line_reaches_past_its_break_even_and_keeps_rising() -> None:
    """A 585 call bought for $4.12 is even at 589.12. From 585 to 589.12 is $4.12, a quarter of it $1.03: the
    line starts at 583 and ends at 589.12 + 1.03 = 590.15, rounded up to 591, where the call is worth $6.00
    and the entry is $1.88 a share ahead. Three structures: three times each."""
    call = _of(_leg("bought", "long", "call", 585))

    figures = at_expiry(call, EntrySize(multiplier=100, units=3), net_price=Decimal("4.12"))

    assert figures.sketch == _line(("583", -123_600), ("585", -123_600), ("591", 56_400))


def test_an_iron_condors_line_turns_at_each_of_its_four_strikes() -> None:
    """Puts at 570 and 575 and calls at 595 and 600, sold for $1.50. It keeps the $150.00 between 575 and 595
    and loses $5.00 - $1.50 at or beyond a wing. Even at 573.50 and 596.50. The window: 30 wide, a quarter is
    $7.50, so from 562 to 608."""
    condor = _of(
        _leg("long_put", "long", "put", 570),
        _leg("short_put", "short", "put", 575),
        _leg("short_call", "short", "call", 595),
        _leg("long_call", "long", "call", 600),
    )

    figures = at_expiry(condor, _ONE, net_price=Decimal("-1.50"))

    assert figures.break_evens == (Fraction("573.5"), Fraction("596.5"))
    assert figures.sketch == _line(
        ("562", -35_000), ("570", -35_000), ("575", 15_000), ("595", 15_000), ("600", -35_000), ("608", -35_000)
    )


def test_a_line_never_starts_below_a_price_of_zero() -> None:
    """A 2 put bought for $1.50 is even at 0.50. A dollar below that is under zero, and no stock trades there:
    the line starts at zero, where the put is worth its whole strike and the entry is 50 cents a share ahead."""
    put = _of(_leg("bought", "long", "put", 2))

    figures = at_expiry(put, _ONE, net_price=Decimal("1.50"))

    assert figures.break_evens == (Fraction("0.5"),)
    assert figures.sketch == _line(("0", 5_000), ("2", -15_000), ("3", -15_000))

