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


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        pytest.param(
            lambda: book("AB", bought("A", "call", "680"), sold("B", "call", "680")),
            "held on both sides",
            id="a contract cannot be held on both sides",
        ),
        pytest.param(
            lambda: book("AB", bought("A", "call", "680"), bought("A", "call", "685")),
            "holds one contract",
            id="a leg holds one contract",
        ),
        pytest.param(
            lambda: book("AB", bought("Z", "call", "680")),
            "not among the setup's legs",
            id="a leg must be one the setup lists",
        ),
        pytest.param(lambda: Held.empty(["A", "A"]), "lists each leg once", id="a setup lists each leg once"),
        pytest.param(
            lambda: book("AB", bought("A", "call", "680")).without(["B"]),
            "hold nothing",
            id="a leg that holds nothing cannot leave",
        ),
        pytest.param(
            lambda: admit_opening(Held.empty("A"), [], cash=10.5, closing=[]),  # type: ignore[arg-type]
            "exact decimal",
            id="cash must be an exact decimal",
        ),
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
        pytest.param(
            lambda: may_leave(book("AB", bought("A", "call", "680"), sold("B", "call", "675")), "B"),
            "bought legs that are held",
            id="may_leave is asked of bought legs that are held: a sold leg",
        ),
        pytest.param(
            lambda: may_leave(book("AB", bought("A", "call", "680")), "B"),
            "bought legs that are held",
            id="may_leave is asked of bought legs that are held: a leg that holds nothing",
        ),
        pytest.param(
            lambda: leaving_plan(book("AB", bought("A", "call", "680"), sold("B", "call", "675")), ["B"]),
            "bought legs that are held",
            id="a leaving leg must be a bought leg that is held: a sold leg",
        ),
        pytest.param(
            lambda: leaving_plan(book("AB", bought("A", "call", "680"), sold("B", "call", "675")), ["Z"]),
            "bought legs that are held",
            id="a leaving leg must be a bought leg that is held: a leg the setup does not list",
        ),
    ],
)
def test_a_book_or_position_the_book_cannot_judge_is_refused(make: Callable[[], object], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        make()


# ---- Collateral ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("held", "pledged"),
    [
        # Puts 660/665 are $5 wide and calls 685/695 are $10 wide. The sums of both sides' worst cases never meet
        # at one price: the lowest summed payoff is -10 a share, so $1,000.00 and not $1,500.00.
        pytest.param(
            book(
                "ABCD",
                bought("A", "put", "660"),
                sold("B", "put", "665"),
                sold("C", "call", "685"),
                bought("D", "call", "695"),
            ),
            D("1000.00"),
            id="an iron condor's collateral is its wider side and not the sum",
        ),
        pytest.param(
            book(
                "ABCD",
                bought("A", "put", "660"),
                sold("B", "put", "665"),
                sold("C", "call", "685"),
                bought("D", "call", "690"),
            ),
            D("500.00"),
            id="an iron condor with equal wings pledges one wing",
        ),
        # +675C -2x680C +685C is worth zero or more at every price: its loss is its debit, which is not collateral.
        pytest.param(
            book("ABC", bought("A", "call", "675"), sold("B", "call", "680", 2), bought("C", "call", "685")),
            D("0.00"),
            id="a call butterfly pledges nothing",
        ),
        pytest.param(
            book(
                "ABCD",
                bought("A", "put", "675"),
                sold("B", "put", "680"),
                sold("C", "call", "680"),
                bought("D", "call", "685"),
            ),
            D("500.00"),
            id="an iron butterfly pledges its wing",
        ),
        pytest.param(
            book("AB", bought("A", "put", "660"), sold("B", "put", "665")),
            D("500.00"),
            id="a sold put with its bought put pledges the width",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680"), sold("B", "call", "675")),
            D("500.00"),
            id="a sold call with its bought call pledges the width",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680"), bought("B", "put", "660", expiry=LATER)),
            D("0.00"),
            id="bought legs pledge nothing even across expiries",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680", 2), sold("B", "call", "675", 2)),
            D("1000.00"),
            id="two contracts of one spread pledge twice the width",
        ),
    ],
)
def test_the_collateral_a_book_pledges(held: Held, pledged: Decimal) -> None:
    assert collateral(held) == pledged, "the collateral a book pledges"
    assert str(collateral(held)) == str(pledged), "collateral is in whole cents as a decimal"


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


