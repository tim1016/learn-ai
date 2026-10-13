"""What must hold of every session that sells, over many generated sessions (#3106, and #3107 for stops).

A scene is worked by hand, and a scene proves the case its author thought of. This one generates sessions with legs of
different contract counts, a cover that did not trade in the minute it was to fill, a sold contract that stops trading
before its buy-back, sales that round to nothing, and legs on two expiries, and asserts what the owner may rely on of any
of them. Each outcome is read back from its own fills and money steps, and nothing is taken from the replay's state:

* after every fill group, pending closes included, in every kind of contract (underlying, call or put, actual expiry),
  what is bought is at least what is sold, and while anything is sold everything held shares one expiry;
* after an opening group the cash is not less than the collateral held, which is the canonical requirement of the
  holdings rebuilt from the fills;
* after every fill group the collateral the money step states is that requirement;
* a profit is the cash that moved plus the signed marks, to the cent.

The second family of sessions (``_a_scene_with_stops``) is built for what #3107 adds: stops on bought and sold legs,
re-entries, a cutoff, both square-offs, a second contract for a re-entry to pick, and premiums that jump, so that a stop
is read far from where its leg fills. The last test holds it to reaching the hard cases it is there for.

The seeds are fixed, so a failure names the seed that reproduces it.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from app.options.contract import OptionContract
from app.options_backtest.cover_book import Held, Holding, collateral
from app.options_backtest.results import EventKind, ExitReason, Fill, FillGroup, LegTrade, SessionOutcome, SessionStatus
from tests.options_backtest._market import (
    D,
    TableMarket,
    after_open,
    at,
    fee_per_contract,
    leg,
    picked,
    replay,
    setup,
    steady,
)

SEEDS = range(150)
# The sessions with stops (#3107). Every pull request runs a sample of them: the three seeds that between them reach
# every hard case (taken from the 250 by choosing, again and again, the seed that adds the most cases not yet reached),
# and every tenth. The other seeds are the same test marked slow, which the daily run runs.
RULE_SEEDS = range(250)
RULE_SAMPLE = tuple(sorted({1, 96, 161, *range(0, 250, 10)}))
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


def _a_walk(rng: random.Random) -> dict[int, tuple[str, ...]]:
    """One contract's bars: a random walk from minute 5 with a minute missing now and then, ending early sometimes."""
    start = round(rng.uniform(0.01, 4.0), 2)
    bars: dict[int, tuple[str, ...]] = {}
    last = rng.choice((LAST_MINUTE_OF_BARS, LAST_MINUTE_OF_BARS, 62, EXIT_MINUTE - 5))
    for minute in range(5, last + 1):
        # A bar is missing now and then, at the fill minutes and the closing ones as much as any other.
        if rng.random() < 0.85:
            move = round(rng.uniform(-0.3, 0.3), 2)
            start = max(0.01, round(start + move, 2))
            bars[minute] = (f"{start:.2f}", f"{max(0.01, round(start + move / 2, 2)):.2f}")
    return bars


def _jumped(rng: random.Random, bars: dict[int, tuple[str, ...]]) -> dict[int, tuple[str, ...]]:
    """A contract that, four times in ten, jumps at some minute and goes on from there: so a stop is read at one price
    and its leg fills at another, far from it."""
    if rng.random() >= 0.4:
        return bars
    at_minute, factor = rng.randint(8, 45), rng.choice((0.35, 0.6, 1.8, 3.0))
    return {
        minute: prices
        if minute < at_minute
        else tuple(f"{max(0.01, round(float(price) * factor, 2)):.2f}" for price in prices)
        for minute, prices in bars.items()
    }


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
        options[contract_picked] = _a_walk(rng)
    market = TableMarket(
        underlying=steady("500"),
        picks=picked(*[(leg_id, price, contract_picked) for leg_id, price, contract_picked in picks]),
        options=options,
    )
    return _Scene(
        written_setup, market, cash=rng.choice(("300", "1500", "10000")), fee=rng.choice(("0", "0.65")), slippage="0"
    )


