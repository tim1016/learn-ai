"""The account activities that end an option position without an order (broker-neutral).

A sold option is assigned, a bought option is exercised, or an option
expires. No order of ours causes any of them, and a broker reports each as
an account activity: with a date and no time, against the account and no
lot. An exercise or an assignment also creates a trade in the underlying's
shares, which the broker may report as an activity of its own.

These are new types beside ``BrokerActivity`` in ``models.py``, which this
module leaves alone: that model is served and holds its numbers as floats.
Here a count is a whole number and money is an exact ``Decimal``, as in
``options.py`` (ADR 0076).

**An activity is read whole or not at all.** A row that cannot be is listed
as unreadable, with its id, the broker's code and the reason, and no figure:
it says such a row exists and nothing about what happened.

Time is ``int64`` ms UTC. An activity carries a date only. It is held as
00:00 New York time on that date, the anchor every date-only activity takes
at the adapter (ADR 0022 (a)): the date need not be a trading day, so no
session time exists for it, and 16:00 is an expiry's anchor alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from annotated_types import Gt, Le
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.options.contract import OptionContract
from app.options.event_allocation import (
    HeldOptionLot,
    OptionEventAllocation,
    OptionEventKind,
    OptionEventRefusal,
    OptionEventRefused,
    ShareTrade,
    allocate_option_event,
)
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms, et_midnight_ms


def _dollars(cents: int) -> Decimal:
    """Whole cents as dollars, exact whatever the ambient decimal context."""
    return Decimal(f"{cents}e-2")


def _a_new_york_date(activity_date_ms: int) -> int:
    if et_midnight_ms(et_date_at_ms(activity_date_ms)) != activity_date_ms:
        raise ValueError(
            f"{activity_date_ms} is not an activity date: a date is held as 00:00 New York time on that date"
        )
    return activity_date_ms


#: An ``int64 ms UTC`` field that must be a date-only activity's anchor: 00:00
#: New York time on its date. The bounds come first, as for an option expiry.
ActivityDateMs = Annotated[int, Gt(0), Le(MAX_TIMESTAMP_MS), AfterValidator(_a_new_york_date)]


class OptionActivityStatus(StrEnum):
    """Whether the broker stands by an activity. The words are Alpaca's reference's own three."""

    EXECUTED = "executed"
    # The reference lists this word and does not say what a row marked with it replaces.
    CORRECT = "correct"
    CANCELED = "canceled"


class _OptionActivityModel(BaseModel):
    """Base for what a broker reported: frozen, and unknown fields are an adapter's typo."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class BrokerOptionEventActivity(_OptionActivityModel):
    """One assignment, exercise or expiry, as the broker reports it against the account."""

    broker: str
    # The broker's id for the activity. With the kind of record this is, it is the key an activity is taken once by.
    activity_id: str = Field(min_length=1)
    # The broker's own code for the activity, as written.
    activity_type: str = Field(min_length=1)
    kind: OptionEventKind
    status: OptionActivityStatus
    # The activity's date: 00:00 New York time on it. No time of day is known.
    activity_date_ms: ActivityDateMs
    contract: OptionContract
    # The change to the account's position in the contract, in whole contracts,
    # as the broker signs it: negative takes from a bought position, positive
    # from a sold one. Never zero.
    position_change: int
    # Dollars the event itself moved, exact as the broker wrote them: zero, or
    # ``None`` when the row carries none. The cash is the share trade's.
    net_amount: Decimal | None
    # The broker's link between sibling activities. ``None`` when the row carries none.
    group_id: str | None
    # When the broker created the row, later than the date it carries. ``None`` when the row does not say.
    created_at_ms: int | None
    observed_at_ms: int

    @model_validator(mode="after")
    def _a_change_its_kind_can_make(self) -> BrokerOptionEventActivity:
        if self.position_change == 0:
            raise ValueError("an option event changes a position by one contract or more, never none")
        # Only a bought option is exercised and only a sold one assigned. A row
        # signed the other way is something else, perhaps one being undone.
        if self.kind is OptionEventKind.EXERCISED and self.position_change > 0:
            raise ValueError(f"an exercise takes from a bought position; this one adds {self.position_change}")
        if self.kind is OptionEventKind.ASSIGNED and self.position_change < 0:
            raise ValueError(f"an assignment takes from a sold position; this one adds {-self.position_change}")
        # The books move no cash for the event itself. One that states some is
        # settled in a way they do not follow, and would leave them out by it.
        if self.net_amount is not None and self.net_amount != 0:
            raise ValueError(f"an option event moves no cash of its own; this one states {self.net_amount}")
        # An option cannot expire before its own expiry date. A row that says
        # so is about something else, and would close lots still alive.
        if self.kind is OptionEventKind.EXPIRED and self.activity_date < self.contract.expiry_date:
            raise ValueError(
                f"an option expires on its expiry date, {self.contract.expiry_date.isoformat()}, or is reported "
                f"after it; this expiry is dated {self.activity_date.isoformat()}"
            )
        return self

    @property
    def contracts(self) -> int:
        """Whole contracts the event ends: one or more."""
        return abs(self.position_change)

    @property
    def activity_date(self) -> date:
        return et_date_at_ms(self.activity_date_ms)


