"""The cover book agrees with what already decides these questions, and keeps its own invariants.

Three comparisons, each exact:

* Cover. The bots' cover check (``app/options/structure_check.py``) and the book are run on every structure of a
  small ladder of contracts, up to four legs, with every choice of side and size. They give one answer wherever
  both can express the case: one expiry. Where the bots let a later-lasting bought contract cover a sold one the
  book does not, and the money path refuses that structure anyway, which the last cover test shows.
* Collateral. The book's figure, the canonical portfolio requirement called on the same legs, and an independent
  scan of the summed exercise payoff at every whole dollar agree to the cent over seeded random holdings of up to
  eight contracts and two expiries. Strikes are whole dollars, so the payoff's turning points are whole dollars
  and the scan is exact. The scan is not the requirement's code: it knows nothing of its turning points.
* Invariants. Over many seeded random sessions of opening groups, leaving legs and pending closes, checked after
  every group: nothing sold is ever uncovered, a sold contract and everything held with it share one expiry, cash
  after a taken group is not below the collateral then held, and a bought leg may leave exactly when its plan is
  empty. A failing seed is printed, so it can be replayed.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from itertools import combinations, product

import pytest

from app.options.contract import OptionContract, OptionRight
from app.options.requirement import (
    EntrySize,
    StructureMoneyRefusal,
    StructureMoneyRefused,
    broker_requirement,
    portfolio_broker_requirement,
)
from app.options.structure import ResolvedLeg, ResolvedStructure
from app.options.structure_check import StructureRefusal, StructureRefused, require_sold_legs_covered
from app.options_backtest.cover_book import (
    Candidate,
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
PLENTY = D("1000000000")
_ONE = EntrySize(multiplier=100, units=1)


def _contract(right: OptionRight, strike: int, expiry: date) -> OptionContract:
    return OptionContract.of(root="SPY", expiry_date=expiry, right=right, strike=D(strike))


# ---- An independent reading of what is held ----------------------------------------


def _net_by_kind(holdings: Iterable[Holding]) -> dict[tuple[str, int], int]:
    """Bought less sold contracts for each call or put at each expiry."""
    net: dict[tuple[str, int], int] = defaultdict(int)
    for holding in holdings:
        net[holding.contract.right, holding.contract.expiry_ms] += holding.signed_contracts
    return dict(net)


def _is_covered(holdings: Iterable[Holding]) -> bool:
    return all(count >= 0 for count in _net_by_kind(holdings).values())


def _scanned_collateral_dollars(holdings: Iterable[Holding]) -> int:
    """The requirement in whole dollars by scanning the summed payoff at every whole-dollar price.

    Per expiry: ``100 · max(0, −min over p of Σ q · payoff(p))``; the largest expiry is kept. The scan runs from
    zero to the highest strike and a little beyond. Past it the payoff's slope is the net calls, which the callers
    here hold at zero or more, so nothing lower is out there.
    """
    held = list(holdings)
    if not held:
        return 0
    top = max(holding.contract.strike_thousandths for holding in held) // 1000 + 5
    worst_by_expiry: dict[int, int] = {}
    for expiry in {holding.contract.expiry_ms for holding in held}:
        legs = [holding for holding in held if holding.contract.expiry_ms == expiry]
        lowest = min(
            sum(
                leg.signed_contracts
                * (
                    max(price - leg.contract.strike_thousandths // 1000, 0)
                    if leg.contract.right == "call"
                    else max(leg.contract.strike_thousandths // 1000 - price, 0)
                )
                for leg in legs
            )
            for price in range(0, top + 1)
        )
        worst_by_expiry[expiry] = 100 * max(0, -lowest)
    return max(worst_by_expiry.values())


def test_the_scan_is_right_about_the_cases_worked_by_hand() -> None:
    iron_condor = [
        Holding("A", _contract("put", 660, SOON), "buy", 1, 100),
        Holding("B", _contract("put", 665, SOON), "sell", 1, 100),
        Holding("C", _contract("call", 685, SOON), "sell", 1, 100),
        Holding("D", _contract("call", 695, SOON), "buy", 1, 100),
    ]
    butterfly = [
        Holding("A", _contract("call", 675, SOON), "buy", 1, 100),
        Holding("B", _contract("call", 680, SOON), "sell", 2, 100),
        Holding("C", _contract("call", 685, SOON), "buy", 1, 100),
    ]

    assert _scanned_collateral_dollars(iron_condor) == 1000
    assert _scanned_collateral_dollars(butterfly) == 0
    assert _scanned_collateral_dollars([]) == 0


# ---- Cover, against the bots' check -----------------------------------------------

_LADDER = (("call", 675), ("call", 680), ("call", 685), ("put", 655), ("put", 660), ("put", 665))


def _structures_of_the_ladder() -> Iterable[tuple[tuple[str, int, str, int], ...]]:
    """Every choice of up to four distinct contracts of the ladder, each bought or sold, one or two of it."""
    for size in range(1, 5):
        for chosen in combinations(_LADDER, size):
            for sides, counts in product(product(("long", "short"), repeat=size), product((1, 2), repeat=size)):
                yield tuple(
                    (right, strike, side, count)
                    for (right, strike), side, count in zip(chosen, sides, counts, strict=True)
                )


def _bots_say_covered(structure: ResolvedStructure) -> bool:
    try:
        require_sold_legs_covered(structure)
    except StructureRefused as refused:
        assert refused.reason is StructureRefusal.SOLD_LEG_NOT_COVERED
        return False
    return True


def _opening_group_of(structure: ResolvedStructure) -> tuple[Held, list[Candidate]]:
    candidates = [
        Candidate(
            leg.leg_id,
            leg.contract,
            "buy" if leg.position == "long" else "sell",
            leg.ratio,
            100,
            D("-1") if leg.position == "long" else D("1"),
        )
        for leg in structure.legs
    ]
    return Held.empty(leg.leg_id for leg in structure.legs), candidates


def _held_as_a_structure_is(structure: ResolvedStructure) -> Held:
    """Every leg of the structure held at once, as the bots' check sees it."""
    return Held(
        tuple(leg.leg_id for leg in structure.legs),
        tuple(
            Holding(leg.leg_id, leg.contract, "buy" if leg.position == "long" else "sell", leg.ratio, 100)
            for leg in structure.legs
        ),
    )


