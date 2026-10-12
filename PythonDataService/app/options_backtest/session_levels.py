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

Three kinds of line, each from one instant to another, both ends being minute closes. One rule ends a line that waits:
**it ends when the thing it waits for happened, whatever came next.**

* ``starting_price``: the premium the leg's momentum is measured from, from the minute it was taken until the buy was
  decided, which is the minute whose close reached the momentum level (the last such minute, if an order was dropped
  and the leg waited again). A buy that was decided and then refused for cash ends its lines at that same minute, as
  one that filled does; the refusal comes a minute later and moves nothing. A leg cancelled before it ever reached the
  level was waiting until it was cancelled, so its lines end at the cancel.
* ``momentum``: the premium the leg waits for, between the same two instants.
* ``stop``: the premium at or below which the position is sold, from the minute it filled (the replay reads the stop
  on that very bar) until the minute the close that sold it was decided: a stop hit, or the exit time.

A stored session that does not agree with itself (a trade with a starting price that no event took, an attempt that
waited and has no end) is not drawn from a guess: ``SessionRecordInconsistent``. The replay cannot make one.

With no setup (a run whose stored setup no longer validates) every line that comes from a stored figure is still drawn:
the stops, and each starting price. The momentum line of an attempt that never filled is the only one left out, since
its level comes from the leg's rule; the lines of one leg that begin together are ordered by the leg's id.

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
    """When an attempt that never filled stopped waiting for what it was waiting for. It ends exactly once.

    A leg cancelled while it waited waited until the cancel. A buy refused for cash was decided at the last minute whose
    close reached the momentum level, so that is when its waiting ended, and not the minute of the refusal.
    """
    cancelled = [event.at_ms for event in events if event.kind is EventKind.LEG_CANCELLED]
    refused = [event.at_ms for event in events if event.kind is EventKind.ENTRY_REFUSED_FOR_CASH]
    if len(cancelled) + len(refused) != 1:
        raise _inconsistent(
            leg_id,
            entry_number,
            f"it took a starting price and never filled, and was ended {len(cancelled) + len(refused)} times",
        )
    if cancelled:
        return cancelled[0]
    decided = [event.at_ms for event in events if event.kind is EventKind.MOMENTUM_HIT]
    if not decided:
        raise _inconsistent(leg_id, entry_number, "its buy was refused for cash and momentum was never reached")
    return max(decided)


def levels_of(setup: OptionsBacktestSetup | None, outcome: SessionOutcome) -> tuple[Level, ...]:
    """Every line of the session, by the minute it begins, then the leg's place in the setup, the entry, the kind.

    ``setup`` is ``None`` for a run whose stored setup no longer validates: see the module docstring.
    """
    legs = {} if setup is None else {leg.leg_id: leg for leg in setup.legs}
    place = {} if setup is None else {leg.leg_id: at for at, leg in enumerate(setup.legs)}
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
        momentum: Decimal | None
        if trade is not None:
            if trade.starting_price != starting_price or trade.momentum_level is None:
                raise _inconsistent(leg_id, entry_number, "the trade's starting price is not the one that was taken")
            momentum, to_ms = trade.momentum_level, trade.entry_decided_at_ms
        else:
            to_ms = _ended_at(leg_id, entry_number, events)
            if setup is None:
                momentum = None
            else:
                rule = legs[leg_id].momentum
                if rule is None:
                    raise _inconsistent(
                        leg_id, entry_number, "it took a starting price and its leg has no momentum rule"
                    )
                momentum = fills.momentum_level(rule, starting_price=starting_price)
        from_ms = taken[0].at_ms
        levels.append(Level(LevelKind.STARTING_PRICE, leg_id, entry_number, contract, starting_price, from_ms, to_ms))
        if momentum is not None:
            levels.append(Level(LevelKind.MOMENTUM, leg_id, entry_number, contract, momentum, from_ms, to_ms))

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
    levels.sort(
        key=lambda each: (
            each.from_ms,
            place.get(each.leg_id, 0),
            each.leg_id,
            each.entry_number,
            _KIND_ORDER[each.kind],
        )
    )
    return tuple(levels)


def fills_of(trades: Sequence[LegTrade]) -> tuple[ChartedFill, ...]:
    """Each trade's entry and, when it was sold, its sale, by the bar they filled in.

    Fills in one bar come in the order of their trades (the sort is stable and they are added trade by trade), and a
    trade's sale is always in a later bar than its entry.
    """
    found: list[tuple[int, ChartedFill]] = []
    for trade in trades:
        entry = trade.entry
        found.append(
            (
                entry.bar_close_ms,
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
    found.sort(key=lambda each: each[0])
    return tuple(fill for _, fill in found)


def marks_of(trades: Sequence[LegTrade]) -> tuple[ChartedMark, ...]:
    """The legs marked and not sold, in the order of their trades."""
    return tuple(
        ChartedMark(trade.leg_id, trade.entry_number, trade.contract, trade.exit.bar_close_ms, trade.exit_price)
        for trade in trades
        if not isinstance(trade.exit, Fill)
    )