# ---- Cover, as the opening group meets it, and the other refusals -------------


@pytest.mark.parametrize(
    ("held", "candidates", "answer"),
    [
        pytest.param(
            Held.empty("AB"),
            (sell("B", "put", "665"), buy("A", "put", "660")),
            {"A": None, "B": None},
            id="a sold put is covered by a bought put of the same expiry",
        ),
        pytest.param(
            Held.empty("AB"),
            (buy("A", "put", "400"), sell("B", "put", "665")),
            {"A": None, "B": None},
            id="cover does not depend on the strike",
        ),
        pytest.param(
            Held.empty("AB"),
            (buy("A", "call", "680"), sell("B", "call", "675", 2)),
            {"A": None, "B": OpeningRefusal.NO_COVER},
            id="one bought contract does not cover two sold in one leg",
        ),
        pytest.param(
            Held.empty("ABC"),
            (buy("A", "call", "680"), sell("B", "call", "675"), sell("C", "call", "670")),
            {"A": None, "B": None, "C": OpeningRefusal.NO_COVER},
            id="one bought contract does not cover two sold in two legs and the first listed is taken",
        ),
        pytest.param(
            Held.empty("ABC"),
            (sell("C", "call", "670"), sell("B", "call", "675"), buy("A", "call", "680")),
            {"A": None, "B": None, "C": OpeningRefusal.NO_COVER},
            id="candidates are judged in the setup's order whatever order they arrive in",
        ),
        # One bought contract: B wants two and is refused without taking it, so C takes it.
        pytest.param(
            Held.empty("ABC"),
            (buy("A", "call", "680"), sell("B", "call", "675", 2), sell("C", "call", "670")),
            {"A": None, "B": OpeningRefusal.NO_COVER, "C": None},
            id="a sold candidate takes cover whole or not at all so a later one may use what is left",
        ),
        pytest.param(
            Held.empty("AB"),
            (buy("A", "call", "680", 2), sell("B", "call", "675", 2)),
            {"A": None, "B": None},
            id="two bought contracts in one leg cover two sold in one leg",
        ),
        pytest.param(
            Held.empty("AB"),
            (sell("A", "call", "675"), buy("B", "call", "680")),
            {"A": None, "B": None},
            id="bought legs listed after the sold leg still cover it in the same minute",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680")),
            (sell("B", "call", "675"),),
            {"B": None},
            id="a held bought contract covers a new sale",
        ),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (sell("C", "call", "670"),),
            {"C": OpeningRefusal.NO_COVER},
            id="a held bought contract does not cover a new sale once a held sale has used it",
        ),
        pytest.param(
            Held.empty("AB"),
            (buy("A", "call", "680", root="QQQ"), sell("B", "call", "675")),
            {"A": None, "B": OpeningRefusal.NO_COVER},
            id="a bought call on another underlying is not cover for a sold call",
        ),
        # Leg X holds a sold 700 call, covered by Y. Candidate V buys that same contract, which X holds on the other
        # side, so V is refused; W's sale then has only Y's contract, and that is already X's cover.
        pytest.param(
            book("XYVW", sold("X", "call", "700"), bought("Y", "call", "710")),
            (buy("V", "call", "700"), sell("W", "call", "695")),
            {"V": OpeningRefusal.OTHER_SIDE, "W": OpeningRefusal.NO_COVER},
            id="a bought candidate that was refused is not cover",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680")),
            (sell("B", "call", "680"),),
            {"B": OpeningRefusal.OTHER_SIDE},
            id="a candidate in a contract another leg holds on the other side is refused",
        ),
        pytest.param(
            Held.empty("ABC"),
            (buy("A", "call", "680"), buy("B", "call", "680")),
            {"A": None, "B": None},
            id="two legs in one group on one contract add when alike",
        ),
        pytest.param(
            Held.empty("ABC"),
            (buy("A", "call", "680"), sell("B", "call", "680")),
            {"A": None, "B": OpeningRefusal.OTHER_SIDE},
            id="two legs in one group on one contract, the later is refused when opposed",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680")),
            (sell("B", "call", "675", expiry=LATER),),
            {"B": OpeningRefusal.SECOND_EXPIRY},
            id="a sold candidate at another expiry than what is held is refused",
        ),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (buy("C", "call", "700", expiry=LATER),),
            {"C": OpeningRefusal.SECOND_EXPIRY},
            id="a bought candidate at another expiry is refused while a sold contract is held",
        ),
        pytest.param(
            Held.empty("ABC"),
            (buy("A", "call", "680"), sell("B", "call", "675"), buy("C", "call", "700", expiry=LATER)),
            {"A": None, "B": None, "C": OpeningRefusal.SECOND_EXPIRY},
            id="a bought candidate at another expiry is refused beside a sold candidate listed first",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680")),
            (buy("B", "put", "660", expiry=LATER),),
            {"B": None},
            id="bought-only holdings may span expiries",
        ),
    ],
)
def test_an_opening_group_meets_its_cover_and_its_other_refusals(
    held: Held, candidates: tuple[Candidate, ...], answer: dict[str, OpeningRefusal | None]
) -> None:
    assert admission(held, *candidates) == answer