def test_the_book_covers_exactly_the_structures_the_bots_cover_where_there_is_one_expiry() -> None:
    covered = uncovered = 0
    for legs in _structures_of_the_ladder():
        structure = ResolvedStructure(
            legs=tuple(
                ResolvedLeg(leg_id=f"L{place}", contract=_contract(right, strike, SOON), position=side, ratio=count)
                for place, (right, strike, side, count) in enumerate(legs)
            )
        )
        held, candidates = _opening_group_of(structure)

        admission = admit_opening(held, candidates, cash=PLENTY, closing=[])

        assert {refused.reason for refused in admission.refused} <= {OpeningRefusal.NO_COVER}, legs
        assert (not admission.refused) == _bots_say_covered(structure), legs
        assert is_covered(_held_as_a_structure_is(structure)) == _bots_say_covered(structure), legs
        covered += not admission.refused
        uncovered += bool(admission.refused)
    # Neither answer is the only one the sweep met, so agreement is not agreement on a constant.
    assert covered > 1000
    assert uncovered > 1000


def test_the_count_of_sold_contracts_covered_is_the_most_the_bought_contracts_allow() -> None:
    for legs in _structures_of_the_ladder():
        structure = ResolvedStructure(
            legs=tuple(
                ResolvedLeg(leg_id=f"L{place}", contract=_contract(right, strike, SOON), position=side, ratio=count)
                for place, (right, strike, side, count) in enumerate(legs)
            )
        )
        held, candidates = _opening_group_of(structure)

        taken = {candidate.leg_id for candidate in admit_opening(held, candidates, cash=PLENTY, closing=[]).taken}

        for right in ("call", "put"):
            bought = sum(leg.ratio for leg in structure.legs if leg.position == "long" and leg.contract.right == right)
            sold_taken = sum(
                leg.ratio
                for leg in structure.legs
                if leg.position == "short" and leg.contract.right == right and leg.leg_id in taken
            )
            # Never more sold contracts taken than bought contracts; and a refused sold leg did not fit in what
            # was left, so what was left was less than its contracts.
            assert sold_taken <= bought, legs
            for leg in structure.legs:
                if leg.position == "short" and leg.contract.right == right and leg.leg_id not in taken:
                    assert bought - sold_taken < leg.ratio, legs