def _a_scene_with_stops(seed: int) -> _Scene:
    """A session built for what #3107 adds: each kind is some bought legs and sold legs that they cover, most legs carry
    a stop, many a re-entry, and the premiums jump now and then so that a stop is read far from where its leg fills.

    The underlying is at 500 until minute 14 and at 498 after it, so that a re-entry picks another contract than the first
    entry did. A sold leg's re-entry rule is kept only where something can close the leg, as the setup demands.
    """
    rng = random.Random(f"stops|{seed}")
    contracts = _contracts()
    legs: list[dict[str, Any]] = []
    picks: list[tuple[str, str, OptionContract]] = []
    again: list[tuple[str, str, OptionContract]] = []
    for right in rng.sample(("call", "put"), rng.choice((1, 1, 2))):
        # At most two bought and two sold legs of a kind, so that two kinds stay within the eight legs of a setup.
        bought = [rng.randint(1, 3) for _ in range(rng.randint(1, 2))]
        room, sold = sum(bought), []
        while room and len(sold) < 2 and rng.random() < 0.85:
            sold.append(rng.randint(1, room))
            room -= sold[-1]
        for side, size in [*(("buy", size) for size in bought), *(("sell", size) for size in sold)]:
            leg_id = f"leg{len(legs)}"
            written = leg(leg_id, right, contracts=size, side=side)  # type: ignore[arg-type]
            if rng.random() < 0.6:
                if rng.random() < 0.7:
                    # A sold leg's percent stop has no ceiling at 100, and a bought leg's is under it.
                    value = rng.choice(("25", "60", "150") if side == "sell" else ("10", "25", "40"))
                    written["stop_loss"] = {"kind": "percent", "value": value}
                else:
                    written["stop_loss"] = {"kind": "points", "value": rng.choice(("0.10", "0.50"))}
            if rng.random() < 0.1:
                written["momentum"] = {"kind": rng.choice(("percent_up", "percent_down")), "value": "10"}
            if rng.random() < 0.45:
                kind = "on_momentum" if "momentum" in written and rng.random() < 0.5 else "at_once"
                written["reentry_on_stop"] = {"kind": kind, "count": rng.randint(1, 3)}
            legs.append(written)
            expiry = SOON if rng.random() < 0.9 else LATER
            first = rng.choice([c for c in contracts if c.right == right and c.expiry_date == expiry])
            second = rng.choice([c for c in contracts if c.right == right and c.expiry_date == expiry])
            picks.append((leg_id, "500", first))
            again.append((leg_id, "498", second))
    order = list(range(len(legs)))
    rng.shuffle(order)
    legs, picks, again = [legs[at_] for at_ in order], [picks[at_] for at_ in order], [again[at_] for at_ in order]
    kept = {
        "square_off": rng.choice(("partial", "partial", "complete")),
        "slippage": rng.choice(("0", "0.3", "1")),
    }
    if rng.random() < 0.35:
        kept["no_reentry_after"] = after_open(rng.choice((8, 12, 20, 30)))
    # Ask the setup itself which legs something can close, with their re-entry rules off, and keep the rules there.
    without = [{key: value for key, value in written.items() if key != "reentry_on_stop"} for written in legs]
    plain = setup(*without, **kept)
    reachable = {each.leg_id for each in plain.legs if plain.stop_can_close(each)}
    for written in legs:
        if written["leg_id"] not in reachable:
            written.pop("reentry_on_stop", None)
    written_setup = setup(*legs, **kept)
    options: dict[OptionContract, dict[int, tuple[str, ...]]] = {}
    for _, _, contract_picked in [*picks, *again]:
        if contract_picked not in options:
            options[contract_picked] = _jumped(rng, _a_walk(rng))
    market = TableMarket(
        underlying={**steady("500", 1, 14), **steady("498", 15)},
        picks=picked(*[*picks, *again]),
        options=options,
    )
    return _Scene(
        written_setup,
        market,
        cash=rng.choice(("300", "1500", "10000", "10000")),
        fee=rng.choice(("0", "0.65")),
        slippage="0",
    )


def _entry_minute(trade: LegTrade) -> int:
    return (trade.entry.bar_close_ms - at(0)) // 60_000


def _exit_minute(trade: LegTrade) -> int | None:
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


def _the_session_holds_what_it_says(
    outcome: SessionOutcome, written: Any, seed: int, *, re_entries: bool = False
) -> None:
    listed = [each.leg_id for each in written.legs]
    # A leg that re-enters holds one contract at a time. The first family's sessions have no rule that re-enters.
    if not re_entries:
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


# ---- Stops, protection and re-entry on sold legs (#3107) --------------------------------


