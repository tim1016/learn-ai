"""What a replay of one session hands back (#3030).

Every time is ``int64 ms UTC``. A fill is named by the close of the bar it
filled in, and it filled at that bar's open: no tick time is invented. Money is
exact ``Decimal``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest import fills


class SessionStatus(StrEnum):
    TRADED = "traded"
    NO_TRADE = "no_trade"
    SKIPPED = "skipped"
    # A leg was still held when the session's data ended, and was marked, not sold.
    INCOMPLETE = "incomplete"


class SkipReason(StrEnum):
    WINDOW_DOES_NOT_FIT = "window_does_not_fit"
    UNDERLYING_DID_NOT_TRADE = "underlying_did_not_trade"


class ExitReason(StrEnum):
    STOP = "stop"
    # Closed because another leg's stop closed everything (``complete``).
    SQUARE_OFF = "square_off"
    EXIT_TIME = "exit_time"
    MARKED_NOT_SOLD = "marked_not_sold"


class CancelReason(StrEnum):
    """Why a leg's waiting, or a re-entry it had earned, was cancelled for the day."""

    # ``complete`` square-off: another leg's stop closed everything.
    SQUARE_OFF = "square_off"
    EXIT_TIME = "exit_time"


class EntryRefusal(StrEnum):
    """Why an entry that found a bar was refused."""

    # The cost of every entry filling in that minute, fees included, was more than the cash on hand.
    CASH_SHORT = "cash_short"


class EventKind(StrEnum):
    PICKED = "picked"
    PICK_REFUSED = "pick_refused"
    STARTING_PRICE_TAKEN = "starting_price_taken"
    NO_STARTING_PRICE = "no_starting_price"
    MOMENTUM_HIT = "momentum_hit"
    ENTRY_FILLED = "entry_filled"
    ENTRY_DROPPED = "entry_dropped"
    ENTRY_REFUSED_FOR_CASH = "entry_refused_for_cash"
    STOP_HIT = "stop_hit"
    SQUARED_OFF = "squared_off"
    EXIT_TIME = "exit_time"
    CLOSE_FILLED = "close_filled"
    RE_ENTRY_EARNED = "re_entry_earned"
    RE_ENTRY_BLOCKED_BY_CUTOFF = "re_entry_blocked_by_cutoff"
    RE_ENTRY_LOST = "re_entry_lost"
    LEG_CANCELLED = "leg_cancelled"
    MARKED_NOT_SOLD = "marked_not_sold"


@dataclass(frozen=True)
class Fill:
    """One order filled: at the open of the bar closing at ``bar_close_ms``, moved against the trader."""

    leg_id: str
    contract: OptionContract
    side: Literal["buy", "sell"]
    contracts: int
    multiplier: int
    bar_close_ms: int
    # The open of the fill bar, and the price it filled at after slippage and the price step.
    seen: Decimal
    filled: Decimal


@dataclass(frozen=True)
class Mark:
    """A leg valued, not sold: the close of the last bar of its contract seen in the session."""

    bar_close_ms: int
    price: Decimal


@dataclass(frozen=True)
class LegTrade:
    """One leg's round trip in one contract: its entry, its exit or its mark, its fees and its profit."""

    leg_id: str
    # 0 for the leg's first entry, then 1, 2, ... for each re-entry that filled.
    entry_number: int
    contract: OptionContract
    contracts: int
    multiplier: int
    # Set when the leg waited on momentum.
    starting_price: Decimal | None
    momentum_level: Decimal | None
    # Set when the leg carried a stop.
    stop_level: Decimal | None
    entry_decided_at_ms: int
    entry: Fill
    exit_reason: ExitReason
    # The exit group and instant of the close that was decided, even for a leg whose sale never filled.
    exit_group: int
    exit_decided_at_ms: int
    exit: Fill | Mark
    entry_fee_usd: Decimal
    # Zero for a leg that was marked, not sold.
    exit_fee_usd: Decimal
    pnl_usd: Decimal

    @property
    def exit_price(self) -> Decimal:
        """The price the leg left at: what it sold for, or its mark."""
        return self.exit.filled if isinstance(self.exit, Fill) else self.exit.price


