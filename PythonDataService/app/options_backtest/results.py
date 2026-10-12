"""What a replay of one session hands back (#3030).

Every time is ``int64 ms UTC``. A fill is named by the close of the bar it
filled in, and it filled at that bar's open: no tick time is invented. Money is
exact ``Decimal``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from app.options.contract import OptionContract


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


# The reason an entry was refused for want of cash.
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
    # Set when a close was decided, even if it never filled.
    exit_group: int | None
    exit_decided_at_ms: int | None
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

    ``price`` is the figure the event is about: the starting price taken, the close that reached a
    level, the price a fill filled at, or a mark. ``reason`` and ``detail`` say why where there is a
    reason: ``detail`` is words an owner can read.
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
    """One session replayed: its trades, what happened, and the cash that moved."""

    status: SessionStatus
    skip_reason: SkipReason | None
    trades: tuple[LegTrade, ...]
    events: tuple[Event, ...]
    # Cash at the end less cash at the start. A leg marked, not sold, has paid out and not been paid back.
    cash_change_usd: Decimal

    @property
    def exit_counts(self) -> dict[ExitReason, int]:
        """How many trades left for each reason."""
        return dict(Counter(trade.exit_reason for trade in self.trades))

    @property
    def event_counts(self) -> dict[EventKind, int]:
        """How many times each thing happened."""
        return dict(Counter(event.kind for event in self.events))