# ---- Pending closes and the cash step -----------------------------------------


@pytest.mark.parametrize(
    ("held", "candidates", "cash", "closing", "answer"),
    [
        pytest.param(
            book("AB", bought("A", "call", "680", 1)),
            (sell("B", "call", "675"),),
            PLENTY,
            (),
            {"B": None},
            id="a bought contract with no close pending is cover for a new sale",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680", 1)),
            (sell("B", "call", "675"),),
            PLENTY,
            ("A",),
            {"B": OpeningRefusal.NO_COVER},
            id="a bought contract whose close is pending is not cover for a new sale",
        ),
        # B is covered by A and is being bought back, but the buy-back fills after the opening group. Until it does,
        # B still sits on A's contract, so a new sale has none to take.
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (sell("C", "call", "670"),),
            PLENTY,
            ("B",),
            {"C": OpeningRefusal.NO_COVER},
            id="a sold leg whose buy-back is pending still uses its cover",
        ),
        # Sell 675C at 6.50 and buy 680C at 3.10, a dollar of fees on each: +649.00 and -311.00, net +338.00.
        # The spread is $5 wide, so $500.00 is pledged and the cash before must be at least 162.00.
        pytest.param(
            Held.empty("AB"),
            (sell("A", "call", "675", cash="649.00"), buy("B", "call", "680", cash="-311.00")),
            D("162.00"),
            (),
            {"A": None, "B": None},
            id="a group that would pledge no more than the cash is taken",
        ),
        pytest.param(
            Held.empty("AB"),
            (sell("A", "call", "675", cash="649.00"), buy("B", "call", "680", cash="-311.00")),
            D("161.99"),
            (),
            {"A": OpeningRefusal.CASH_SHORT, "B": OpeningRefusal.CASH_SHORT},
            id="a group that would pledge more than the cash is refused whole",
        ),
        pytest.param(
            Held.empty("AB"),
            (buy("A", "call", "680", cash="-300.00"), buy("B", "put", "660", cash="-250.00")),
            D("550.00"),
            (),
            {"A": None, "B": None},
            id="for bought legs only the cost not more than the cash is taken",
        ),
        pytest.param(
            Held.empty("AB"),
            (buy("A", "call", "680", cash="-300.00"), buy("B", "put", "660", cash="-250.00")),
            D("549.99"),
            (),
            {"A": OpeningRefusal.CASH_SHORT, "B": OpeningRefusal.CASH_SHORT},
            id="for bought legs only a cost more than the cash is refused",
        ),
        # A $5 spread is held and pledges $500.00. With $400.00 of cash, a $50.00 purchase would leave $350.00.
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (buy("C", "put", "660", cash="-50.00"),),
            D("400.00"),
            (),
            {"C": OpeningRefusal.CASH_SHORT},
            id="collateral already held is not cash a new bought leg may spend",
        ),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (buy("C", "put", "660", cash="-50.00"),),
            D("550.00"),
            (),
            {"C": None},
            id="a new bought leg may spend the cash above the collateral already held",
        ),
        # Holdings pledge $500.00 and the cash is $499.00: even a free purchase leaves the cash under the pledge.
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (buy("C", "put", "660", cash="0"),),
            D("499.00"),
            (),
            {"C": OpeningRefusal.CASH_SHORT},
            id="a bought leg that does not change the collateral is judged against the collateral held",
        ),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675")),
            (buy("C", "put", "660", cash="-50.00"),),
            D("400.00"),
            ("A", "B"),
            {"C": OpeningRefusal.CASH_SHORT},
            id="a pending close is still held and still pledged for the cash step",
        ),
        # B is refused for cover. A's cost alone is then judged against the cash, and fails on its own.
        pytest.param(
            Held.empty("AB"),
            (buy("A", "put", "660", cash="-300.00"), sell("B", "call", "675", cash="649.00")),
            D("100"),
            (),
            {"A": OpeningRefusal.CASH_SHORT, "B": OpeningRefusal.NO_COVER},
            id="the cash step sees only the candidates still standing",
        ),
    ],
)
def test_pending_closes_and_cash_decide_what_an_opening_group_takes(
    held: Held,
    candidates: tuple[Candidate, ...],
    cash: Decimal,
    closing: tuple[str, ...],
    answer: dict[str, OpeningRefusal | None],
) -> None:
    assert admission(held, *candidates, cash=cash, closing=closing) == answer


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