def _the_stops_and_protections_are_what_they_say(outcome: SessionOutcome, written: Any, seed: int) -> None:
    sold_legs = {each.leg_id for each in written.legs if each.side == "sell"}
    bought_stops = {
        event.at_ms for event in outcome.events if event.kind is EventKind.STOP_HIT and event.leg_id not in sold_legs
    }
    for trade in outcome.trades:
        where = f"seed {seed}, leg {trade.leg_id}, entry {trade.entry_number}"
        if trade.stop_level is not None:
            # A sold leg's stop is above what it was sold for and a bought leg's is below what it paid.
            if trade.is_sold_leg:
                assert trade.stop_level > trade.entry.filled, f"{where}: a sold leg's stop is not above its sale"
            else:
                assert trade.stop_level < trade.entry.filled, f"{where}: a bought leg's stop is not below its cost"
        if isinstance(trade.exit, Fill):
            assert trade.exit.side == ("buy" if trade.is_sold_leg else "sell"), (
                f"{where}: the exit is on the wrong side"
            )
            assert trade.exit.bar_close_ms > trade.exit_decided_at_ms, (
                f"{where}: it filled in the minute it was decided"
            )
        if trade.exit_reason is ExitReason.CLOSED_WITH_ITS_PROTECTION:
            assert trade.is_sold_leg, f"{where}: a bought leg was closed with its protection"
            assert trade.exit_decided_at_ms in bought_stops, f"{where}: no bought leg was stopped when it was decided"
    for each in written.legs:
        taken = sum(1 for trade in outcome.trades if trade.leg_id == each.leg_id)
        allowed = 1 + (each.reentry_on_stop.count if each.reentry_on_stop is not None else 0)
        assert taken <= allowed, f"seed {seed}, leg {each.leg_id}: {taken} entries when {allowed} are allowed"


def _the_sample_every_pr_and_the_rest_daily(seeds: Iterable[int], sample: Iterable[int]) -> list[Any]:
    """The seeds as parameters: those of the sample run in every pull request, the others are marked slow."""
    in_every_pr = set(sample)
    return [seed if seed in in_every_pr else pytest.param(seed, marks=pytest.mark.slow) for seed in seeds]


@pytest.mark.parametrize("seed", _the_sample_every_pr_and_the_rest_daily(RULE_SEEDS, RULE_SAMPLE))
def test_whatever_a_generated_session_with_stops_and_re_entries_does_it_is_never_uncovered_and_is_what_was_held(
    seed: int,
) -> None:
    scene = _a_scene_with_stops(seed)

    outcome = replay(scene.written, scene.market, cash=scene.cash, fee_for=fee_per_contract(scene.fee))

    _the_session_holds_what_it_says(outcome, scene.written, seed, re_entries=True)
    _the_stops_and_protections_are_what_they_say(outcome, scene.written, seed)


def _hard_cases_reached_by(seed: int) -> set[str]:
    """Which of the hard cases the session of this seed reaches."""
    scene = _a_scene_with_stops(seed)
    outcome = replay(scene.written, scene.market, cash=scene.cash, fee_for=fee_per_contract(scene.fee))
    sold = {each.leg_id for each in scene.written.legs if each.side == "sell"}
    by_leg = {each.leg_id: each for each in scene.written.legs}
    stops = [event for event in outcome.events if event.kind is EventKind.STOP_HIT]
    reached = {
        kind
        for kind in ("stop_hit", "closed_with_its_protection", "re_entry_earned", "re_entry_blocked_by_cutoff")
        if any(event.kind.value == kind for event in outcome.events)
    }
    cases = {
        "a sold leg stopped by its own stop": any(event.leg_id in sold for event in stops),
        "a sold leg's re-entry filled": any(t.is_sold_leg and t.entry_number > 0 for t in outcome.trades),
        "a sold leg's re-entry refused for cover": any(
            event.kind is EventKind.ENTRY_REFUSED_FOR_COVER and event.leg_id in sold and event.entry_number > 0
            for event in outcome.events
        ),
        "a cutoff blocked a sold leg": any(
            event.kind is EventKind.RE_ENTRY_BLOCKED_BY_CUTOFF and event.leg_id in sold for event in outcome.events
        ),
        "a bought and a sold leg stopped in one minute": any(
            {event.leg_id in sold for event in stops if event.at_ms == minute} == {True, False}
            for minute in {event.at_ms for event in stops}
        ),
        "a close that waited for a bar": any(
            isinstance(t.exit, Fill) and t.exit.bar_close_ms - t.exit_decided_at_ms > 60_000 for t in outcome.trades
        ),
        "a protection that waited for a bar": any(
            t.exit_reason is ExitReason.CLOSED_WITH_ITS_PROTECTION
            and isinstance(t.exit, Fill)
            and t.exit.bar_close_ms - t.exit_decided_at_ms > 60_000
            for t in outcome.trades
        ),
        "a fill far from the level the stop was read at": any(
            t.exit_reason is ExitReason.STOP
            and t.stop_level is not None
            and abs(t.exit_price - t.stop_level) >= t.stop_level / 2
            for t in outcome.trades
        ),
        "sold and bought legs of different contract counts": any(
            each.side == "sell"
            and any(
                other.side == "buy" and other.right == each.right and other.contracts != each.contracts
                for other in by_leg.values()
            )
            for each in by_leg.values()
        ),
        "a complete square-off": scene.written.square_off == "complete"
        and any(t.exit_reason is ExitReason.SQUARE_OFF for t in outcome.trades),
    }
    return reached | {case for case, reached_it in cases.items() if reached_it}