def test_where_the_bots_let_a_later_bought_contract_cover_the_book_does_not_and_the_money_path_refuses_it_too() -> None:
    """The one place the two cover rules differ, and why it is not a case anything trades."""
    differing = 0
    for first, second in product(_LADDER, repeat=2):
        for later_side in ("long", "short"):
            if first[0] != second[0]:
                continue
            structure = ResolvedStructure(
                legs=(
                    ResolvedLeg(
                        leg_id="near",
                        contract=_contract(first[0], first[1], SOON),
                        position="short" if later_side == "long" else "long",
                        ratio=1,
                    ),
                    ResolvedLeg(
                        leg_id="far", contract=_contract(second[0], second[1], LATER), position=later_side, ratio=1
                    ),
                )
            )
            held, candidates = _opening_group_of(structure)

            admission = admit_opening(held, candidates, cash=PLENTY, closing=[])

            if structure.has_sold_leg:
                assert admission.refused, "two expiries and a sold leg are never taken whole"
            if _bots_say_covered(structure) and structure.has_sold_leg:
                differing += 1
                bought_leg = next(leg for leg in structure.legs if leg.position == "long")
                sold_leg = next(leg for leg in structure.legs if leg.position == "short")
                assert not covers(bought_leg.contract, sold_leg.contract)
                assert not is_covered(_held_as_a_structure_is(structure))
                with pytest.raises(StructureMoneyRefused) as refused:
                    broker_requirement(structure, _ONE)
                assert refused.value.reason is StructureMoneyRefusal.MIXED_EXPIRY_WITH_SOLD_LEG
    assert differing > 0


# ---- Collateral, against the canonical requirement and a scan ---------------------

_CALL_STRIKES = (670, 675, 680, 685, 690)
_PUT_STRIKES = (650, 655, 660, 665, 670)


def _random_holdings(rng: random.Random) -> list[Holding]:
    """Up to eight distinct contracts at one or two expiries; no sold call is left bare, a sold put may be."""
    universe = [
        (right, strike, expiry)
        for expiry in (SOON, LATER)
        for right, strikes in (("call", _CALL_STRIKES), ("put", _PUT_STRIKES))
        for strike in strikes
    ]
    chosen = rng.sample(universe, k=rng.randint(1, 8))
    return [
        Holding(
            f"L{place}",
            _contract(right, strike, expiry),
            rng.choice(("buy", "sell")),
            rng.randint(1, 3),
            100,
        )
        for place, (right, strike, expiry) in enumerate(chosen)
    ]


def _covered_in_calls(holdings: Iterable[Holding]) -> bool:
    return all(count >= 0 for (right, _expiry), count in _net_by_kind(holdings).items() if right == "call")


def test_collateral_agrees_with_the_canonical_requirement_and_a_scan_to_the_cent() -> None:
    rng = random.Random(3104)
    judged = nonzero = 0
    for _ in range(1500):
        holdings = _random_holdings(rng)
        if not _covered_in_calls(holdings):
            continue
        held = Held(tuple(holding.leg_id for holding in holdings), tuple(holdings))
        canonical = portfolio_broker_requirement(
            tuple(
                ResolvedLeg(
                    leg_id=holding.leg_id,
                    contract=holding.contract,
                    position="long" if holding.side == "buy" else "short",
                    ratio=holding.contracts,
                )
                for holding in holdings
            ),
            _ONE,
        )

        figure = collateral(held)

        assert figure == D(canonical) / 100, holdings
        assert figure == D(_scanned_collateral_dollars(holdings)), holdings
        judged += 1
        nonzero += figure > 0
    assert judged > 500
    assert nonzero > 200


def test_a_book_of_up_to_eight_legs_is_asked_whole() -> None:
    """Eight held legs are beyond one order's four, and the book never splits them.

    Condor one: puts 640/650 ($10 wide), calls 700/705 ($5). Condor two: puts 660/665 ($5), calls 710/720 ($10).
    Each condor alone pledges $1,000, so a sum of two groups would say $2,000. Together the puts' worst case is -15
    at or below 640 and the calls' is -15 at or above 720, and they never coincide: $1,500.00.
    """
    holdings = [
        Holding("A", _contract("put", 640, SOON), "buy", 1, 100),
        Holding("B", _contract("put", 650, SOON), "sell", 1, 100),
        Holding("C", _contract("call", 700, SOON), "sell", 1, 100),
        Holding("D", _contract("call", 705, SOON), "buy", 1, 100),
        Holding("E", _contract("put", 660, SOON), "buy", 1, 100),
        Holding("F", _contract("put", 665, SOON), "sell", 1, 100),
        Holding("G", _contract("call", 710, SOON), "sell", 1, 100),
        Holding("H", _contract("call", 720, SOON), "buy", 1, 100),
    ]
    held = Held(tuple("ABCDEFGH"), tuple(holdings))

    assert collateral(held) == D("1500.00")
    assert _scanned_collateral_dollars(holdings) == 1500