@dataclass(frozen=True)
class Event:
    """One thing that happened, for a one-day timeline.

    ``entry_number`` follows one rule, as ``LegTrade.entry_number`` counts: 0 for a leg's first entry,
    1 for its first re-entry, and so on.

    - An event about a position (``entry_filled``, ``stop_hit``, ``squared_off``, ``exit_time``,
      ``close_filled``, ``marked_not_sold``) carries that position's entry number.
    - An event about an attempt to enter (``picked``, ``pick_refused``, ``starting_price_taken``,
      ``no_starting_price``, ``momentum_hit``, ``entry_dropped``, ``entry_refused_for_cash``,
      ``re_entry_earned``, ``re_entry_blocked_by_cutoff``, ``re_entry_lost``, ``leg_cancelled``)
      carries the number that entry has or would have had.

    So a stop of entry 0 that earns a re-entry is ``stop_hit`` 0 and ``re_entry_earned`` 1; a re-entry
    cancelled at the exit time is 1 whether or not the sale that earned it had filled; a first entry
    cancelled while it waited is 0.

    ``price`` is the figure the event is about:

    - ``starting_price_taken``: the starting price, the close of the picked contract's bar closing at that
      instant. The momentum level the leg then waits for is ``fills.momentum_level(rule, starting_price=price)``
      with the leg's momentum rule: a leg that waited and never entered has no trade to carry it.
    - ``momentum_hit`` and ``stop_hit``: the close of the bar that reached the level.
    - ``entry_filled`` and ``close_filled``: the price the order filled at.
    - ``entry_refused_for_cash``: the price the entry would have filled at.
    - ``marked_not_sold``: the mark.

    ``reason`` is a plain ``str``, the ``value`` of the enum it comes from: ``pick_refused`` takes
    ``PickRefusal``, ``entry_refused_for_cash`` takes ``EntryRefusal``, ``leg_cancelled`` takes
    ``CancelReason``. No other event has one. ``detail`` is words an owner can read, where there are any.
    """

    kind: EventKind
    at_ms: int
    leg_id: str
    entry_number: int
    contract: OptionContract | None = None
    price: Decimal | None = None
    reason: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SessionOutcome:
    """One session replayed: its trades, what happened, and the cash that moved.

    ``cash_change_usd`` counts only cash that moved, so a leg marked, not sold, has paid out and not been
    paid back. ``pnl_usd`` counts that leg at its mark, and ``marked_value_usd`` is the difference:
    ``pnl_usd == cash_change_usd + marked_value_usd``, to the cent, on every outcome. A caller that carries
    cash from one session to the next carries ``cash_change_usd``, and reports ``pnl_usd``.

    Only an entry is checked against the cash. A sale is never refused and its fee is paid even when it
    fills at nothing, so the cash at the session's end (the start plus ``cash_change_usd``) can be below zero
    by a closing fee.
    """

    status: SessionStatus
    skip_reason: SkipReason | None
    trades: tuple[LegTrade, ...]
    events: tuple[Event, ...]
    # Cash at the end less cash at the start.
    cash_change_usd: Decimal

    @property
    def pnl_usd(self) -> Decimal:
        """The sum of the trades' profit, a leg marked, not sold, counted at its mark."""
        with localcontext(fills.EXACT):
            return sum((trade.pnl_usd for trade in self.trades), start=Decimal(0))

    @property
    def marked_value_usd(self) -> Decimal:
        """What the legs marked, not sold, are worth at their marks: zero when none is."""
        with localcontext(fills.EXACT):
            return sum(
                (
                    fills.order_value(trade.exit_price, multiplier=trade.multiplier, contracts=trade.contracts)
                    for trade in self.trades
                    if trade.exit_reason is ExitReason.MARKED_NOT_SOLD
                ),
                start=Decimal(0),
            )

    @property
    def exit_counts(self) -> dict[ExitReason, int]:
        """How many trades left for each reason."""
        return dict(Counter(trade.exit_reason for trade in self.trades))

    @property
    def event_counts(self) -> dict[EventKind, int]:
        """How many times each thing happened."""
        return dict(Counter(event.kind for event in self.events))
