"""The lines and marks a one-day timeline draws for a saved session, worked out from its stored trades and events (#3030).

Formula: momentum level = starting price moved by the leg's momentum rule (``fills.momentum_level``); a stop level is
    the one the trade stored. Nothing else is calculated here: each line is a stored figure and the two instants it
    applies between.
Reference: ``app/options_backtest/replay.py`` for when each figure is set (and ``results.Event`` for what each event
  carries); ``fills.py`` for the momentum level.
Canonical implementation: this file for which lines there are and when they apply; ``fills.py`` for the level itself.
Validated against: tests/options_backtest/test_session_levels.py.

An *attempt* is one try of a leg to enter, named by the leg and the entry number (0 for the first entry, 1 for its
first re-entry, and so on). An attempt that filled leaves a trade; one that did not (momentum never reached, cancelled,
refused for cash) leaves events and no trade, so its levels come from its events and the run's setup.

Three kinds of line, each from one instant to another, both ends being minute closes:

* ``starting_price``: the premium the leg's momentum is measured from, from the minute it was taken until the attempt
  ended: for one that filled, the minute whose close reached the momentum level and decided the buy; for one that did
  not, the minute it was cancelled or refused for cash.
* ``momentum``: the premium the leg waits for, between the same two instants.
* ``stop``: the premium at or below which the position is sold, from the minute it filled (the replay reads the stop
  on that very bar) until the minute the close that sold it was decided: a stop hit, or the exit time.

A stored session that does not agree with itself (a trade with a starting price that no event took, an attempt that
waited and has no end) is not drawn from a guess: ``SessionRecordInconsistent``. The replay cannot make one.

A leg marked and not sold has no sale to draw: it is a mark, the last bar its contract traded in and that bar's close.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.results import Event, EventKind, ExitReason, Fill, LegTrade, SessionOutcome
from app.options_backtest.setup import OptionsBacktestSetup


class LevelKind(StrEnum):
    STARTING_PRICE = "starting_price"
    MOMENTUM = "momentum"
    STOP = "stop"


# The order lines are listed in when they begin together.
_KIND_ORDER = {kind: place for place, kind in enumerate(LevelKind)}


class SessionRecordInconsistent(ValueError):
    """A stored session's trades and events do not agree, so its lines cannot be drawn without a guess."""


@dataclass(frozen=True)
class Level:
    """A horizontal line: one price, one contract, between two minute closes (``from_ms`` at or before ``to_ms``)."""

    kind: LevelKind
    leg_id: str
    entry_number: int
    contract: OptionContract
    price: Decimal
    from_ms: int
    to_ms: int


@dataclass(frozen=True)
class ChartedFill:
    """An order that filled, to be marked on the bar it filled in: the price seen is that bar's open."""

    kind: Literal["entry", "exit"]
    leg_id: str
    entry_number: int
    contract: OptionContract
    contracts: int
    bar_close_ms: int
    seen: Decimal
    filled: Decimal
    # Why the leg was sold; ``None`` for an entry.
    exit_reason: ExitReason | None


@dataclass(frozen=True)
class ChartedMark:
    """A leg valued and not sold: the last bar its contract traded in, and that bar's close."""

    leg_id: str
    entry_number: int
    contract: OptionContract
    bar_close_ms: int
    price: Decimal


def _inconsistent(leg_id: str, entry_number: int, what: str) -> SessionRecordInconsistent:
    return SessionRecordInconsistent(f"leg {leg_id!r}, entry {entry_number}: {what}")


def _ended_at(leg_id: str, entry_number: int, events: Sequence[Event]) -> int:
    """When an attempt that never filled ended: the minute it was cancelled or refused for cash."""
    endings = [
        event.at_ms for event in events if event.kind in (EventKind.LEG_CANCELLED, EventKind.ENTRY_REFUSED_FOR_CASH)
    ]
    if not endings:
        raise _inconsistent(leg_id, entry_number, "it took a starting price, never filled and was never ended")
    return max(endings)