@pytest.mark.parametrize(
    ("held", "leg", "answer"),
    [
        pytest.param(
            book("AB", bought("A", "call", "680"), sold("B", "call", "675")),
            "A",
            False,
            id="a bought leg may not leave when it alone covers a sold leg",
        ),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675"), bought("C", "put", "660")),
            "C",
            True,
            id="a bought leg that protects nothing sold may leave",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680"), bought("B", "put", "660")),
            "A",
            True,
            id="with nothing sold the call may leave",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680"), bought("B", "put", "660")),
            "B",
            True,
            id="with nothing sold the put may leave",
        ),
    ],
)
def test_whether_a_bought_leg_may_leave(held: Held, leg: str, answer: bool) -> None:
    assert may_leave(held, leg) is answer


def test_a_bought_leg_may_not_leave_when_every_sold_contract_stays_covered_but_the_collateral_rises() -> None:
    # Bought 680 and 690 calls, sold 675 call: $500.00 pledged. Without the 680 the 690 still covers, but the
    # spread is then 15 wide and pledges $1,500.00. Without the 690 it is still 5 wide.
    held = book("ABC", bought("A", "call", "680"), bought("B", "call", "690"), sold("C", "call", "675"))

    assert collateral(held) == D("500.00")
    assert may_leave(held, "A") is False
    assert may_leave(held, "B") is True


# ---- Which sold legs go with a leaving bought leg -----------------------------


