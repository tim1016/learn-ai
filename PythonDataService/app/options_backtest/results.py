"""What a replay of one session hands back (#3030).

Every time is ``int64 ms UTC``. A fill is named by the close of the bar it
filled in, and it filled at that bar's open: no tick time is invented. Money is
exact ``Decimal``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Literal

from app.options.contract import OptionContract
from app.options_backtest import fills


class SessionStatus(StrEnum):
    TRADED = "traded"
    NO_TRADE = "no_trade"
    SKIPPED = "skipped"
    # A leg was still held when the session's data ended, and was marked: a bought leg not sold, a sold leg not
    # bought back.
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
    # A sold leg never bought back: valued at its last traded close as an amount owed (#3106).
    MARKED_NOT_BOUGHT_BACK = "marked_not_bought_back"


class CancelReason(StrEnum):
    """Why a leg's waiting, or a re-entry it had earned, was cancelled for the day."""

    # ``complete`` square-off: another leg's stop closed everything.
    SQUARE_OFF = "square_off"
    EXIT_TIME = "exit_time"


class EntryRefusal(StrEnum):
    """Why an entry that found a bar was refused.

    The members after ``CASH_SHORT`` say what the cover book (``cover_book.OpeningRefusal``) said, by value, and
    the book's one refusal for want of cover is told as two (see ``replay.entry_refusal``).
    """

    # The entries filling in that minute, fees included, needed more cash than was on hand: for a setup that
    # sells, more than the cash less the collateral of what the group would then hold.
    CASH_SHORT = "cash_short"
    # Another leg holds this contract, or stood to hold it in the same minute, on the other side.
    OTHER_SIDE = "other_side"
    # Its expiry differs from one in play, and a sold contract is in play or it is itself sold.
    SECOND_EXPIRY = "second_expiry"
    # A sold leg with no bought contract of its kind to cover it, and a bought leg of its kind was dropped in the
    # same minute for want of a bar.
    COVER_DID_NOT_TRADE = "cover_did_not_trade"
    # A sold leg with no bought contract of its kind to cover it, and none held or filling with it.
    COVER_NOT_YET_HELD = "cover_not_yet_held"
    # A sale to open that would have filled at nothing.
    SALE_AT_ZERO = "sale_at_zero"


class EventKind(StrEnum):
    PICKED = "picked"
    PICK_REFUSED = "pick_refused"
    STARTING_PRICE_TAKEN = "starting_price_taken"
    NO_STARTING_PRICE = "no_starting_price"
    MOMENTUM_HIT = "momentum_hit"
    ENTRY_FILLED = "entry_filled"
    ENTRY_DROPPED = "entry_dropped"
    ENTRY_REFUSED_FOR_CASH = "entry_refused_for_cash"
    ENTRY_REFUSED_FOR_COVER = "entry_refused_for_cover"
    ENTRY_REFUSED_FOR_SECOND_EXPIRY = "entry_refused_for_second_expiry"
    ENTRY_REFUSED_FOR_OTHER_SIDE = "entry_refused_for_other_side"
    SALE_NOT_TAKEN_AT_ZERO = "sale_not_taken_at_zero"
    STOP_HIT = "stop_hit"
    SQUARED_OFF = "squared_off"
    EXIT_TIME = "exit_time"
    CLOSE_FILLED = "close_filled"
    RE_ENTRY_EARNED = "re_entry_earned"
    RE_ENTRY_BLOCKED_BY_CUTOFF = "re_entry_blocked_by_cutoff"
    RE_ENTRY_LOST = "re_entry_lost"
    LEG_CANCELLED = "leg_cancelled"
    MARKED_NOT_SOLD = "marked_not_sold"
    MARKED_NOT_BOUGHT_BACK = "marked_not_bought_back"