def _what_the_sessions_with_stops_reach(seeds: Iterable[int]) -> dict[str, int]:
    """How many of the sessions of these seeds reach each hard case."""
    reached: dict[str, int] = defaultdict(int)
    for seed in seeds:
        for case in _hard_cases_reached_by(seed):
            reached[case] += 1
    return dict(reached)


def _short_of(reached: dict[str, int], floors: dict[str, int]) -> dict[str, tuple[int, int]]:
    """Each case that fewer sessions reached than its floor, as ``(reached, floor)``."""
    return {case: (reached.get(case, 0), floor) for case, floor in floors.items() if reached.get(case, 0) < floor}


# The least number of the sample's sessions that must reach each case, a little under what they reach. A change to the
# generator that stopped it reaching one would leave the invariants above passing and proving less.
_THE_SAMPLE_REACHES_AT_LEAST: dict[str, int] = {
    "stop_hit": 10,
    "closed_with_its_protection": 3,
    "re_entry_earned": 6,
    "re_entry_blocked_by_cutoff": 2,
    "a sold leg stopped by its own stop": 3,
    "a sold leg's re-entry filled": 1,
    "a sold leg's re-entry refused for cover": 2,
    "a cutoff blocked a sold leg": 2,
    "a bought and a sold leg stopped in one minute": 1,
    "a close that waited for a bar": 8,
    "a protection that waited for a bar": 2,
    "a fill far from the level the stop was read at": 3,
    "sold and bought legs of different contract counts": 12,
    "a complete square-off": 3,
}

# The same for all 250 seeds, which the daily run runs.
_THE_FAMILY_REACHES_AT_LEAST: dict[str, int] = {
    "stop_hit": 90,
    "closed_with_its_protection": 20,
    "re_entry_earned": 50,
    "re_entry_blocked_by_cutoff": 8,
    "a sold leg stopped by its own stop": 18,
    "a sold leg's re-entry filled": 8,
    "a sold leg's re-entry refused for cover": 8,
    "a cutoff blocked a sold leg": 3,
    "a bought and a sold leg stopped in one minute": 2,
    "a close that waited for a bar": 50,
    "a protection that waited for a bar": 4,
    "a fill far from the level the stop was read at": 18,
    "sold and bought legs of different contract counts": 120,
    "a complete square-off": 18,
}


def test_the_sample_of_sessions_with_stops_reaches_the_hard_cases_they_are_there_for() -> None:
    reached = _what_the_sessions_with_stops_reach(RULE_SAMPLE)

    short = _short_of(reached, _THE_SAMPLE_REACHES_AT_LEAST)
    assert not short, f"the sample reached fewer than it must (reached, floor): {short}; all: {reached}"


@pytest.mark.slow  # all 250 seeds: this runs in the daily run, and every pull request runs the sample above
def test_the_whole_family_of_sessions_with_stops_reaches_the_hard_cases_they_are_there_for() -> None:
    reached = _what_the_sessions_with_stops_reach(RULE_SEEDS)

    short = _short_of(reached, _THE_FAMILY_REACHES_AT_LEAST)
    assert not short, f"sessions reached fewer than they must (reached, floor): {short}; all: {reached}"