@pytest.mark.parametrize(
    ("held", "leaving", "plan"),
    [
        # A is two 680 calls; B sells the 685 and C the 690. Without A both are bare, so both go, C first.
        pytest.param(
            book("ABC", bought("A", "call", "680", 2), sold("B", "call", "685"), sold("C", "call", "690")),
            ("A",),
            ("C", "B"),
            id="two sold legs that depend on one leaving leg go last listed first",
        ),
        # The same four legs listed A, D, C, B: the B sale is now last listed and goes first. C against D is then a
        # 15-wide spread of $1,500.00, which is more than before, so C goes too. Closing C alone would have sufficed.
        pytest.param(
            book(
                "ADCB",
                bought("A", "call", "680"),
                bought("D", "call", "690"),
                sold("C", "call", "675"),
                sold("B", "call", "685"),
            ),
            ("A",),
            ("B", "C"),
            id="the plan is a policy by listed order and not the fewest legs",
        ),
        pytest.param(
            book("ABC", bought("A", "call", "680"), sold("B", "call", "675"), bought("C", "put", "660")),
            ("C",),
            (),
            id="a bought leg that protects nothing leaves alone",
        ),
        pytest.param(
            book("AB", bought("A", "put", "660"), sold("B", "put", "665")),
            (),
            (),
            id="nothing leaving from covered holdings closes nothing",
        ),
        pytest.param(
            book("AB", bought("A", "call", "660"), sold("B", "put", "665")),
            (),
            ("B",),
            id="nothing leaving from uncovered holdings closes the bare leg",
        ),
        pytest.param(
            book(
                "ABCD",
                bought("A", "call", "680"),
                sold("B", "call", "675"),
                bought("C", "put", "660"),
                sold("D", "put", "665"),
            ),
            ("A", "C"),
            ("D", "B"),
            id="several legs may leave at once",
        ),
        pytest.param(
            book("AB", bought("A", "call", "680"), bought("B", "put", "660")),
            ("A", "B"),
            (),
            id="holdings with no sold leg need no plan whatever leaves",
        ),
        # Isolated from the expiry step: what is held has the sold put and this cover, and nothing leaves.
        pytest.param(
            book("AB", bought("A", "put", "660", expiry=LATER), sold("B", "put", "665")),
            (),
            ("B",),
            id="a sold put is not covered by a bought put of another expiry",
        ),
        pytest.param(
            book("AB", bought("A", "call", "660"), sold("B", "put", "665")),
            (),
            ("B",),
            id="a sold put is not covered by a bought call",
        ),
    ],
)
def test_the_sold_legs_that_go_with_the_leaving_bought_legs(
    held: Held, leaving: tuple[str, ...], plan: tuple[str, ...]
) -> None:
    assert leaving_plan(held, leaving) == plan


@pytest.mark.parametrize(
    ("held", "pledged_now", "plan"),
    [
        # Listed A, D, B, C. Before: A 680 and D 690 bought, B 685 and C 675 sold: $1,000.00. Without A the C sale goes
        # first (last listed) and B stays against D, a 5-wide spread of $500.00, which is not more.
        pytest.param(
            book(
                "ADBC",
                bought("A", "call", "680"),
                bought("D", "call", "690"),
                sold("B", "call", "685"),
                sold("C", "call", "675"),
            ),
            D("1000.00"),
            ("C",),
            id="the plan stops as soon as what remains is covered with no more collateral",
        ),
        # A is two 680 calls leaving, B sells the 685 call, P1 is two 690 puts and P2 sells the 700 put.
        # Nothing is pledged now. Without A the 685 call is bare; closed, the puts alone pledge $1,000.00, which is
        # more, so the put goes as well. Last listed first alone would have said P2 then B.
        pytest.param(
            book(
                "ABPQ",
                bought("A", "call", "680", 2),
                sold("B", "call", "685"),
                bought("P", "put", "690", 2),
                sold("Q", "put", "700"),
            ),
            D("0.00"),
            ("B", "Q"),
            id="sold legs of the leaving legs' kind go before any other whatever the listing",
        ),
    ],
)
def test_the_plan_is_judged_against_the_collateral_held_before(
    held: Held, pledged_now: Decimal, plan: tuple[str, ...]
) -> None:
    assert collateral(held) == pledged_now, "the collateral pledged now"
    assert leaving_plan(held, ["A"]) == plan, "the sold legs that close with A"


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