# The event each refusal of an entry is told in. Every one is about an attempt to enter, carries the price the entry
# would have filled at, and has its refusal as its reason.
ENTRY_REFUSAL_EVENTS: Mapping[EntryRefusal, EventKind] = {
    EntryRefusal.CASH_SHORT: EventKind.ENTRY_REFUSED_FOR_CASH,
    EntryRefusal.OTHER_SIDE: EventKind.ENTRY_REFUSED_FOR_OTHER_SIDE,
    EntryRefusal.SECOND_EXPIRY: EventKind.ENTRY_REFUSED_FOR_SECOND_EXPIRY,
    EntryRefusal.COVER_DID_NOT_TRADE: EventKind.ENTRY_REFUSED_FOR_COVER,
    EntryRefusal.COVER_NOT_YET_HELD: EventKind.ENTRY_REFUSED_FOR_COVER,
    EntryRefusal.SALE_AT_ZERO: EventKind.SALE_NOT_TAKEN_AT_ZERO,
}


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
    """A leg valued, not closed: the close of the last bar of its contract seen in the session.

    A bought leg's mark is what it is worth, never sold. A sold leg's is an amount owed, never bought back.
    """

    bar_close_ms: int
    price: Decimal


@dataclass(frozen=True)
class LegTrade:
    """One leg's round trip in one contract: its entry, its exit or its mark, its fees and its profit.

    A bought leg's entry is a buy and its exit a sale. A sold leg's entry is a sale and its exit a buy-back (a fill
    on the buy side). Either leg may instead end in a mark, which has no side: the trade's entry side says whether
    it is owed.
    """

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
    # The exit group and instant of the close that was decided, even for a leg whose close never filled.
    exit_group: int
    exit_decided_at_ms: int
    exit: Fill | Mark
    entry_fee_usd: Decimal
    # Zero for a leg that was marked.
    exit_fee_usd: Decimal
    pnl_usd: Decimal

    @property
    def exit_price(self) -> Decimal:
        """The price the leg left at: what it was sold or bought back for, or its mark."""
        return self.exit.filled if isinstance(self.exit, Fill) else self.exit.price

    @property
    def is_sold_leg(self) -> bool:
        """Whether the leg opened by a sale: it left by a buy-back, or is marked as an amount owed."""
        return self.entry.side == "sell"

    @property
    def marked(self) -> bool:
        """Whether the leg left by a mark and not by a fill."""
        return isinstance(self.exit, Mark)


@dataclass(frozen=True)
class Event:
    """One thing that happened, for a one-day timeline.

    ``entry_number`` follows one rule, as ``LegTrade.entry_number`` counts: 0 for a leg's first entry,
    1 for its first re-entry, and so on.

    - An event about a position (``entry_filled``, ``stop_hit``, ``squared_off``, ``exit_time``,
      ``close_filled``, ``marked_not_sold``, ``marked_not_bought_back``) carries that position's entry number.
    - An event about an attempt to enter (``picked``, ``pick_refused``, ``starting_price_taken``,
      ``no_starting_price``, ``momentum_hit``, ``entry_dropped``, ``entry_refused_for_cash``,
      ``entry_refused_for_cover``, ``entry_refused_for_second_expiry``, ``entry_refused_for_other_side``,
      ``sale_not_taken_at_zero``, ``re_entry_earned``, ``re_entry_blocked_by_cutoff``, ``re_entry_lost``,
      ``leg_cancelled``) carries the number that entry has or would have had.

    So a stop of entry 0 that earns a re-entry is ``stop_hit`` 0 and ``re_entry_earned`` 1; a re-entry
    cancelled at the exit time is 1 whether or not the sale that earned it had filled; a first entry
    cancelled while it waited is 0.

    ``price`` is the figure the event is about:

    - ``starting_price_taken``: the starting price, the close of the picked contract's bar closing at that
      instant. The momentum level the leg then waits for is ``fills.momentum_level(rule, starting_price=price)``
      with the leg's momentum rule: a leg that waited and never entered has no trade to carry it.
    - ``momentum_hit`` and ``stop_hit``: the close of the bar that reached the level.
    - ``entry_filled`` and ``close_filled``: the price the order filled at, a sale to open or a buy-back included.
    - an entry refused (``entry_refused_for_cash``, ``..._for_cover``, ``..._for_second_expiry``,
      ``..._for_other_side``) or not taken (``sale_not_taken_at_zero``): the price the entry would have filled at.
    - ``marked_not_sold`` and ``marked_not_bought_back``: the mark.

    ``reason`` is a plain ``str``, the ``value`` of the enum it comes from: ``pick_refused`` takes
    ``PickRefusal``, each event of ``ENTRY_REFUSAL_EVENTS`` takes the ``EntryRefusal`` it is the event of,
    ``leg_cancelled`` takes ``CancelReason``. No other event has one. ``detail`` is words an owner can read,
    where there are any.
    """

    kind: EventKind
    at_ms: int
    leg_id: str
    entry_number: int
    contract: OptionContract | None = None
    price: Decimal | None = None
    reason: str | None = None
    detail: str | None = None


