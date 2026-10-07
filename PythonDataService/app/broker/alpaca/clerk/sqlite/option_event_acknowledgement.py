"""The owner's acknowledgement of an option-event hold (ADR 0076 decision 22).

Most option-event holds name a row of the broker's that the Clerk could not
book as it reads, or booked and then noted something about. The row stays
inside the read's window, so such a hold never ends by itself. The owner
ends it by saying they have looked: an acknowledgement.

**What it is not.** It books nothing and moves no position. It is no way to
make the books agree with the broker, and it must never hide that they do
not. So it is given only when all of these hold:

- the hold is one that an acknowledgement ends. A hold that ends another
  way is refused, with the way it does end;
- the request names the notice the owner read: its id, when it was last
  written, and each thing it named. A notice that has changed since, or
  names other things, is not the one they looked at;
- a reconciliation pass made for the request compared the account with the
  broker, nothing was written to the books since, and for every contract or
  underlying the hold names, and the symbol its bot trades, the broker's
  position equals the books'. A symbol that differs is named;
- who acknowledges is this installation's operator, never a field of the
  request.

A draining lane refuses it, as it refuses the review of an outside order:
neither can stop a bot or reduce what it holds.

The acknowledgement is the hold's own resolution, hash-chained. It covers
each thing the hold named and what was wrong with it then
(``option_event_books.AcknowledgedFinding``): the same row with a new fault
is held for again.

This module is a leaf: which holds are acknowledged, the request, the
receipt, the refusals, and what a hold's items name. The act itself is
``option_events.acknowledge_option_event_hold``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    OPTION_EVENT_HOLD_REASON_CODES,
    OPTION_EVENTS_NOT_READ_REASON_CODE,
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE,
    OPTION_ORDER_NOT_OURS_REASON_CODE,
    OPTION_SHARES_HELD_REASON_CODE,
    OptionEventHoldItem,
)
from app.options.contract import OptionContract

# The holds an acknowledgement does not end, each with what does. Said to the owner who asks.
ENDS_ANOTHER_WAY: dict[str, str] = {
    OPTION_EVENTS_NOT_READ_REASON_CODE: (
        "It ends by itself at the next whole read of the account's option events."
    ),
    OPTION_SHARES_HELD_REASON_CODE: (
        "It ends when the bot holds no shares. Stop the bot and flatten it."
    ),
    OPTION_ORDER_NOT_OURS_REASON_CODE: (
        "It ends when the order has ended and what it filled is booked. While the order has filled nothing, "
        "marking the order itself reviewed releases it."
    ),
    OPTION_EXPIRED_NOT_SETTLED_REASON_CODE: (
        "The books still hold a contract that has expired, and an acknowledgement would leave it there. It ends "
        "when the broker's record of the contract's end is booked, or when you end the contract at your word."
    ),
}
# The holds the owner ends by acknowledging them.
ACKNOWLEDGEABLE_REASON_CODES: tuple[str, ...] = tuple(
    code for code in OPTION_EVENT_HOLD_REASON_CODES if code not in ENDS_ANOTHER_WAY
)

# Why an acknowledgement was refused, each under its own code.
ENDS_BY_OTHER_MEANS = "OPTION_EVENT_HOLD_ENDS_ANOTHER_WAY"
NOT_OPEN = "OPTION_EVENT_HOLD_NOT_OPEN"
NOTICE_CHANGED = "OPTION_EVENT_HOLD_CHANGED_SINCE_READ"
NAMES_OTHER_ITEMS = "OPTION_EVENT_HOLD_NAMES_OTHER_ITEMS"
ACCOUNT_NOT_COMPARED = "OPTION_EVENT_HOLD_ACCOUNT_NOT_COMPARED"
BOOKS_MOVED = "OPTION_EVENT_HOLD_BOOKS_MOVED_SINCE_THE_PASS"
POSITIONS_DIFFER = "OPTION_EVENT_HOLD_POSITIONS_DIFFER"
NO_OPERATOR = "OPTION_EVENT_HOLD_NO_OPERATOR"


@dataclass(frozen=True, slots=True)
class HoldAcknowledgementRequest:
    """The owner's word that they have looked at one option-event hold, as they read it."""

    # The bot the hold is raised for. None is the account's own hold.
    strategy_instance_id: str | None
    reason_code: str
    # The notice the owner read: its id, and when it was last written. A notice that was
    # written again since is not the one they read.
    uncertainty_id: str
    observed_at_ms: int
    # Each thing the notice named, as the owner read it.
    item_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HoldAcknowledgementReceipt:
    """The books' record of one acknowledged option-event hold."""

    strategy_instance_id: str | None
    reason_code: str
    item_refs: tuple[str, ...]
    operator: str
    sequence: int
    recorded_at_ms: int


