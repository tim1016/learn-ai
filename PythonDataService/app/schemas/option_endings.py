"""How an option position ended with no order of the bot's, as a screen reads it (ADR 0076 decision 22).

History reads it on the run that held the contract, and Activity on the
record of the event. Both are handed the same closed set, the same facts as
fields, and the same sentence, written by the server.

Every figure a screen shows is a field, so no screen parses the sentence or
does arithmetic. Money is whole cents, and beside each amount of cents is
the same amount written out in dollars (``…_usd``) for a screen to show as
it is. A price a share and a strike are exact decimals, each with its
``…_usd`` too. A time is ``int64 ms UTC``.

Two fields are dates and not instants: ``ended_on_ms`` and the contract's
``expiry_ms``. A screen shows each in the date-only New York mode, so it can
never shift a day for a viewer west of New York.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs

#: The closed set. ``sold_outside`` is an order the Clerk did not place: the broker's own sale, or the owner's by hand.
#: ``settled_in_cash`` is a simulation's own rule and never a broker's: a Dry Run sold the contract at its
#: exercise value at the close of its last session. ``ended_by_owner`` is a contract past its last session
#: that nothing would ever settle, counted as worth nothing at the owner's word.
OptionEndingKind = Literal["expired", "exercised", "assigned", "sold_outside", "settled_in_cash", "ended_by_owner"]


class OptionEndingContract(BaseModel):
    """The contract, as every option view serves one: for the shared renderer, which writes its name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The contract's code at the broker.
    occ_symbol: str
    underlying: str
    #: The expiry date's 16:00 New York anchor: a date, to be shown in the date-only New York mode.
    expiry_ms: EpochMs
    right: Literal["call", "put"]
    strike: Decimal


class OptionEndingSettlementPrice(BaseModel):
    """The underlying's price a simulation settled a contract from, and when that price is from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying: str
    #: Dollars a share: the close of one bar the simulation kept.
    price: Decimal
    price_usd: str
    #: When that bar ended. An instant.
    bar_ended_at_ms: EpochMs
    #: When the contract's last session closed. An instant.
    last_session_close_ms: EpochMs
    #: How long before that close the bar ended. Nothing for the closing bar itself.
    before_close_ms: int = Field(ge=0)


class OptionEndingView(BaseModel):
    """One option position that ended with no order of the bot's."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: OptionEndingKind
    contract: OptionEndingContract
    #: Contracts the bot lost: one or more.
    contracts: int = Field(ge=1)
    #: The side of the lots that ended: the bot had bought them, or sold them.
    held: Literal["bought", "sold"]
    #: The date the position ended, as 00:00 New York time on it. A date, not an instant: to be shown in the
    #: date-only New York mode. Never the day the Clerk read or booked it, which may be days later.
    ended_on_ms: EpochMs
    #: When the books recorded it. An instant.
    recorded_at_ms: EpochMs
    #: True when a simulation ended it by its own rule. No broker reported it.
    simulated: bool
    #: Shares the bot's books gained (positive) or gave up (negative). ``None`` when it moved none.
    shares: int | None = None
    #: The symbol those shares are in, their price a share, and the cash they came to: paid is below zero.
    share_symbol: str | None = None
    share_price: Decimal | None = None
    share_price_usd: str | None = None
    share_cash_cents: int | None = None
    share_cash_usd: str | None = None
    #: What the contracts were closed at, a share, when that is above nothing: a sale by an order the Clerk did
    #: not place, or a simulation's settlement in cash. ``None`` for a position that ended at nothing.
    close_price: Decimal | None = None
    close_price_usd: str | None = None
    #: What that close came to for these contracts: received is above zero, paid to buy back is below. ``None``
    #: with no close price, and when the amount is not whole cents.
    close_cash_cents: int | None = None
    close_cash_usd: str | None = None
    #: The price a simulation settled from. ``None`` on a real account, and for a contract with no such price.
    settled_from: OptionEndingSettlementPrice | None = None
    #: Who ended it, for a contract ended at the owner's word, and why no settlement could be made.
    ended_by_operator: str | None = None
    no_settlement_reason: str | None = None
    #: What happened, in one sentence. It names no contract code: the contract is ``contract``.
    headline: str = Field(min_length=1)