class FillGroup(StrEnum):
    """The two groups of fills a minute holds, in the order they fill: the opening group, then the closing group."""

    OPENING = "opening"
    CLOSING = "closing"


@dataclass(frozen=True)
class MoneyStep:
    """What one fill group did to the money, as facts the replay states and not as judgments (#3106).

    There is one for each fill group that filled anything, in the order the groups filled. ``collateral`` is the
    canonical requirement of what was held (``cover_book.collateral``), so it is nothing for a setup that sells
    nothing. ``obligation_after_usd`` is the assignment obligation of what was held after the group: sold contracts
    times strike times multiplier, gross, and not a loss.

    ``bought_back_with_cover_held`` is whether the group bought back a sold leg while a bought contract of that
    kind and expiry stayed held after the group. It is always false for an opening group.
    """

    at_ms: int
    group: FillGroup
    cash_before_usd: Decimal
    cash_after_usd: Decimal
    collateral_before_usd: Decimal
    collateral_after_usd: Decimal
    obligation_after_usd: Decimal
    bought_back_with_cover_held: bool


@dataclass(frozen=True)
class SessionOutcome:
    """One session replayed: its trades, what happened, and the cash that moved.

    ``cash_change_usd`` counts only cash that moved, so a leg marked has paid out (a bought leg) or been paid (a
    sold leg) and not been settled. ``pnl_usd`` counts that leg at its mark, and ``marked_value_usd`` is the
    difference: ``pnl_usd == cash_change_usd + marked_value_usd``, to the cent, on every outcome. A caller that
    carries cash from one session to the next carries ``cash_change_usd`` and ``marked_value_usd``, and reports
    ``pnl_usd``.

    Only an entry is checked against the cash. A close is never refused and its fee is paid even when it fills at
    nothing, so the cash at the session's end (the start plus ``cash_change_usd``) can be below zero by a closing
    fee, and below the collateral of what is then held by the cost of a buy-back.

    ``money_steps`` are not stored with a saved run: what a later reader judges of the funding it works out as the
    run is replayed (#3108), so a run read back has none, and they are left out of equality so that a run read
    back is equal to the run that was saved.
    """

    status: SessionStatus
    skip_reason: SkipReason | None
    trades: tuple[LegTrade, ...]
    events: tuple[Event, ...]
    # Cash at the end less cash at the start.
    cash_change_usd: Decimal
    money_steps: tuple[MoneyStep, ...] = field(default=(), compare=False)

    @property
    def pnl_usd(self) -> Decimal:
        """The sum of the trades' profit, a leg marked counted at its mark."""
        with localcontext(fills.EXACT):
            return sum((trade.pnl_usd for trade in self.trades), start=Decimal(0))

    @property
    def marked_value_usd(self) -> Decimal:
        """What the legs marked are worth at their marks, signed: a bought leg's mark counts for the run and a sold
        leg's, an amount owed, counts against it. Zero when no leg is marked."""
        with localcontext(fills.EXACT):
            marked = Decimal(0)
            for trade in self.trades:
                if trade.marked:
                    value = fills.order_value(trade.exit_price, multiplier=trade.multiplier, contracts=trade.contracts)
                    marked += -value if trade.is_sold_leg else value
            return marked

    @property
    def exit_counts(self) -> dict[ExitReason, int]:
        """How many trades left for each reason."""
        return dict(Counter(trade.exit_reason for trade in self.trades))

    @property
    def event_counts(self) -> dict[EventKind, int]:
        """How many times each thing happened."""
        return dict(Counter(event.kind for event in self.events))