class BrokerOptionShareTradeActivity(_OptionActivityModel):
    """The trade in the underlying's shares an exercise or assignment created, reported as its own activity.

    It names the underlying and not the contract. Which event it belongs to
    is not on it, unless the broker gives both one ``group_id``.
    """

    broker: str
    activity_id: str = Field(min_length=1)
    activity_type: str = Field(min_length=1)
    status: OptionActivityStatus
    activity_date_ms: ActivityDateMs
    # The underlying's symbol as the broker writes it.
    underlying_symbol: str = Field(min_length=1)
    # Whole shares bought (positive) or sold (negative). Never zero.
    shares: int
    # Dollars a share, exact.
    price: Decimal
    # Dollars paid (negative) or received (positive), exact.
    net_amount: Decimal
    group_id: str | None
    created_at_ms: int | None
    observed_at_ms: int

    @model_validator(mode="after")
    def _a_trade_of_some_shares_at_a_price(self) -> BrokerOptionShareTradeActivity:
        if self.shares == 0:
            raise ValueError("a share trade is for one share or more, never none")
        if self.price <= 0:
            raise ValueError(f"a share trade's price is above zero; got {self.price}")
        return self

    @property
    def activity_date(self) -> date:
        return et_date_at_ms(self.activity_date_ms)


class BrokerOptionActivityUnreadable(_OptionActivityModel):
    """An option activity the broker reported that could not be read whole.

    It carries what places the row and no figure from it. While one is
    outstanding, the account's option events are not all known.
    """

    broker: str
    activity_id: str = Field(min_length=1)
    activity_type: str = Field(min_length=1)
    # ``None`` when the row's date is what could not be read.
    activity_date_ms: ActivityDateMs | None
    # The symbol on the row, as the broker wrote it and unparsed: a contract's,
    # an underlying's, or one this app cannot name. ``None`` when it has none.
    symbol: str | None
    # What could not be read, in plain words.
    reason: str = Field(min_length=1)


