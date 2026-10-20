"""The proof that ``session_needs`` is never short: no replay reads a contract it did not name.

Over many seeded random sessions (a random walk underlying, random walk premiums with minutes missing, random setups
with stops, momentum, both kinds of re-entry, both square-off modes, cutoffs, cash that runs short, and several
slippage values) a market records every contract the replay asks ``option_bar`` about. Each one must be in the set
``session_needs`` gave before one option bar was read. A failing seed is printed with its setup, so it can be replayed.

Each session is then replayed again on the market a run builds (``DayMarket``) holding only the needed contracts, and
must give an equal outcome: a contract it was not handed would raise.

The sessions are generated to reach the cases that make a set too small, not only to be many: legs that stop out and
re-enter at fresh strikes as the underlying walks across the ladder. Each test also makes sure they did.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.needs import session_needs
from app.options_backtest.picking import Listing, pick_contract
from app.options_backtest.replay import replay_session
from app.options_backtest.results import EventKind, ExitReason, SessionOutcome, SessionStatus
from app.options_backtest.session_market import DayMarket
from app.options_backtest.setup import LegSetup, OptionsBacktestSetup
from tests.options_backtest._market import after_open, before_close
from tests.options_backtest._run_data import HALF_DAY, D, close_ms, listing_on

SEEDS = range(500)
# Setups that sell (#3107), with seeds of their own so that their premiums are not those of the first family. Every pull
# request runs a sample of them: the three seeds that between them reach every case the checks below count (chosen
# again and again as the seed that adds the most cases not yet reached) and every tenth. All 200 run in the daily run.
SELLING_SEEDS = range(10_000, 10_200)
SELLING_SAMPLE = tuple(sorted({10_004, 10_022, 10_090, *range(10_000, 10_200, 10)}))
SLIPPAGES = ("0", "0.3", "1", "5")
# A Wednesday: this week's expiry is that Friday and a week's is the Wednesday after, all of them listed.
# One seed in six is the half day instead (it closes 180 minutes early), where a window counted from the close
# does not fit and the session is skipped.
FULL_DAY = date(2026, 10, 7)
FULL_DAY_EXPIRIES = (FULL_DAY, date(2026, 10, 9), date(2026, 10, 14), date(2026, 10, 16))
SHORT_DAY_EXPIRIES = (HALF_DAY, date(2026, 12, 4), date(2026, 12, 11), date(2026, 12, 18))
STRIKES = tuple(range(488, 513))
LAST_GENERATED_MINUTE = 75


def _random_stop(rng: random.Random) -> dict[str, Any]:
    return rng.choice(
        [
            {"kind": "percent", "value": str(rng.choice([8, 15, 30]))},
            {"kind": "points", "value": rng.choice(["0.05", "0.20"])},
        ]
    )


def _random_leg(rng: random.Random, index: int, *, fixed: dict[str, Any] | None = None) -> dict[str, Any]:
    """One random leg. ``fixed`` overrides what the leg is (its side, right, contracts or expiry) after it is drawn, so
    that the stream of draws is the same with or without it."""
    has_momentum = rng.random() < 0.5
    written: dict[str, Any] = {
        "leg_id": f"leg{index}",
        "right": rng.choice(["call", "put"]),
        "contracts": rng.randint(1, 3),
        "expiry": rng.choice(
            [{"kind": "same_day"}, {"kind": "this_week"}, {"kind": "min_days", "days": rng.choice([1, 7])}]
        ),
        "strike": rng.choice([{"kind": "atm"}, {"kind": "atm_offset", "steps": rng.choice([-3, -1, 1, 2])}]),
    }
    written.update(fixed or {})
    if rng.random() < 0.75:
        written["stop_loss"] = _random_stop(rng)
    if has_momentum:
        written["momentum"] = rng.choice(
            [
                {"kind": "percent_up", "value": "6"},
                {"kind": "percent_down", "value": "6"},
                {"kind": "points_up", "value": "0.15"},
                {"kind": "points_down", "value": "0.15"},
            ]
        )
    if rng.random() < 0.65:
        kind = "on_momentum" if has_momentum and rng.random() < 0.5 else "at_once"
        written["reentry_on_stop"] = {"kind": kind, "count": rng.randint(1, 3)}
        # A setup that gives a leg a re-entry rule no stop can reach is refused, so the leg gets a stop of its own.
        if "stop_loss" not in written:
            written["stop_loss"] = _random_stop(rng)
    return written


def _random_selling_legs(rng: random.Random) -> list[dict[str, Any]]:
    """Legs that sell (#3107): bought legs and the sold legs they cover, in one or two kinds, on one expiry rule.

    A sold leg that re-enters is sometimes left with no stop of its own when a bought leg of its kind has one, so that
    it comes back only by being closed with its protection, which is how ``stop_can_close`` says it can.
    """
    expiry = rng.choice([{"kind": "same_day"}, {"kind": "this_week"}, {"kind": "min_days", "days": rng.choice([1, 7])}])
    legs: list[dict[str, Any]] = []
    for right in rng.sample(["call", "put"], rng.choice([1, 2])):
        bought = [rng.randint(1, 3) for _ in range(rng.randint(1, 2))]
        room, sold = sum(bought), []
        while room and len(sold) < 2 and rng.random() < 0.9:
            sold.append(rng.randint(1, room))
            room -= sold[-1]
        for side, size in [*(("buy", size) for size in bought), *(("sell", size) for size in sold)]:
            fixed: dict[str, Any] = {"right": right, "contracts": size, "expiry": expiry}
            if side == "sell":
                fixed["side"] = "sell"
            legs.append(_random_leg(rng, len(legs), fixed=fixed))
    for written in legs:
        # Most legs enter at once, so that what they cover and what stops them is held for a stop to take it along.
        if "momentum" in written and rng.random() < 0.6:
            del written["momentum"]
            if written.get("reentry_on_stop", {}).get("kind") == "on_momentum":
                written["reentry_on_stop"]["kind"] = "at_once"
    for written in legs:
        protected = any(
            other.get("side") != "sell" and other["right"] == written["right"] and "stop_loss" in other
            for other in legs
        )
        if written.get("side") == "sell" and "reentry_on_stop" in written and protected and rng.random() < 0.5:
            del written["stop_loss"]
    return legs


def _random_setup(rng: random.Random, slippage: str, *, selling: bool = False) -> OptionsBacktestSetup:
    entry_minute = rng.randint(2, 15)
    exit_minute = entry_minute + rng.randint(15, 60)
    written: dict[str, Any] = {
        "underlying": "SPY",
        "entry": after_open(entry_minute),
        # The same instant counted from the open or from the close of a full day.
        "exit": rng.choice([after_open(exit_minute), before_close(390 - exit_minute)]),
        "square_off": rng.choice(["partial", "complete"]),
        "legs": _random_selling_legs(rng)
        if selling
        else [_random_leg(rng, index) for index in range(rng.randint(1, 3))],
        "slippage_percent": slippage,
        "price_step_usd": "0.01",
        "fees": {"kind": "not_charged"},
        # A setup that sells has the collateral to hold, so it starts with more than the first family's smallest.
        "starting_cash_usd": rng.choice(["2000", "10000", "10000"] if selling else ["250", "2000", "10000"]),
    }
    if rng.random() < 0.4:
        written["no_reentry_after"] = after_open(entry_minute + rng.randint(3, 40))
    return OptionsBacktestSetup.model_validate(written)


def _underlying(rng: random.Random, day: date) -> tuple[MinuteBar, ...]:
    """A random walk of closes, each bar with an open, a high and a low of its own around its close.

    The prices come to different strikes on purpose: a needs or a market that read the open, the high or the low
    where the close belongs would pick other contracts.
    """
    price, closes = 500.0 + rng.gauss(0, 3), []
    for minute in range(1, 391):
        price += rng.gauss(0, 0.6)
        if rng.random() < 0.04:
            continue  # the underlying did not trade
        closes.append((minute, D(f"{price:.2f}")))
    # Drawn after the walk, so that the walk is the same one it was before the bars had a range.
    around = random.Random(rng.random())
    bars = []
    for minute, close in closes:
        opened = close + D(f"{around.gauss(0, 1.2):.2f}")
        high = max(opened, close) + D(f"{abs(around.gauss(0, 0.5)):.2f}")
        low = min(opened, close) - D(f"{abs(around.gauss(0, 0.5)):.2f}")
        bars.append(MinuteBar(close_ms=close_ms(day, minute), open=opened, high=high, low=low, close=close))
    return tuple(bars)


def _premiums(seed: int, day: date, contract: OptionContract) -> dict[int, MinuteBar]:
    """One contract's bars: a random walk that always trades above a cent, with some minutes missing."""
    rng = random.Random(f"{seed}|{contract.occ_symbol}")
    price = max(0.15, 4.0 - 0.3 * abs(float(contract.strike) - 500.0))
    # Three contracts in ten stop trading early, so a leg holding one is marked, not sold.
    last_minute = LAST_GENERATED_MINUTE if rng.random() < 0.7 else rng.randint(8, 60)
    bars: dict[int, MinuteBar] = {}
    for minute in range(1, last_minute + 1):
        opened = max(0.02, price * (1 + rng.gauss(0, 0.03)))
        price = max(0.02, opened * (1 + rng.gauss(0, 0.05)))
        if rng.random() < 0.12:
            continue  # nothing traded this minute
        high, low = max(opened, price), min(opened, price)
        at = close_ms(day, minute)
        bars[at] = MinuteBar(at, D(f"{opened:.2f}"), D(f"{high:.2f}"), D(f"{low:.2f}"), D(f"{price:.2f}"))
    return bars


@dataclass
class _RecordingMarket:
    """A real session's observations, with the premiums generated on demand, that remembers every contract asked for."""

    day: date
    listing: Listing
    underlying: dict[int, Decimal]
    seed: int
    asked: set[OptionContract] = field(default_factory=set)
    _series: dict[OptionContract, dict[int, MinuteBar]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        window = session_window_for_date(self.day)
        self.session_open_ms, self.session_close_ms = window.open_ms_utc, window.close_ms_utc

    def underlying_close(self, close_ms: int) -> Decimal | None:
        return self.underlying.get(close_ms)

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        return pick_contract(leg, listing=self.listing, trading_date=self.day, underlying_price=underlying_price)

    def option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        self.asked.add(contract)
        if contract not in self._series:
            self._series[contract] = _premiums(self.seed, self.day, contract)
        return self._series[contract].get(close_ms)


def _no_fee(_fill: object) -> Decimal:
    return D(0)


@dataclass
class _Seen:
    sessions: int = 0
    skipped: int = 0
    traded: int = 0
    marked: int = 0
    re_entries: int = 0
    a_re_entry_picked_a_fresh_contract: int = 0
    refused_for_cash: int = 0
    closed_with_its_protection: int = 0
    a_sold_leg_came_back_by_its_protection_alone: int = 0


def _check_one_seed(seed: int, seen: _Seen, *, selling: bool = False) -> None:
    rng = random.Random(seed)
    setup_at_zero = _random_setup(rng, "0", selling=selling)
    day, expiries = (HALF_DAY, SHORT_DAY_EXPIRIES) if seed % 6 == 0 else (FULL_DAY, FULL_DAY_EXPIRIES)
    underlying = _underlying(rng, day)
    listing = listing_on(day, strikes=STRIKES, expiries=expiries)
    window = session_window_for_date(day)
    needs = session_needs(
        setup_at_zero,
        trading_date=day,
        session_open_ms=window.open_ms_utc,
        session_close_ms=window.close_ms_utc,
        underlying=underlying,
        listing=listing,
    )
    market = _RecordingMarket(day, listing, {bar.close_ms: bar.close for bar in underlying}, seed)
    where = f"seed {seed}: {json.dumps(setup_at_zero.model_dump(mode='json'))}"
    # What the real market a run builds does when it holds only the needed contracts: the same outcome, or it raises
    # ``ContractDayNotLoaded`` for the first contract it was asked for and was not handed.
    needed_only = DayMarket(
        day,
        underlying=underlying,
        listing=listing,
        option_days={contract: tuple(_premiums(seed, day, contract).values()) for contract in needs.contracts},
    )
    for slippage in SLIPPAGES:
        written = OptionsBacktestSetup.model_validate(
            {**setup_at_zero.model_dump(mode="python"), "slippage_percent": slippage}
        )
        outcome = replay_session(written, market, cash_usd=written.starting_cash_usd, fee_for=_no_fee)
        assert outcome.skip_reason == needs.skip_reason, f"{where}: the replay skipped for {outcome.skip_reason}"
        assert outcome == replay_session(written, needed_only, cash_usd=written.starting_cash_usd, fee_for=_no_fee), (
            f"{where}: at slippage {slippage} the needed contracts alone give another outcome"
        )
        if slippage == "0":
            _count(outcome, seen)
            # A sold leg with a re-entry rule and no stop of its own, under a partial square-off, comes back only when
            # a bought leg's stop takes it along.
            by_protection_alone = {
                each.leg_id
                for each in setup_at_zero.legs
                if each.side == "sell" and each.stop_loss is None and each.reentry_on_stop is not None
            }
            seen.a_sold_leg_came_back_by_its_protection_alone += setup_at_zero.square_off == "partial" and any(
                trade.leg_id in by_protection_alone and trade.entry_number > 0 for trade in outcome.trades
            )
    missing = market.asked - needs.contracts
    assert not missing, f"{where}: the replay asked for {sorted(c.occ_symbol for c in missing)} which were not needed"


def _count(outcome: SessionOutcome, seen: _Seen) -> None:
    seen.sessions += 1
    seen.skipped += outcome.status is SessionStatus.SKIPPED
    seen.traded += bool(outcome.trades)
    seen.marked += outcome.status is SessionStatus.INCOMPLETE
    seen.re_entries += any(trade.entry_number > 0 for trade in outcome.trades)
    seen.refused_for_cash += EventKind.ENTRY_REFUSED_FOR_CASH in outcome.event_counts
    seen.closed_with_its_protection += any(
        trade.exit_reason is ExitReason.CLOSED_WITH_ITS_PROTECTION for trade in outcome.trades
    )
    first_picks = {
        (event.leg_id, event.contract)
        for event in outcome.events
        if event.kind is EventKind.PICKED and event.entry_number == 0
    }
    seen.a_re_entry_picked_a_fresh_contract += any(
        event.kind is EventKind.PICKED and event.entry_number > 0 and (event.leg_id, event.contract) not in first_picks
        for event in outcome.events
    )


@cache
def _all_seeds() -> _Seen:
    seen = _Seen()
    for seed in SEEDS:
        _check_one_seed(seed, seen)
    return seen


@cache
def _selling_sessions(seeds: tuple[int, ...]) -> _Seen:
    seen = _Seen()
    for seed in seeds:
        _check_one_seed(seed, seen, selling=True)
    return seen


def test_no_replay_over_many_random_sessions_reads_a_contract_that_was_not_needed_and_they_reach_the_cases_that_make_a_set_too_small() -> (
    None
):
    seen = _all_seeds()

    assert seen.sessions == len(SEEDS), "no replay over many random sessions reads a contract that was not needed"
    # The generator is held to its purpose: if a change to it stopped producing re-entries at fresh strikes, or
    # skipped sessions, the proof above would still pass and prove less.
    assert seen.skipped >= 30, "the random sessions reach enough skipped sessions"
    assert seen.traded >= 130, "the random sessions reach enough traded sessions"
    assert seen.re_entries >= 45, "the random sessions reach enough re-entries"
    assert seen.marked >= 25, "the random sessions reach enough marked sessions"
    assert seen.refused_for_cash >= 70, "the random sessions reach enough entries refused for cash"
    assert seen.a_re_entry_picked_a_fresh_contract >= 50, (
        "the random sessions reach enough re-entries at a fresh strike"
    )


def test_no_replay_of_a_sample_of_setups_that_sell_reads_a_contract_that_was_not_needed_and_they_reach_the_closes_and_re_entries() -> (
    None
):
    # The same proof for setups with sold legs (#3107): a sold leg that re-enters, or is closed with its protection and
    # then re-enters, picks afresh at a later minute, and what it picks must be in what the session needs.
    seen = _selling_sessions(SELLING_SAMPLE)

    assert seen.sessions == len(SELLING_SAMPLE), "no replay of a sample of setups that sell reads a contract not needed"
    assert seen.traded >= 10, "the sample of sessions that sell reaches enough traded sessions"
    assert seen.re_entries >= 5, "the sample of sessions that sell reaches enough re-entries"
    assert seen.a_re_entry_picked_a_fresh_contract >= 8, "the sample reaches enough re-entries at a fresh strike"
    assert seen.closed_with_its_protection >= 2, "the sample reaches enough sold legs closed with their protection"
    assert seen.a_sold_leg_came_back_by_its_protection_alone >= 1, (
        "the sample reaches a sold leg back by its protection"
    )


@pytest.mark.slow  # all 200 seeds: this runs in the daily run, and every pull request runs the sample above
def test_no_replay_over_many_random_setups_that_sell_reads_a_contract_that_was_not_needed_and_they_reach_the_closes_and_re_entries() -> (
    None
):
    seen = _selling_sessions(tuple(SELLING_SEEDS))

    assert seen.sessions == len(SELLING_SEEDS), (
        "no replay over many random setups that sell reads a contract not needed"
    )
    assert seen.traded >= 120, "the random setups that sell reach enough traded sessions"
    assert seen.re_entries >= 60, "the random setups that sell reach enough re-entries"
    assert seen.a_re_entry_picked_a_fresh_contract >= 60, (
        "the random setups that sell reach enough fresh-strike re-entries"
    )
    assert seen.closed_with_its_protection >= 20, "the random setups that sell reach enough protection closes"
    assert seen.a_sold_leg_came_back_by_its_protection_alone >= 3, (
        "the setups that sell reach sold legs back by protection"
    )
