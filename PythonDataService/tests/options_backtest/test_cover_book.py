"""The cover book, one scene at a time, each worked by hand from the rules and never taken from the code's output.

A scene states what is held, what is offered and what the owner would see: the contracts taken, the reason for
each refusal, the collateral in dollars. Money is exact, so every comparison is exact equality.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal

import pytest

from app.options.contract import OptionContract, OptionRight
from app.options_backtest.cover_book import (
    Admission,
    Candidate,
    CoverInvariantBroken,
    Held,
    Holding,
    OpeningRefusal,
    admit_opening,
    assignment_obligation,
    collateral,
    covers,
    is_covered,
    leaving_plan,
    may_leave,
)

D = Decimal
SOON = date(2026, 10, 9)
LATER = date(2026, 10, 16)
PLENTY = D("1000000")


def option(right: OptionRight, strike: str, expiry: date = SOON, root: str = "SPY") -> OptionContract:
    return OptionContract.of(root=root, expiry_date=expiry, right=right, strike=D(strike))


def bought(
    leg_id: str, right: OptionRight, strike: str, contracts: int = 1, expiry: date = SOON, root: str = "SPY"
) -> Holding:
    return Holding(leg_id, option(right, strike, expiry, root), "buy", contracts, 100)


def sold(
    leg_id: str, right: OptionRight, strike: str, contracts: int = 1, expiry: date = SOON, root: str = "SPY"
) -> Holding:
    return Holding(leg_id, option(right, strike, expiry, root), "sell", contracts, 100)


def buy(
    leg_id: str,
    right: OptionRight,
    strike: str,
    contracts: int = 1,
    *,
    cash: str = "-1",
    expiry: date = SOON,
    root: str = "SPY",
) -> Candidate:
    return Candidate(leg_id, option(right, strike, expiry, root), "buy", contracts, 100, D(cash))


def sell(
    leg_id: str,
    right: OptionRight,
    strike: str,
    contracts: int = 1,
    *,
    cash: str = "1",
    expiry: date = SOON,
    root: str = "SPY",
) -> Candidate:
    return Candidate(leg_id, option(right, strike, expiry, root), "sell", contracts, 100, D(cash))


def book(listed: str, *holdings: Holding) -> Held:
    """A book of legs named one letter each, in the order written, holding ``holdings``."""
    return Held(listed=tuple(listed), holdings=holdings)


def admission(
    held: Held, *candidates: Candidate, cash: Decimal = PLENTY, closing: Iterable[str] = ()
) -> dict[str, OpeningRefusal | None]:
    """What each candidate met: None when it was taken, else the reason it was refused."""
    result: Admission = admit_opening(held, candidates, cash=cash, closing=closing)
    answer: dict[str, OpeningRefusal | None] = {candidate.leg_id: None for candidate in result.taken}
    answer.update({refused.candidate.leg_id: refused.reason for refused in result.refused})
    return answer


# ---- What is held -------------------------------------------------------------


def test_a_book_built_empty_holds_nothing_and_remembers_the_setups_order() -> None:
    held = Held.empty(["B", "A", "C"])

    assert held.listed == ("B", "A", "C")
    assert held.holdings == ()
    assert held.holding_of("A") is None
    assert collateral(held) == D("0")
    assert assignment_obligation(held) == D("0")


def test_opened_and_without_return_new_values_and_leave_the_old_one_alone() -> None:
    empty = Held.empty(["A", "B"])

    one = empty.opened([buy("A", "call", "680")])
    two = one.opened([sell("B", "call", "675")])
    back = two.without(["B"])

    assert empty.holdings == ()
    assert [holding.leg_id for holding in one.holdings] == ["A"]
    assert [holding.leg_id for holding in two.holdings] == ["A", "B"]
    assert back == one


def test_holdings_are_kept_in_the_order_the_setup_lists_its_legs() -> None:
    held = Held.empty(["A", "B", "C"]).opened([sell("C", "call", "675"), buy("A", "call", "680")])

    assert [holding.leg_id for holding in held.holdings] == ["A", "C"]


def test_two_legs_on_one_contract_on_the_same_side_add_up() -> None:
    # Three bought 680 calls in two legs cover three sold 675 calls in two legs: $5 wide, three contracts.
    # With one more contract sold, the sold side is four against three bought and one is bare.
    together = book(
        "ABCD",
        bought("A", "call", "680", 2),
        bought("B", "call", "680", 1),
        sold("C", "call", "675", 2),
        sold("D", "call", "675", 1),
    )
    one_more = book(
        "ABCD",
        bought("A", "call", "680", 2),
        bought("B", "call", "680", 1),
        sold("C", "call", "675", 2),
        sold("D", "call", "675", 2),
    )

    assert is_covered(together)
    assert collateral(together) == D("1500.00")
    assert not is_covered(one_more)


def test_a_sold_holding_counts_as_negative_and_a_bought_one_as_positive() -> None:
    assert sold("A", "put", "660", 2).signed_contracts == -2
    assert bought("A", "put", "660", 3).signed_contracts == 3


def test_the_setups_leg_order_is_a_tuple_whatever_it_was_built_from() -> None:
    from_a_list = Held(listed=["A", "B"], holdings=[bought("A", "call", "680")])  # type: ignore[arg-type]
    from_tuples = Held(listed=("A", "B"), holdings=(bought("A", "call", "680"),))

    assert from_a_list.listed == ("A", "B")
    assert from_a_list.holdings == (bought("A", "call", "680"),)
    assert from_a_list == from_tuples
    assert hash(from_a_list) == hash(from_tuples)


def test_a_contract_cannot_be_held_on_both_sides() -> None:
    with pytest.raises(ValueError, match="held on both sides"):
        book("AB", bought("A", "call", "680"), sold("B", "call", "680"))


def test_a_leg_holds_one_contract_and_must_be_one_the_setup_lists() -> None:
    with pytest.raises(ValueError, match="holds one contract"):
        book("AB", bought("A", "call", "680"), bought("A", "call", "685"))
    with pytest.raises(ValueError, match="not among the setup's legs"):
        book("AB", bought("Z", "call", "680"))
    with pytest.raises(ValueError, match="lists each leg once"):
        Held.empty(["A", "A"])


def test_a_leg_that_holds_nothing_cannot_leave() -> None:
    with pytest.raises(ValueError, match="hold nothing"):
        book("AB", bought("A", "call", "680")).without(["B"])


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        pytest.param(lambda: Holding("A", option("call", "680"), "buy", 1, 10), "standard 100-share", id="multiplier"),
        pytest.param(lambda: Holding("A", option("call", "680"), "buy", 0, 100), "whole number", id="no contracts"),
        pytest.param(lambda: Holding("A", option("call", "680"), "long", 1, 100), "'buy' or 'sell'", id="side"),  # type: ignore[arg-type]
        pytest.param(lambda: Holding("", option("call", "680"), "buy", 1, 100), "non-empty id", id="leg id"),
        pytest.param(
            lambda: Candidate("A", option("call", "680"), "buy", 1, 100, D("2.00")), "takes cash out", id="buy for cash"
        ),
        pytest.param(
            lambda: Candidate("A", option("call", "680"), "sell", 1, 100, 1.5),  # type: ignore[arg-type]
            "exact decimal",
            id="float cash",
        ),
    ],
)
def test_a_position_that_is_not_one_the_book_can_judge_is_refused(make: Callable[[], object], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        make()


# ---- Collateral ---------------------------------------------------------------


def test_an_iron_condors_collateral_is_its_wider_side_and_not_the_sum() -> None:
    # Puts 660/665 are $5 wide and calls 685/695 are $10 wide. The sums of both sides' worst cases never meet
    # at one price: the lowest summed payoff is -10 a share, so $1,000.00 and not $1,500.00.
    held = book(
        "ABCD",
        bought("A", "put", "660"),
        sold("B", "put", "665"),
        sold("C", "call", "685"),
        bought("D", "call", "695"),
    )

    assert collateral(held) == D("1000.00")


def test_an_iron_condor_with_equal_wings_pledges_one_wing() -> None:
    held = book(
        "ABCD",
        bought("A", "put", "660"),
        sold("B", "put", "665"),
        sold("C", "call", "685"),
        bought("D", "call", "690"),
    )

    assert collateral(held) == D("500.00")


def test_a_call_butterfly_pledges_nothing() -> None:
    # +675C -2x680C +685C is worth zero or more at every price: its loss is its debit, which is not collateral.
    held = book("ABC", bought("A", "call", "675"), sold("B", "call", "680", 2), bought("C", "call", "685"))

    assert collateral(held) == D("0.00")


def test_an_iron_butterfly_pledges_its_wing() -> None:
    held = book(
        "ABCD",
        bought("A", "put", "675"),
        sold("B", "put", "680"),
        sold("C", "call", "680"),
        bought("D", "call", "685"),
    )

    assert collateral(held) == D("500.00")


def test_a_sold_put_with_its_bought_put_pledges_the_width() -> None:
    held = book("AB", bought("A", "put", "660"), sold("B", "put", "665"))

    assert collateral(held) == D("500.00")


def test_bought_legs_pledge_nothing_even_across_expiries() -> None:
    held = book("AB", bought("A", "call", "680"), bought("B", "put", "660", expiry=LATER))

    assert collateral(held) == D("0.00")


def test_two_contracts_of_one_spread_pledge_twice_the_width() -> None:
    held = book("AB", bought("A", "call", "680", 2), sold("B", "call", "675", 2))

    assert collateral(held) == D("1000.00")


def test_collateral_is_in_whole_cents_as_a_decimal() -> None:
    held = book("AB", bought("A", "call", "680"), sold("B", "call", "675"))

    assert str(collateral(held)) == "500.00"


def test_a_loss_with_no_limit_is_a_broken_invariant_and_is_never_estimated() -> None:
    # A sold call with no bought call at its expiry: the book never builds this, so seeing it is a fault.
    held = book("AB", sold("A", "call", "675"), bought("B", "call", "680", expiry=LATER))

    with pytest.raises(CoverInvariantBroken, match="no limit"):
        collateral(held)
    with pytest.raises(CoverInvariantBroken):
        leaving_plan(held, [])
    with pytest.raises(CoverInvariantBroken):
        may_leave(held, "B")


# ---- The cover identity, public -----------------------------------------------

_SPY_680_CALL = option("call", "680")


@pytest.mark.parametrize(
    ("bought_contract", "sold_contract", "answer"),
    [
        pytest.param(_SPY_680_CALL, option("call", "675"), True, id="same kind and expiry, another strike"),
        pytest.param(_SPY_680_CALL, option("call", "680"), True, id="the same contract"),
        pytest.param(option("put", "660"), option("put", "665"), True, id="puts"),
        pytest.param(_SPY_680_CALL, option("put", "680"), False, id="a call for a put"),
        pytest.param(_SPY_680_CALL, option("call", "675", LATER), False, id="another expiry"),
        pytest.param(_SPY_680_CALL, option("call", "675", root="QQQ"), False, id="another underlying"),
    ],
)
def test_one_contract_covers_another_only_on_underlying_kind_and_expiry(
    bought_contract: OptionContract, sold_contract: OptionContract, answer: bool
) -> None:
    assert covers(bought_contract, sold_contract) is answer
    # An identity on those three: it does not matter which is which.
    assert covers(sold_contract, bought_contract) is answer


@pytest.mark.parametrize(
    ("held", "answer"),
    [
        pytest.param(Held.empty("A"), True, id="nothing held"),
        pytest.param(book("AB", bought("A", "call", "680"), bought("B", "put", "660")), True, id="bought only"),
        pytest.param(book("AB", bought("A", "call", "680"), sold("B", "call", "675")), True, id="a spread"),
        pytest.param(book("A", sold("A", "put", "660")), False, id="a bare sale"),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675"), sold("C", "call", "670")),
            False,
            id="two sold for one bought",
        ),
        pytest.param(book("AB", bought("A", "call", "680", 2), sold("B", "call", "675", 2)), True, id="two for two"),
        pytest.param(book("AB", bought("A", "put", "660", expiry=LATER), sold("B", "put", "665")), False, id="expiry"),
        pytest.param(book("AB", bought("A", "call", "660"), sold("B", "put", "665")), False, id="call for a put"),
        pytest.param(
            book("AB", bought("A", "call", "680", root="QQQ"), sold("B", "call", "675")), False, id="underlying"
        ),
        pytest.param(
            book(
                "ABCD",
                bought("A", "call", "680"),
                sold("B", "call", "675"),
                bought("C", "put", "660", 3),
                sold("D", "put", "665", 3),
            ),
            True,
            id="two kinds each covered",
        ),
    ],
)
def test_what_is_held_is_covered_when_each_kind_has_a_bought_contract_for_each_sold_one(
    held: Held, answer: bool
) -> None:
    assert is_covered(held) is answer


# ---- Cover, as the opening group meets it -------------------------------------


def test_a_sold_put_is_covered_by_a_bought_put_of_the_same_expiry() -> None:
    held = Held.empty("AB")

    assert admission(held, sell("B", "put", "665"), buy("A", "put", "660")) == {"A": None, "B": None}


def test_cover_does_not_depend_on_the_strike() -> None:
    held = Held.empty("AB")

    assert admission(held, buy("A", "put", "400"), sell("B", "put", "665")) == {"A": None, "B": None}


@pytest.mark.parametrize(
    "cover",
    [
        pytest.param(bought("A", "put", "660", expiry=LATER), id="a bought put of another expiry"),
        pytest.param(bought("A", "call", "660"), id="a bought call"),
    ],
)
def test_a_sold_put_is_not_covered_by_another_expiry_or_the_other_kind(cover: Holding) -> None:
    # Isolated from the expiry step: what is held has the sold put and this cover, and nothing leaves.
    held = book("AB", cover, sold("B", "put", "665"))

    assert leaving_plan(held, []) == ("B",)


def test_one_bought_contract_does_not_cover_two_sold_in_one_leg() -> None:
    held = Held.empty("AB")

    assert admission(held, buy("A", "call", "680"), sell("B", "call", "675", 2)) == {
        "A": None,
        "B": OpeningRefusal.NO_COVER,
    }


def test_one_bought_contract_does_not_cover_two_sold_in_two_legs_and_the_first_listed_is_taken() -> None:
    held = Held.empty("ABC")

    assert admission(held, buy("A", "call", "680"), sell("B", "call", "675"), sell("C", "call", "670")) == {
        "A": None,
        "B": None,
        "C": OpeningRefusal.NO_COVER,
    }


def test_candidates_are_judged_in_the_setups_order_whatever_order_they_arrive_in() -> None:
    held = Held.empty("ABC")

    assert admission(held, sell("C", "call", "670"), sell("B", "call", "675"), buy("A", "call", "680")) == {
        "A": None,
        "B": None,
        "C": OpeningRefusal.NO_COVER,
    }


def test_a_sold_candidate_takes_cover_whole_or_not_at_all_so_a_later_one_may_use_what_is_left() -> None:
    held = Held.empty("ABC")

    # One bought contract: B wants two and is refused without taking it, so C takes it.
    assert admission(held, buy("A", "call", "680"), sell("B", "call", "675", 2), sell("C", "call", "670")) == {
        "A": None,
        "B": OpeningRefusal.NO_COVER,
        "C": None,
    }


def test_two_bought_contracts_in_one_leg_cover_two_sold_in_one_leg() -> None:
    held = Held.empty("AB")

    assert admission(held, buy("A", "call", "680", 2), sell("B", "call", "675", 2)) == {"A": None, "B": None}


def test_bought_legs_listed_after_the_sold_leg_still_cover_it_in_the_same_minute() -> None:
    held = Held.empty("AB")

    assert admission(held, sell("A", "call", "675"), buy("B", "call", "680")) == {"A": None, "B": None}


def test_a_held_bought_contract_covers_a_new_sale_until_a_held_sale_has_used_it() -> None:
    covered_once = book("AB", bought("A", "call", "680"))
    covered_and_used = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(covered_once, sell("B", "call", "675")) == {"B": None}
    assert admission(covered_and_used, sell("C", "call", "670")) == {"C": OpeningRefusal.NO_COVER}


def test_a_bought_contract_whose_close_is_pending_is_not_cover_for_a_new_sale() -> None:
    held = book("AB", bought("A", "call", "680", 1))

    assert admission(held, sell("B", "call", "675"), closing=[]) == {"B": None}
    assert admission(held, sell("B", "call", "675"), closing=["A"]) == {"B": OpeningRefusal.NO_COVER}


def test_a_bought_call_on_another_underlying_is_not_cover_for_a_sold_call() -> None:
    held = Held.empty("AB")

    assert admission(held, buy("A", "call", "680", root="QQQ"), sell("B", "call", "675")) == {
        "A": None,
        "B": OpeningRefusal.NO_COVER,
    }


def test_a_sold_leg_whose_buy_back_is_pending_still_uses_its_cover() -> None:
    # B is covered by A and is being bought back, but the buy-back fills after the opening group. Until it does,
    # B still sits on A's contract, so a new sale has none to take.
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(held, sell("C", "call", "670"), closing=["B"]) == {"C": OpeningRefusal.NO_COVER}


def test_a_bought_candidate_that_was_refused_is_not_cover() -> None:
    # Leg X holds a sold 700 call, covered by Y. Candidate V buys that same contract, which X holds on the other
    # side, so V is refused; W's sale then has only Y's contract, and that is already X's cover.
    held = book("XYVW", sold("X", "call", "700"), bought("Y", "call", "710"))

    assert admission(held, buy("V", "call", "700"), sell("W", "call", "695")) == {
        "V": OpeningRefusal.OTHER_SIDE,
        "W": OpeningRefusal.NO_COVER,
    }


# ---- The other refusals, each by a case built for it --------------------------


def test_a_candidate_in_a_contract_another_leg_holds_on_the_other_side_is_refused() -> None:
    held = book("AB", bought("A", "call", "680"))

    assert admission(held, sell("B", "call", "680")) == {"B": OpeningRefusal.OTHER_SIDE}


def test_two_legs_in_one_group_on_one_contract_add_when_alike_and_the_later_is_refused_when_opposed() -> None:
    held = Held.empty("ABC")

    assert admission(held, buy("A", "call", "680"), buy("B", "call", "680")) == {"A": None, "B": None}
    assert admission(held, buy("A", "call", "680"), sell("B", "call", "680")) == {
        "A": None,
        "B": OpeningRefusal.OTHER_SIDE,
    }


def test_a_sold_candidate_at_another_expiry_than_what_is_held_is_refused() -> None:
    held = book("AB", bought("A", "call", "680"))

    assert admission(held, sell("B", "call", "675", expiry=LATER)) == {"B": OpeningRefusal.SECOND_EXPIRY}


def test_a_bought_candidate_at_another_expiry_is_refused_while_a_sold_contract_is_held() -> None:
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(held, buy("C", "call", "700", expiry=LATER)) == {"C": OpeningRefusal.SECOND_EXPIRY}


def test_a_bought_candidate_at_another_expiry_is_refused_beside_a_sold_candidate_listed_first() -> None:
    held = Held.empty("ABC")

    assert admission(
        held, buy("A", "call", "680"), sell("B", "call", "675"), buy("C", "call", "700", expiry=LATER)
    ) == {
        "A": None,
        "B": None,
        "C": OpeningRefusal.SECOND_EXPIRY,
    }


def test_bought_only_holdings_may_span_expiries() -> None:
    held = book("AB", bought("A", "call", "680"))

    assert admission(held, buy("B", "put", "660", expiry=LATER)) == {"B": None}


def test_a_group_that_would_pledge_more_than_the_cash_is_refused_whole() -> None:
    # Sell 675C at 6.50 and buy 680C at 3.10, a dollar of fees on each: +649.00 and -311.00, net +338.00.
    # The spread is $5 wide, so $500.00 is pledged and the cash before must be at least 162.00.
    held = Held.empty("AB")
    group = (sell("A", "call", "675", cash="649.00"), buy("B", "call", "680", cash="-311.00"))

    assert admission(held, *group, cash=D("162.00")) == {"A": None, "B": None}
    assert admission(held, *group, cash=D("161.99")) == {"A": OpeningRefusal.CASH_SHORT, "B": OpeningRefusal.CASH_SHORT}


def test_for_bought_legs_only_the_rule_is_that_the_cost_is_not_more_than_the_cash() -> None:
    held = Held.empty("AB")
    group = (buy("A", "call", "680", cash="-300.00"), buy("B", "put", "660", cash="-250.00"))

    assert admission(held, *group, cash=D("550.00")) == {"A": None, "B": None}
    assert admission(held, *group, cash=D("549.99")) == {"A": OpeningRefusal.CASH_SHORT, "B": OpeningRefusal.CASH_SHORT}


def test_collateral_already_held_is_not_cash_a_new_bought_leg_may_spend() -> None:
    # A $5 spread is held and pledges $500.00. With $400.00 of cash, a $50.00 purchase would leave $350.00.
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(held, buy("C", "put", "660", cash="-50.00"), cash=D("400.00")) == {"C": OpeningRefusal.CASH_SHORT}
    assert admission(held, buy("C", "put", "660", cash="-50.00"), cash=D("550.00")) == {"C": None}


def test_a_bought_leg_that_does_not_change_the_collateral_is_judged_against_the_collateral_held() -> None:
    # Holdings pledge $500.00 and the cash is $499.00: even a free purchase leaves the cash under the pledge.
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(held, buy("C", "put", "660", cash="0"), cash=D("499.00")) == {"C": OpeningRefusal.CASH_SHORT}


def test_a_pending_close_is_still_held_and_still_pledged_for_the_cash_step() -> None:
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(held, buy("C", "put", "660", cash="-50.00"), cash=D("400.00"), closing=["A", "B"]) == {
        "C": OpeningRefusal.CASH_SHORT
    }


def test_the_cash_step_sees_only_the_candidates_still_standing() -> None:
    # B is refused for cover. A's cost alone is then judged against the cash, and fails on its own.
    held = Held.empty("AB")

    assert admission(
        held, buy("A", "put", "660", cash="-300.00"), sell("B", "call", "675", cash="649.00"), cash=D("100")
    ) == {
        "A": OpeningRefusal.CASH_SHORT,
        "B": OpeningRefusal.NO_COVER,
    }


def test_an_empty_group_is_taken_whole_and_asks_nothing_of_the_cash() -> None:
    result = admit_opening(book("A", bought("A", "call", "680")), [], cash=D("-5"), closing=[])

    assert result == Admission(taken=(), refused=())


# ---- The order of the four steps, each by a case where two would apply --------


def test_a_sold_candidate_refused_for_cover_was_still_in_play_for_the_second_expiry_step() -> None:
    # A sells a call with nothing to cover it. The second-expiry step runs before cover does and sees A in play,
    # so B, a call bought at another expiry beside a sold contract, is refused there. A's own refusal, at the
    # cover step, frees nothing an earlier step decided.
    held = Held.empty("AB")

    assert admission(held, sell("A", "call", "675"), buy("B", "call", "700", expiry=LATER)) == {
        "A": OpeningRefusal.NO_COVER,
        "B": OpeningRefusal.SECOND_EXPIRY,
    }


def test_other_side_is_judged_before_second_expiry() -> None:
    # Bought-only holdings span two expiries. Selling the 680 call at SOON is a contract held on the other side
    # and would also be a second expiry (LATER is in play and the candidate is sold).
    held = book("ABC", bought("A", "call", "680"), bought("B", "call", "700", expiry=LATER))

    assert admission(held, sell("C", "call", "680")) == {"C": OpeningRefusal.OTHER_SIDE}


def test_second_expiry_is_judged_before_cover() -> None:
    # Selling a LATER call is a second expiry and is also without cover (nothing bought expires LATER).
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"))

    assert admission(held, sell("C", "call", "700", expiry=LATER)) == {"C": OpeningRefusal.SECOND_EXPIRY}


def test_cover_is_judged_before_cash() -> None:
    # A lone sale of a put has no cover, and it would also pledge $66,000 against $0 of cash.
    assert admission(Held.empty("A"), sell("A", "put", "660", cash="100.00"), cash=D("0")) == {
        "A": OpeningRefusal.NO_COVER
    }


# ---- Which bought legs may leave ----------------------------------------------


def test_a_bought_leg_may_not_leave_when_it_alone_covers_a_sold_leg() -> None:
    held = book("AB", bought("A", "call", "680"), sold("B", "call", "675"))

    assert may_leave(held, "A") is False


def test_a_bought_leg_may_not_leave_when_every_sold_contract_stays_covered_but_the_collateral_rises() -> None:
    # Bought 680 and 690 calls, sold 675 call: $500.00 pledged. Without the 680 the 690 still covers, but the
    # spread is then 15 wide and pledges $1,500.00. Without the 690 it is still 5 wide.
    held = book("ABC", bought("A", "call", "680"), bought("B", "call", "690"), sold("C", "call", "675"))

    assert collateral(held) == D("500.00")
    assert may_leave(held, "A") is False
    assert may_leave(held, "B") is True


def test_a_bought_leg_that_protects_nothing_sold_may_leave() -> None:
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"), bought("C", "put", "660"))

    assert may_leave(held, "C") is True


def test_with_nothing_sold_every_bought_leg_may_leave() -> None:
    held = book("AB", bought("A", "call", "680"), bought("B", "put", "660"))

    assert may_leave(held, "A") is True
    assert may_leave(held, "B") is True


def test_may_leave_is_asked_of_bought_legs_that_are_held() -> None:
    held = book("AB", bought("A", "call", "680"), sold("B", "call", "675"))

    with pytest.raises(ValueError, match="bought legs that are held"):
        may_leave(held, "B")
    with pytest.raises(ValueError, match="bought legs that are held"):
        may_leave(book("AB", bought("A", "call", "680")), "B")


# ---- Which sold legs go with a leaving bought leg -----------------------------


def test_two_sold_legs_that_depend_on_one_leaving_leg_go_last_listed_first() -> None:
    # A is two 680 calls; B sells the 685 and C the 690. Without A both are bare, so both go, C first.
    held = book("ABC", bought("A", "call", "680", 2), sold("B", "call", "685"), sold("C", "call", "690"))

    assert leaving_plan(held, ["A"]) == ("C", "B")


def test_the_plan_stops_as_soon_as_what_remains_is_covered_with_no_more_collateral() -> None:
    # Listed A, D, B, C. Before: A 680 and D 690 bought, B 685 and C 675 sold: $1,000.00. Without A the C sale goes
    # first (last listed) and B stays against D, a 5-wide spread of $500.00, which is not more.
    held = book(
        "ADBC",
        bought("A", "call", "680"),
        bought("D", "call", "690"),
        sold("B", "call", "685"),
        sold("C", "call", "675"),
    )

    assert collateral(held) == D("1000.00")
    assert leaving_plan(held, ["A"]) == ("C",)


def test_the_plan_is_a_policy_by_listed_order_and_not_the_fewest_legs() -> None:
    # The same four legs listed A, D, C, B: the B sale is now last listed and goes first. C against D is then a
    # 15-wide spread of $1,500.00, which is more than before, so C goes too. Closing C alone would have sufficed.
    held = book(
        "ADCB",
        bought("A", "call", "680"),
        bought("D", "call", "690"),
        sold("C", "call", "675"),
        sold("B", "call", "685"),
    )

    assert leaving_plan(held, ["A"]) == ("B", "C")


def test_sold_legs_of_the_leaving_legs_kind_go_before_any_other_whatever_the_listing() -> None:
    # A is two 680 calls leaving, B sells the 685 call, P1 is two 690 puts and P2 sells the 700 put.
    # Nothing is pledged now. Without A the 685 call is bare; closed, the puts alone pledge $1,000.00, which is
    # more, so the put goes as well. Last listed first alone would have said P2 then B.
    held = book(
        "ABPQ",
        bought("A", "call", "680", 2),
        sold("B", "call", "685"),
        bought("P", "put", "690", 2),
        sold("Q", "put", "700"),
    )

    assert collateral(held) == D("0.00")
    assert leaving_plan(held, ["A"]) == ("B", "Q")


def test_a_bought_leg_that_protects_nothing_leaves_alone() -> None:
    held = book("ABC", bought("A", "call", "680"), sold("B", "call", "675"), bought("C", "put", "660"))

    assert leaving_plan(held, ["C"]) == ()


def test_nothing_leaving_from_covered_holdings_closes_nothing_and_from_uncovered_closes_the_bare_leg() -> None:
    covered = book("AB", bought("A", "put", "660"), sold("B", "put", "665"))
    bare = book("AB", bought("A", "call", "660"), sold("B", "put", "665"))

    assert leaving_plan(covered, []) == ()
    assert leaving_plan(bare, []) == ("B",)


def test_several_legs_may_leave_at_once() -> None:
    held = book(
        "ABCD", bought("A", "call", "680"), sold("B", "call", "675"), bought("C", "put", "660"), sold("D", "put", "665")
    )

    assert leaving_plan(held, ["A", "C"]) == ("D", "B")


def test_holdings_with_no_sold_leg_need_no_plan_whatever_leaves() -> None:
    held = book("AB", bought("A", "call", "680"), bought("B", "put", "660"))

    assert leaving_plan(held, ["A", "B"]) == ()


def test_a_leaving_leg_must_be_a_bought_leg_that_is_held() -> None:
    held = book("AB", bought("A", "call", "680"), sold("B", "call", "675"))

    with pytest.raises(ValueError, match="bought legs that are held"):
        leaving_plan(held, ["B"])
    with pytest.raises(ValueError, match="bought legs that are held"):
        leaving_plan(held, ["Z"])


# ---- Assignment obligation ----------------------------------------------------


def test_the_assignment_obligation_is_sold_contracts_times_strike_times_multiplier() -> None:
    # 2 x 675 x 100 + 1 x 680 x 100 = 135,000 + 68,000. The bought leg is not counted.
    held = book("ABC", sold("A", "put", "675", 2), sold("B", "call", "680"), bought("C", "call", "700", 5))

    assert assignment_obligation(held) == D("203000")


def test_the_obligation_is_gross_and_a_spread_is_not_netted() -> None:
    held = book("AB", bought("A", "call", "680"), sold("B", "call", "675"))

    assert assignment_obligation(held) == D("67500")
    assert collateral(held) == D("500.00")


# ---- Opening groups ask only what they must -----------------------------------


@pytest.mark.parametrize(
    ("held", "candidates", "closing", "reason"),
    [
        pytest.param(Held.empty("A"), [buy("Z", "call", "680")], [], "not among the setup's legs", id="unlisted leg"),
        pytest.param(
            Held.empty("A"), [buy("A", "call", "680"), buy("A", "call", "685")], [], "these repeat", id="a leg twice"
        ),
        pytest.param(
            book("A", bought("A", "call", "680")), [buy("A", "call", "685")], [], "already hold", id="leg that holds"
        ),
        pytest.param(Held.empty("AB"), [buy("A", "call", "680")], ["B"], "hold nothing", id="close of nothing"),
    ],
)
def test_a_group_that_is_not_a_question_the_book_can_answer_is_refused_loudly(
    held: Held, candidates: list[Candidate], closing: list[str], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        admit_opening(held, candidates, cash=PLENTY, closing=closing)


def test_cash_must_be_an_exact_decimal() -> None:
    with pytest.raises(ValueError, match="exact decimal"):
        admit_opening(Held.empty("A"), [], cash=10.5, closing=[])  # type: ignore[arg-type]
