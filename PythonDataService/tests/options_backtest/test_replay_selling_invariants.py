"""What must hold of every session that sells, over many generated sessions (#3106).

A scene is worked by hand, and a scene proves the case its author thought of. This one generates sessions with legs of
different contract counts, a cover that did not trade in the minute it was to fill, a sold contract that stops trading
before its buy-back, sales that round to nothing, and legs on two expiries, and asserts what the owner may rely on of any
of them. Each outcome is read back from its own fills and money steps, and nothing is taken from the replay's state:

* after every fill group, in every kind of contract (underlying, call or put, actual expiry), what is bought is at least
  what is sold, and while anything is sold everything held shares one expiry;
* after an opening group the cash is not less than the collateral held, which is the canonical requirement of the
  holdings rebuilt from the fills;
* a profit is the cash that moved plus the signed marks, to the cent.

The seeds are fixed, so a failure names the seed that reproduces it.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.options.contract import OptionContract
from app.options_backtest.cover_book import Held, Holding, collateral
from app.options_backtest.results import FillGroup, LegTrade, SessionOutcome, SessionStatus
from tests.options_backtest._market import D, TableMarket, at, fee_per_contract, leg, picked, replay, setup, steady

SEEDS = range(150)
SOON, LATER = date(2026, 10, 9), date(2026, 10, 16)
STRIKES = (490, 495, 500, 505, 510)
LAST_MINUTE_OF_BARS = 90
EXIT_MINUTE = 60


@dataclass(frozen=True)
class _Scene:
    written: Any
    market: TableMarket
    cash: str
    fee: str
    slippage: str


def _contracts() -> list[OptionContract]:
    return [
        OptionContract.of(root="SPY", expiry_date=expiry, right=right, strike=D(strike))
        for expiry in (SOON, LATER)
        for right in ("call", "put")
        for strike in STRIKES
    ]


def _a_scene(seed: int) -> _Scene | None:
    rng = random.Random(seed)
    contracts = _contracts()
    count = rng.randint(2, 5)
    legs: list[dict[str, Any]] = []
    picks: list[tuple[str, str, OptionContract]] = []
    uncovered = {"call": 0, "put": 0}  # contracts bought and not yet sold against, by kind
    for number in range(count):
        leg_id = f"leg{number}"
        right = rng.choice(("call", "put"))
        # A leg sells only what the bought legs before it can cover, so that the setup is a valid one, and the replay is
        # left to find where the picked contracts do not cover each other after all.
        side = "sell" if uncovered[right] and rng.random() < 0.5 else "buy"
        size = rng.randint(1, uncovered[right]) if side == "sell" else rng.randint(1, 3)
        uncovered[right] += size if side == "buy" else -size
        written = leg(leg_id, right, contracts=size, side=side)  # type: ignore[arg-type]
        if rng.random() < 0.3:
            written["momentum"] = {"kind": rng.choice(("percent_up", "percent_down")), "value": "10"}
        legs.append(written)
        # Mostly the nearer expiry, so that covers happen; sometimes the later one, so that expiries differ.
        expiry = SOON if rng.random() < 0.8 else LATER
        picks.append(
            (leg_id, "500", rng.choice([c for c in contracts if c.right == right and c.expiry_date == expiry]))
        )
    # The order the legs are listed in decides who takes cover first, so it is not the order they were made in.
    order = list(range(count))
    rng.shuffle(order)
    legs, picks = [legs[at_] for at_ in order], [picks[at_] for at_ in order]
    try:
        written_setup = setup(*legs, slippage=rng.choice(("0", "0.3", "1")))
    except ValidationError:
        return None  # not reached by construction: every sale is of what the legs before it bought
    options: dict[OptionContract, dict[int, tuple[str, ...]]] = {}
    for _, _, contract_picked in picks:
        if contract_picked in options:
            continue
        start = round(rng.uniform(0.01, 4.0), 2)
        bars: dict[int, tuple[str, ...]] = {}
        last = rng.choice((LAST_MINUTE_OF_BARS, LAST_MINUTE_OF_BARS, 62, EXIT_MINUTE - 5))
        for minute in range(5, last + 1):
            # A bar is missing now and then, at the fill minutes and the closing ones as much as any other.
            if rng.random() < 0.85:
                move = round(rng.uniform(-0.3, 0.3), 2)
                start = max(0.01, round(start + move, 2))
                bars[minute] = (f"{start:.2f}", f"{max(0.01, round(start + move / 2, 2)):.2f}")
        options[contract_picked] = bars
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(*[(leg_id, price, contract_picked) for leg_id, price, contract_picked in picks]),
        options=options,
    )
    return _Scene(
        written_setup, market, cash=rng.choice(("300", "1500", "10000")), fee=rng.choice(("0", "0.65")), slippage="0"
    )


def _entry_minute(trade: LegTrade) -> int:
    return (trade.entry.bar_close_ms - at(0)) // 60_000


def _exit_minute(trade: LegTrade) -> int | None:
    from app.options_backtest.results import Fill

    return (trade.exit.bar_close_ms - at(0)) // 60_000 if isinstance(trade.exit, Fill) else None


def _held_after(trades: tuple[LegTrade, ...], minute: int, group: FillGroup) -> list[LegTrade]:
    """The trades whose positions are held once ``group`` of ``minute`` has filled, rebuilt from the fills alone."""
    held = []
    for trade in trades:
        if _entry_minute(trade) > minute:
            continue
        left = _exit_minute(trade)
        gone = left is not None and (left < minute or (left == minute and group is FillGroup.CLOSING))
        if not gone:
            held.append(trade)
    return held


def _assert_covered_and_one_expiry(held: list[LegTrade], where: str) -> None:
    net: dict[tuple[str, str, int], int] = defaultdict(int)
    for trade in held:
        contract = trade.contract
        net[(contract.root, contract.right, contract.expiry_ms)] += (
            trade.contracts if trade.entry.side == "buy" else -trade.contracts
        )
    assert all(count >= 0 for count in net.values()), f"{where}: a sold contract is uncovered: {dict(net)}"
    if any(trade.entry.side == "sell" for trade in held):
        assert len({trade.contract.expiry_ms for trade in held}) <= 1, f"{where}: a sold contract beside two expiries"


def _the_book_of(held: list[LegTrade], listed: list[str]) -> Held:
    return Held(
        tuple(listed),
        tuple(Holding(t.leg_id, t.contract, t.entry.side, t.contracts, t.multiplier) for t in held),
    )


def _the_session_holds_what_it_says(outcome: SessionOutcome, written: Any, seed: int) -> None:
    listed = [each.leg_id for each in written.legs]
    # A leg that re-enters holds one contract at a time, and nothing here re-enters.
    assert all(trade.entry_number == 0 for trade in outcome.trades), f"seed {seed}"
    for step in outcome.money_steps:
        minute = (step.at_ms - at(0)) // 60_000
        held = _held_after(outcome.trades, minute, step.group)
        where = f"seed {seed}, minute {minute}, {step.group.value} group"
        _assert_covered_and_one_expiry(held, where)
        book = _the_book_of(held, listed)
        assert step.collateral_after_usd == collateral(book), f"{where}: the collateral is not the requirement held"
        if step.group is FillGroup.OPENING:
            assert step.cash_after_usd >= step.collateral_after_usd, f"{where}: the cash is under the collateral"
    # The profit is the cash that moved and the marks, and a mark is held to the end.
    assert outcome.pnl_usd == outcome.cash_change_usd + outcome.marked_value_usd, f"seed {seed}"
    if outcome.money_steps:
        assert (
            outcome.money_steps[-1].cash_after_usd - outcome.money_steps[0].cash_before_usd == outcome.cash_change_usd
        )
    assert outcome.status is (
        SessionStatus.INCOMPLETE
        if any(trade.marked for trade in outcome.trades)
        else (SessionStatus.TRADED if outcome.trades else SessionStatus.NO_TRADE)
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_whatever_a_generated_session_does_it_is_never_uncovered_and_its_money_steps_are_what_was_held(
    seed: int,
) -> None:
    scene = _a_scene(seed)
    if scene is None:
        pytest.skip("the generated setup sells more of a kind than it buys")

    outcome = replay(scene.written, scene.market, cash=scene.cash, fee_for=fee_per_contract(scene.fee))

    _the_session_holds_what_it_says(outcome, scene.written, seed)


def test_the_generator_reaches_the_hard_cases_it_is_there_for() -> None:
    reached = defaultdict(int)
    for seed in SEEDS:
        scene = _a_scene(seed)
        if scene is None:
            continue
        outcome = replay(scene.written, scene.market, cash=scene.cash, fee_for=fee_per_contract(scene.fee))
        for event in outcome.events:
            reached[event.kind.value] += 1
        reached["sold leg owed"] += any(t.exit_reason.value == "marked_not_bought_back" for t in outcome.trades)
        reached["sold leg bought back"] += any(t.is_sold_leg and not t.marked for t in outcome.trades)
        reached["a cover that waited"] += any(
            (step.group is FillGroup.CLOSING and (step.at_ms - at(0)) // 60_000 > EXIT_MINUTE + 1)
            for step in outcome.money_steps
        )

    for hard in (
        "entry_refused_for_cover",
        "entry_refused_for_second_expiry",
        "entry_refused_for_other_side",
        "entry_refused_for_cash",
        "entry_dropped",
        "sold leg owed",
        "sold leg bought back",
        "a cover that waited",
    ):
        assert reached[hard] > 0, f"no generated session reached {hard}: {dict(reached)}"