def test_an_obligation_is_the_sum_of_what_sold_contracts_could_deliver() -> None:
    rng = random.Random(3105)
    for _ in range(300):
        holdings = _random_holdings(rng)
        held = Held(tuple(holding.leg_id for holding in holdings), tuple(holdings))

        expected = sum(
            holding.contracts * (holding.contract.strike_thousandths // 1000) * 100
            for holding in holdings
            if holding.side == "sell"
        )

        assert assignment_obligation(held) == D(expected), holdings


# ---- Invariants over seeded random sessions ---------------------------------------


def _random_candidate(rng: random.Random, leg_id: str) -> Candidate:
    right: OptionRight = rng.choice(("call", "put"))
    strike = rng.choice(_CALL_STRIKES if right == "call" else _PUT_STRIKES)
    expiry = SOON if rng.random() < 0.8 else LATER
    contracts = rng.randint(1, 2)
    if rng.random() < 0.55:
        return Candidate(leg_id, _contract(right, strike, expiry), "buy", contracts, 100, -D(rng.randint(0, 400)))
    return Candidate(leg_id, _contract(right, strike, expiry), "sell", contracts, 100, D(rng.randint(0, 700)))


def _run_session(seed: int, seen: Counter[str]) -> None:
    rng = random.Random(seed)
    legs = [f"L{place}" for place in range(rng.randint(2, 8))]
    held = Held.empty(legs)
    cash = D(rng.randint(0, 5000))
    where = f"seed {seed}"
    for _ in range(rng.randint(2, 6)):
        idle = [leg for leg in legs if held.holding_of(leg) is None]
        group = rng.sample(idle, k=rng.randint(1, len(idle))) if idle else []
        candidates = [_random_candidate(rng, leg) for leg in group]
        # Any held leg may have a close pending, a sold leg's buy-back included.
        held_legs = [holding.leg_id for holding in held.holdings]
        closing = rng.sample(held_legs, k=rng.randint(0, len(held_legs)))

        admission = admit_opening(held, candidates, cash=cash, closing=closing)

        taken_ids = [candidate.leg_id for candidate in admission.taken]
        refused_ids = [refused.candidate.leg_id for refused in admission.refused]
        assert sorted(taken_ids + refused_ids) == sorted(group), where
        for refused in admission.refused:
            seen[refused.reason.value] += 1
        after = held.opened(admission.taken)

        assert _is_covered(after.holdings), where
        assert is_covered(after), where
        if any(holding.side == "sell" for holding in after.holdings):
            assert len({holding.contract.expiry_ms for holding in after.holdings}) == 1, where
        if admission.taken:
            seen["taken"] += 1
            seen["taken_sold"] += any(candidate.side == "sell" for candidate in admission.taken)
            cash_after = cash + sum((candidate.cash_effect for candidate in admission.taken), start=D(0))
            assert cash_after >= D(_scanned_collateral_dollars(after.holdings)), where
            cash = cash_after
        if OpeningRefusal.CASH_SHORT in {refused.reason for refused in admission.refused}:
            assert not admission.taken, where
        held = after

        # A bought leg may leave exactly when it needs no sold leg to go with it, and a plan leaves what
        # remains covered and pledging no more.
        bought_now = [holding.leg_id for holding in held.holdings if holding.side == "buy"]
        for leg_id in bought_now:
            assert may_leave(held, leg_id) == (leaving_plan(held, [leg_id]) == ()), where
        if bought_now:
            leaver = rng.choice(bought_now)
            before = collateral(held)
            plan = leaving_plan(held, [leaver])
            remaining = held.without([leaver, *plan])
            assert _is_covered(remaining.holdings), where
            assert is_covered(remaining), where
            assert collateral(remaining) <= before, where
            seen["plans"] += 1
            seen["plans_nonempty"] += bool(plan)
            held = remaining


def test_invariants_hold_after_every_group_over_seeded_random_sessions() -> None:
    seen: Counter[str] = Counter()
    for seed in range(400):
        _run_session(seed, seen)

    # The generator reached each branch; a sweep that never refused would prove nothing.
    for branch in ("other_side", "second_expiry", "no_cover", "cash_short", "taken", "taken_sold", "plans_nonempty"):
        assert seen[branch] > 20, (branch, dict(seen))
