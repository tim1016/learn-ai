"""A quote as it was received, and the tests it must pass before a decision may use it.

There is no snapshot of a chain at an instant. There is the newest bid and the
newest ask received for each contract, each with the time the provider's
callback delivered it, so each side has its own age.

A quote is usable when every test here passes. The tests are asked in one
order and the first that fails is the quote's fault, so a quote is refused as
one thing: an old generation's quote is not also called stale.

The first two tests ask whether a whole quote has arrived since the last
reconnect. One that has not is ``NOT_YET_ARRIVED``: there is nothing wrong
with its market, and nothing is known of it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class QuoteDataType(StrEnum):
    """What kind of data the provider says a quote is. Only ``LIVE`` is used for a decision."""

    LIVE = "live"
    FROZEN = "frozen"
    DELAYED = "delayed"
    DELAYED_FROZEN = "delayed_frozen"


@dataclass(frozen=True, slots=True)
class QuoteSide:
    """One side of a quote: the price per share, and when the provider's callback delivered it."""

    price: Decimal
    received_ms: int


@dataclass(frozen=True, slots=True)
class HeldQuote:
    """The newest quote held for one instrument. A side nothing has arrived for is ``None``."""

    bid: QuoteSide | None
    ask: QuoteSide | None
    data_type: QuoteDataType
    # The subscription generation it arrived in. A reconnect starts a new one, and every older quote is unusable.
    generation: int


class QuoteFault(StrEnum):
    """The first test a quote fails, in the order the tests are asked."""

    # It arrived before the last reconnect.
    OLD_GENERATION = "QUOTE_OLD_GENERATION"
    # A side has not arrived.
    ONE_SIDED = "QUOTE_ONE_SIDED"
    # It is delayed or frozen data.
    NOT_LIVE = "QUOTE_NOT_LIVE"
    # A side is not a number, or is below zero.
    NOT_A_PRICE = "QUOTE_NOT_A_PRICE"
    # The ask is zero: nothing is offered.
    ASK_NOT_ABOVE_ZERO = "QUOTE_ASK_NOT_ABOVE_ZERO"
    # The bid is zero where something is to be sold at it.
    BID_NOT_ABOVE_ZERO = "QUOTE_BID_NOT_ABOVE_ZERO"
    # The bid is above the ask.
    CROSSED = "QUOTE_CROSSED"
    # A side is older than the limit, or was received after the moment it is used at.
    STALE = "QUOTE_STALE"


# The faults of a quote that has not arrived whole in the current generation.
NOT_YET_ARRIVED = frozenset({QuoteFault.OLD_GENERATION, QuoteFault.ONE_SIDED})


def quote_fault(
    quote: HeldQuote, *, at_ms: int, generation: int, max_age_ms: int, needs_a_bid: bool
) -> QuoteFault | None:
    """The first test ``quote`` fails for use at ``at_ms``, or ``None`` when it passes them all.

    ``needs_a_bid`` is true where the bid is a price something would be sold
    at: a leg to be sold, or the underlying. A bought option's bid may be
    zero, as a far out-of-the-money one's often is.
    """
    if quote.generation != generation:
        return QuoteFault.OLD_GENERATION
    if quote.bid is None or quote.ask is None:
        return QuoteFault.ONE_SIDED
    if quote.data_type is not QuoteDataType.LIVE:
        return QuoteFault.NOT_LIVE
    bid, ask = quote.bid.price, quote.ask.price
    if not (bid.is_finite() and ask.is_finite()) or bid < 0 or ask < 0:
        return QuoteFault.NOT_A_PRICE
    if ask == 0:
        return QuoteFault.ASK_NOT_ABOVE_ZERO
    if needs_a_bid and bid == 0:
        return QuoteFault.BID_NOT_ABOVE_ZERO
    if bid > ask:
        return QuoteFault.CROSSED
    if not all(0 <= at_ms - side.received_ms <= max_age_ms for side in (quote.bid, quote.ask)):
        return QuoteFault.STALE
    return None


def receipt_times(quote: HeldQuote) -> tuple[int, ...]:
    """When each side that has arrived was received."""
    return tuple(side.received_ms for side in (quote.bid, quote.ask) if side is not None)