def levels_of(setup: OptionsBacktestSetup, outcome: SessionOutcome) -> tuple[Level, ...]:
    """Every line of the session, by the minute it begins, then the leg's place in the setup, the entry, the kind."""
    legs = {leg.leg_id: leg for leg in setup.legs}
    place = {leg.leg_id: at for at, leg in enumerate(setup.legs)}
    trade_of = {(trade.leg_id, trade.entry_number): trade for trade in outcome.trades}
    events_of: dict[tuple[str, int], list[Event]] = defaultdict(list)
    for event in outcome.events:
        events_of[(event.leg_id, event.entry_number)].append(event)

    levels: list[Level] = []
    waited: set[tuple[str, int]] = set()
    for (leg_id, entry_number), events in events_of.items():
        taken = [event for event in events if event.kind is EventKind.STARTING_PRICE_TAKEN]
        if not taken:
            continue
        contract, starting_price = taken[0].contract, taken[0].price
        if len(taken) > 1 or contract is None or starting_price is None:
            raise _inconsistent(leg_id, entry_number, "its starting price was taken more than once, or not fully")
        trade = trade_of.get((leg_id, entry_number))
        waited.add((leg_id, entry_number))
        if trade is not None:
            if trade.starting_price != starting_price or trade.momentum_level is None:
                raise _inconsistent(leg_id, entry_number, "the trade's starting price is not the one that was taken")
            momentum, to_ms = trade.momentum_level, trade.entry_decided_at_ms
        else:
            rule = legs[leg_id].momentum
            if rule is None:
                raise _inconsistent(leg_id, entry_number, "it took a starting price and its leg has no momentum rule")
            momentum = fills.momentum_level(rule, starting_price=starting_price)
            to_ms = _ended_at(leg_id, entry_number, events)
        from_ms = taken[0].at_ms
        for kind, price in ((LevelKind.STARTING_PRICE, starting_price), (LevelKind.MOMENTUM, momentum)):
            levels.append(Level(kind, leg_id, entry_number, contract, price, from_ms, to_ms))

    for trade in outcome.trades:
        if trade.starting_price is not None and (trade.leg_id, trade.entry_number) not in waited:
            raise _inconsistent(trade.leg_id, trade.entry_number, "the trade has a starting price no event took")
        if trade.stop_level is not None:
            levels.append(
                Level(
                    LevelKind.STOP,
                    trade.leg_id,
                    trade.entry_number,
                    trade.contract,
                    trade.stop_level,
                    trade.entry.bar_close_ms,
                    trade.exit_decided_at_ms,
                )
            )
    levels.sort(key=lambda each: (each.from_ms, place[each.leg_id], each.entry_number, _KIND_ORDER[each.kind]))
    return tuple(levels)


def fills_of(trades: Sequence[LegTrade]) -> tuple[ChartedFill, ...]:
    """Each trade's entry and, when it was sold, its sale: by the bar they filled in, then the trade's place, then entry first."""
    found: list[tuple[int, int, int, ChartedFill]] = []
    for place, trade in enumerate(trades):
        entry = trade.entry
        found.append(
            (
                entry.bar_close_ms,
                place,
                0,
                ChartedFill(
                    "entry",
                    trade.leg_id,
                    trade.entry_number,
                    trade.contract,
                    trade.contracts,
                    entry.bar_close_ms,
                    entry.seen,
                    entry.filled,
                    None,
                ),
            )
        )
        if isinstance(trade.exit, Fill):
            sale = trade.exit
            found.append(
                (
                    sale.bar_close_ms,
                    place,
                    1,
                    ChartedFill(
                        "exit",
                        trade.leg_id,
                        trade.entry_number,
                        trade.contract,
                        trade.contracts,
                        sale.bar_close_ms,
                        sale.seen,
                        sale.filled,
                        trade.exit_reason,
                    ),
                )
            )
    found.sort(key=lambda each: each[:3])
    return tuple(each[3] for each in found)


def marks_of(trades: Sequence[LegTrade]) -> tuple[ChartedMark, ...]:
    """The legs marked and not sold, in the order of their trades."""
    return tuple(
        ChartedMark(trade.leg_id, trade.entry_number, trade.contract, trade.exit.bar_close_ms, trade.exit_price)
        for trade in trades
        if not isinstance(trade.exit, Fill)
    )