class OptionEventHoldNotAcknowledged(Exception):
    """An option-event hold was not acknowledged. Nothing was written."""

    def __init__(
        self, refusal_code: str, message: str, *, symbols: Iterable[str] = (), item_refs: Iterable[str] = ()
    ) -> None:
        super().__init__(message)
        self.refusal_code = refusal_code
        # The symbols in which the broker's position differs from the books', when that is why.
        self.symbols = tuple(symbols)
        # What the open hold names now, when it is not what the request named.
        self.item_refs = tuple(item_refs)


class ComparedWithTheBroker(Protocol):
    """What one reconciliation pass found when it compared the account with the broker."""

    @property
    def verdict(self) -> str: ...

    # The symbols in which the broker's position differs from the books' total.
    @property
    def drifted_symbols(self) -> tuple[str, ...]: ...

    # The symbols in which it differs while an order is still working there: not proven equal.
    @property
    def indeterminate_symbols(self) -> tuple[str, ...]: ...

    # The newest custody record the comparison saw. None when the pass reached no verdict.
    @property
    def through_sequence(self) -> int | None: ...


def refuse_what_an_acknowledgement_does_not_end(reason_code: str) -> None:
    """Refuse, by name, a hold that ends another way, and anything that is no option-event hold."""
    if reason_code in ACKNOWLEDGEABLE_REASON_CODES:
        return
    ends = ENDS_ANOTHER_WAY.get(reason_code)
    if ends is None:
        raise ValueError(f"{reason_code!r} is not an option-event hold")
    raise OptionEventHoldNotAcknowledged(
        ENDS_BY_OTHER_MEANS, f"This notice is not ended by an acknowledgement. {ends}"
    )


def symbols_a_hold_names(items: Iterable[OptionEventHoldItem], *, registered_symbol: str | None) -> tuple[str, ...]:
    """Every symbol whose position an acknowledgement of these items rests on.

    A contract an item names, and that contract's underlying, whose shares
    an exercise or an assignment moves. An underlying an item names itself,
    as a share trade does. And the symbol the hold's bot trades: its shares
    are counted there, whatever the contract's root spells.
    """
    named: list[str] = [] if registered_symbol is None else [registered_symbol.upper()]
    for item in items:
        if item.symbol is None:
            continue
        symbol = item.symbol.upper()
        named.append(symbol)
        try:
            named.append(OptionContract.from_occ_symbol(symbol).root)
        except ValueError:
            # No contract: an underlying, or a symbol the row carried that is neither.
            continue
    return tuple(dict.fromkeys(named))


__all__ = [
    "ACCOUNT_NOT_COMPARED",
    "ACKNOWLEDGEABLE_REASON_CODES",
    "BOOKS_MOVED",
    "ENDS_ANOTHER_WAY",
    "ENDS_BY_OTHER_MEANS",
    "NAMES_OTHER_ITEMS",
    "NOTICE_CHANGED",
    "NOT_OPEN",
    "NO_OPERATOR",
    "POSITIONS_DIFFER",
    "ComparedWithTheBroker",
    "HoldAcknowledgementReceipt",
    "HoldAcknowledgementRequest",
    "OptionEventHoldNotAcknowledged",
    "refuse_what_an_acknowledgement_does_not_end",
    "symbols_a_hold_names",
]
