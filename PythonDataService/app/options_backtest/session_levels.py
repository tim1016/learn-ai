"""The lines and marks a one-day timeline draws for a saved session, worked out from its stored trades and events (#3030).

Formula: momentum level = starting price moved by the leg's momentum rule (``fills.momentum_level``); a stop level is
    the one the trade stored, which for a sold leg is the price at or above which it is bought back
    (``fills.sold_stop_level``). Nothing else is calculated here: each line is a stored figure and the two instants it
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
  and the leg waited again). An order that was decided and then refused (for cash, for cover, for a second expiry or
  for a contract held on the other side) or not taken (a sale to open at nothing) ends its lines at that same minute,
  as one that filled does; the refusal comes a minute later and moves nothing. A leg cancelled before it ever reached
  the level was waiting until it was cancelled, so its lines end at the cancel.
* ``momentum``: the premium the leg waits for, between the same two instants.
* ``stop``: the premium at which the position is closed, from the minute it filled (the replay reads the stop on that
  very bar) until the minute the close that closed it was decided: a stop hit, a square-off, the leg's protection
  leaving, or the exit time. A bought leg's stop is the price at or below which it is sold, and a sold leg's (#3107) is
  the price at or above which it is bought back, so it is drawn above the price it was sold for.

A stored session that does not agree with itself (a trade with a starting price that no event took, or took on another
contract or at another price, an attempt that waited and has no end) is not drawn from a guess:
``SessionRecordInconsistent``. The replay cannot make one. Every event that names a contract and belongs to a stored
trade, the trade of its leg and entry number, is held to it (#3086): it must name the trade's contract, and an event that
says which way an order traded must agree with the trade's fills: an entry fill is on the side the leg trades (the
trade's entry side), a close fill is on the other, and a leg marked as owed was sold to open.

With no setup (a run whose stored setup no longer validates) every line that comes from a stored figure is still drawn:
the stops, and each starting price. The momentum line of an attempt that never filled is the only one left out, since
its level comes from the leg's rule; the lines of one leg that begin together are ordered by the leg's id.

A leg marked and not sold, or sold and not bought back, has no close to draw: it is a mark, the last bar its contract
traded in and that bar's close, and it says whether it is owed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest import fills
from app.options_backtest.results import (
    ENTRY_REFUSAL_EVENTS,
    Event,
    EventKind,
    ExitReason,
    Fill,
    LegTrade,
    SessionOutcome,
)
from app.options_backtest.setup import LegSetup, OptionsBacktestSetup


class LevelKind(StrEnum):
    STARTING_PRICE = "starting_price"
    MOMENTUM = "momentum"
    STOP = "stop"


# The order lines are listed in when they begin together.
_KIND_ORDER = {kind: place for place, kind in enumerate(LevelKind)}
# The events that end an entry which found a bar without a fill.
_ENTRY_REFUSED = frozenset(ENTRY_REFUSAL_EVENTS.values())


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
    """An order that filled, to be marked on the bar it filled in: the price seen is that bar's open.

    ``kind`` says whether it opened or closed the leg and ``side`` which way it traded, so a sold leg's entry is an
    ``entry`` on the ``sell`` side and its buy-back an ``exit`` on the ``buy`` side (#3109).
    """

    kind: Literal["entry", "exit"]
    side: Literal["buy", "sell"]
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
    """A leg valued and not closed: the last bar its contract traded in, and that bar's close.

    ``owed`` is whether the leg was sold to open, so its mark is an amount owed and not a value held (#3109).
    """

    leg_id: str
    entry_number: int
    contract: OptionContract
    bar_close_ms: int
    price: Decimal
    owed: bool


def _inconsistent(leg_id: str, entry_number: int, what: str) -> SessionRecordInconsistent:
    return SessionRecordInconsistent(f"leg {leg_id!r}, entry {entry_number}: {what}")


def _side_disagreement(kind: EventKind, trade: LegTrade, leg: LegSetup | None) -> str | None:
    """What an event says of the way an order traded that its trade's fills do not bear out, or ``None``.

    Only the kinds that say which way an order traded are asked: an entry fill (the leg's side, which is the trade's
    entry side), a close fill (the other side, and a fill), a mark (no fill, and owed exactly when the leg opened by
    a sale) and a leg closed with its protection (a sold leg). ``leg`` is ``None`` when the run's setup is lost.
    """
    opened = trade.entry.side
    leaving = trade.exit
    if kind is EventKind.ENTRY_FILLED:
        if leg is not None and leg.side != opened:
            return f"its entry filled on the {opened} side and the setup's leg trades on the {leg.side} side"
    elif kind is EventKind.CLOSE_FILLED:
        if not isinstance(leaving, Fill):
            return "its close filled and the trade was marked, not closed"
        if leaving.side == opened:
            return f"its close traded on the {leaving.side} side, which is the side the leg opened on"
    elif kind in (EventKind.MARKED_NOT_SOLD, EventKind.MARKED_NOT_BOUGHT_BACK):
        if isinstance(leaving, Fill):
            return "it was marked and the trade was closed by a fill"
        if (kind is EventKind.MARKED_NOT_BOUGHT_BACK) != (opened == "sell"):
            marked_as = "owed" if kind is EventKind.MARKED_NOT_BOUGHT_BACK else "held"
            return f"it was marked as {marked_as} and the leg opened on the {opened} side"
    elif kind is EventKind.CLOSED_WITH_ITS_PROTECTION and opened != "sell":
        return "it was closed with its protection and only a sold leg is, and the leg opened on the buy side"
    return None


def _check_events_against_trades(
    legs: Mapping[str, LegSetup], trade_of: Mapping[tuple[str, int], LegTrade], events: Sequence[Event]
) -> None:
    """Hold every event that names a contract and belongs to a stored trade to that trade (#3086).

    An event belongs to the trade of its leg and entry number, when there is one: an attempt that never filled has
    none, and an event of it has nothing to be held to here.
    """
    for event in events:
        if event.contract is None:
            continue
        trade = trade_of.get((event.leg_id, event.entry_number))
        if trade is None:
            continue
        if event.contract != trade.contract:
            raise _inconsistent(
                event.leg_id,
                event.entry_number,
                f"its {event.kind.value} event is of a different contract from the trade's",
            )
        if (why := _side_disagreement(event.kind, trade, legs.get(event.leg_id))) is not None:
            raise _inconsistent(event.leg_id, event.entry_number, f"its {event.kind.value} event disagrees: {why}")


def _ended_at(leg_id: str, entry_number: int, events: Sequence[Event]) -> int:
    """When an attempt that never filled stopped waiting for what it was waiting for. It ends exactly once.

    A leg cancelled while it waited waited until the cancel. An order refused (for whatever reason an entry is
    refused) was decided at the last minute whose close reached the momentum level, so that is when its waiting
    ended, and not the minute of the refusal.
    """
    cancelled = [event.at_ms for event in events if event.kind is EventKind.LEG_CANCELLED]
    refused = [event.at_ms for event in events if event.kind in _ENTRY_REFUSED]
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
        raise _inconsistent(leg_id, entry_number, "its order was refused and momentum was never reached")
    return max(decided)


def levels_of(setup: OptionsBacktestSetup | None, outcome: SessionOutcome) -> tuple[Level, ...]:
    """Every line of the session, by the minute it begins, then the leg's place in the setup, the entry, the kind.

    ``setup`` is ``None`` for a run whose stored setup no longer validates: see the module docstring.
    """
    legs = {} if setup is None else {leg.leg_id: leg for leg in setup.legs}
    place = {} if setup is None else {leg.leg_id: at for at, leg in enumerate(setup.legs)}
    trade_of = {(trade.leg_id, trade.entry_number): trade for trade in outcome.trades}
    _check_events_against_trades(legs, trade_of, outcome.events)
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
            # The lines are drawn on the event's contract and the fills and the stop on the trade's: they are one.
            if trade.contract != contract:
                raise _inconsistent(
                    leg_id,
                    entry_number,
                    "the trade is of a different contract from the one whose starting price was taken",
                )
            momentum, to_ms = trade.momentum_level, trade.entry_decided_at_ms
        else:
            to_ms = _ended_at(leg_id, entry_number, events)
            if setup is None:
                momentum = None
            else:
                # A setup edited by hand into another valid one may no longer have the leg the events name.
                leg = legs.get(leg_id)
                if leg is None:
                    raise _inconsistent(leg_id, entry_number, "its leg is not in the run's setup")
                rule = leg.momentum
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
    """Each trade's entry and, when it was closed, its sale or buy-back, by the bar they filled in.

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
                    entry.side,
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
                        sale.side,
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
    """The legs marked and not closed, in the order of their trades: a bought leg not sold, a sold leg owed."""
    return tuple(
        ChartedMark(
            trade.leg_id,
            trade.entry_number,
            trade.contract,
            trade.exit.bar_close_ms,
            trade.exit_price,
            trade.is_sold_leg,
        )
        for trade in trades
        if not isinstance(trade.exit, Fill)
    )