class BrokerOptionEventEvidence(_OptionActivityModel):
    """A bounded read of the account's option events from one New York date on, newest first.

    ``history_complete`` and ``next_page_token`` mean what they mean on
    ``BrokerActivityEvidence``: complete only when the walk proved it reached
    the window's start, and otherwise the token continues it from where it
    stopped.

    **What a caller must do.** No event in the window means nothing happened
    only when ``read_whole`` is true. While it is false the caller holds: it
    follows ``next_page_token`` until the walk is complete, and it keeps a
    hold for every row in ``unreadable`` and every row in ``not_executed``
    until the owner has settled what that row did. Neither kind of row is
    ever booked. A row the broker marks ``correct`` or ``canceled`` stays in
    ``events`` or ``share_trades`` so it can be shown; ``allocation_of``
    refuses it.
    """

    events: tuple[BrokerOptionEventActivity, ...]
    share_trades: tuple[BrokerOptionShareTradeActivity, ...]
    unreadable: tuple[BrokerOptionActivityUnreadable, ...] = ()
    history_complete: bool
    next_page_token: str | None = None

    @property
    def not_executed(self) -> tuple[BrokerOptionEventActivity | BrokerOptionShareTradeActivity, ...]:
        """The events and share trades the broker does not report as executed.

        Alpaca's reference does not say which row a ``correct`` or a
        ``canceled`` one replaces or undoes, so the books cannot account
        for one.
        """
        return tuple(
            activity
            for activity in (*self.events, *self.share_trades)
            if activity.status is not OptionActivityStatus.EXECUTED
        )

    @property
    def read_whole(self) -> bool:
        """Whether every option activity in the window is in hand, was read, and is one the books can account for."""
        return self.history_complete and not self.unreadable and not self.not_executed


def in_allocation_order(events: Iterable[BrokerOptionEventActivity]) -> tuple[BrokerOptionEventActivity, ...]:
    """The events of one read in one fixed order: the oldest date first, on one date an expiry last, then by id.

    A read returns the newest first. This puts its rows in the same order
    however they arrived, so the same rows give the same allocation, row
    for row: of two assignments, the one dated earlier takes the older lot.

    It does not decide which bot is assigned. That is the rule's doing: an
    assignment or an exercise takes the oldest lots and an expiry the
    newest, whichever is matched first. And it orders only the rows it is
    given. An event that arrives in a later read is matched after these,
    whatever its date.
    """
    return tuple(
        sorted(
            events,
            key=lambda event: (event.activity_date_ms, event.kind is OptionEventKind.EXPIRED, event.activity_id),
        )
    )


def allocation_of(
    activity: BrokerOptionEventActivity, *, multiplier: int, lots: Sequence[HeldOptionLot]
) -> OptionEventAllocation:
    """The lots a broker's activity takes, with each owner's book effects.

    ``lots`` are every open lot of the activity's contract, across owners,
    and ``multiplier`` is that contract's. The rule is
    ``app.options.event_allocation.allocate_option_event``, which is given
    the activity's signed position change; this refuses, before it, an
    activity the broker does not report as executed.
    """
    if activity.status is not OptionActivityStatus.EXECUTED:
        raise OptionEventRefused(
            OptionEventRefusal.NOT_EXECUTED,
            f"The broker reports activity {activity.activity_id} as {activity.status.value}, not executed; "
            "what such a row does to a position is not known.",
            unmatched_contracts=activity.contracts,
        )
    return allocate_option_event(
        kind=activity.kind,
        contract=activity.contract,
        position_change=activity.position_change,
        event_date_ms=activity.activity_date_ms,
        multiplier=multiplier,
        lots=lots,
    )


def share_trade_disagreements(expected: ShareTrade, reported: BrokerOptionShareTradeActivity) -> tuple[str, ...]:
    """Where the share trade a broker reported differs from the one an event must create; empty when it agrees.

    ``expected`` is ``expected_share_trade`` of the event. The underlying is
    compared with the contract's root as written, so a share-class ticker,
    whose root drops the dot, reads as a difference. A share trade the broker
    does not report as executed agrees with nothing.
    """
    differences: list[str] = []
    if reported.status is not OptionActivityStatus.EXECUTED:
        differences.append(f"the broker reports the share trade as {reported.status.value}, not executed")
    if reported.underlying_symbol != expected.root:
        differences.append(f"the shares are in {reported.underlying_symbol}, and the contract's root is {expected.root}")
    if reported.shares != expected.shares:
        differences.append(f"{reported.shares} shares were reported, and the contract moves {expected.shares}")
    if reported.price != expected.price:
        differences.append(f"the reported price is {reported.price}, and the strike is {expected.price}")
    if reported.net_amount != _dollars(expected.cash_cents):
        differences.append(
            f"the reported cash is {reported.net_amount}, and the shares at the strike come to "
            f"{_dollars(expected.cash_cents)}"
        )
    return tuple(differences)
