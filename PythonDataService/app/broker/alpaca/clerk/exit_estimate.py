"""What getting out of a held option position costs at the quotes held, before fees (#3100).

Two figures, because they answer two questions. Closing everything as one
order buys each sold leg back at its ask and sells each bought leg at its
bid: that is the net close. Buying back the sold legs alone costs the asks of
the sold legs only: the gross buy-back. Alpaca documents that buying a short
option back needs buying power for the buy-back itself, and that the relief
the close creates does not always fund it, so a $5-wide spread deep in the
money can close whole for $500 and need $10,000 to buy its sold leg back.

Formula: with ``n`` a contract's signed whole contracts (bought above zero),
  ``m`` its multiplier and ``bid`` and ``ask`` its newest quote a share:
  buy back (a sold leg)  = |n| · m · ask         paid
  sale (a bought leg)    = n · m · bid           received
  net close              = Σ over sold legs of buy back − Σ over bought legs of sale
                           positive is paid
  gross buy-back         = Σ over sold legs of buy back, zero with no sold leg
  A figure is unknown when a side it needs is no price, stale when a side it
  used was received more than ``HELD_MARK_MAX_AGE_MS`` before ``now_ms`` (or
  after it), and known otherwise. Every amount is whole cents and none is
  rounded: a price that is not a whole number of cents for the contract is no
  price. Fees are not in either figure.
Reference: PRD #3095 part B and issue #3100; Alpaca support, "Why can't I buy
  to close my short option" (buying to close does not always provide margin
  relief to fund the closing trade itself); ADR 0076 decision 28 for the side
  a close fills at, which ``app.options.simulated_fill`` states.
Canonical implementation: this file. The side each leg closes at and whether
  it is a price are ``app.options.simulated_fill.a_price_to_close_at``'s.
Validated against: tests/broker/alpaca/clerk/test_exit_estimate.py (every
  figure worked by hand).

The module reads no clock but ``now_ms``, no books and no broker. It takes
what the caller read: the legs held, the newest quote held for each contract,
and the closes the Clerk owes the position. It informs and blocks nothing.

A side's age counts from its own receipt, not from its hand-over, as a
simulated close tells the age of the quote it was priced from
(``simulated_fill``). The simulated account holds a mark to the same limit
from its hand-over, which a quiet contract's bid can outlive: such a side
reads stale here while the mark beside it is fresh. That errs toward saying a
figure is not current, never toward saying it is. A crossed quote (a bid above
its ask) is no price either, as ``app.options.quotes.quote_fault`` has it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from app.broker.alpaca.clerk.funding import Figure, FigureState, Term
from app.broker.alpaca.clerk.kept_option_quotes import HELD_MARK_MAX_AGE_MS
from app.options.deadlines import CloseKind
from app.options.exact import whole_units
from app.options.quotes import HeldQuote, QuoteDataType
from app.options.requirement import ContractAtMultiplier
from app.options.simulated_fill import a_price_to_close_at
from app.utils.et_words import et_when_words

QuoteSideName = Literal["bid", "ask"]

_CENTS_PER_DOLLAR = 100
_NO_SOLD_LEG = "No sold leg is held, so nothing is bought back"

FEES_LINE = "Amounts are before fees. A positive amount is paid and a negative one comes back."
BUY_BACK_ALONE_LINE = (
    "Buying back a sold leg alone is a separate order from closing the whole structure, "
    "and Alpaca does not always fund it from the relief the close creates."
)
CLOSE_UNREAD_LINE = "The time of this system's own scheduled close could not be read."
CUTOFF_LINE = "Alpaca's own expiry-day cutoff has not been verified."

_WHY_OWED: dict[CloseKind, str] = {
    CloseKind.EXPIRY_DAY: "a contract it holds expires that day",
    CloseKind.SOLD_LEG: "a sold option is not held overnight",
}


@dataclass(frozen=True, slots=True)
class OwedClose:
    """A close the Clerk owes the position, or one a session ended on: what it is for, and when it was due.

    ``left_to_owner`` is set when the Clerk has stopped exiting this bot by
    itself, so the close is the owner's to make and nothing here says the
    Clerk will make it. ``missed`` is set for a close whose session ended with
    the position still held.
    """

    kind: CloseKind
    due_ms: int
    left_to_owner: bool = False
    missed: bool = False


@dataclass(frozen=True, slots=True)
class LegQuality:
    """How far the quote behind one leg's price can be trusted.

    ``state`` is the leg's own: unknown when the side its close fills at is no
    price, stale when that side is past its limit, known otherwise. ``age_ms``
    is that side's age, ``None`` when no side arrived. ``missing`` lists every
    side that has not arrived, used or not, and ``gap`` is the quote's ask less
    its bid a share, ``None`` unless both sides arrived. A crossed quote is
    unknown, and shows its gap below zero as quoted.
    """

    symbol: str
    # Signed whole contracts held: bought above zero, sold below.
    contracts: int
    state: FigureState
    age_ms: int | None
    missing: tuple[QuoteSideName, ...]
    gap: Decimal | None


@dataclass(frozen=True, slots=True)
class ExitEstimate:
    """What it costs to get out, in two figures, with the quotes behind them and what is not settled."""

    # Closing everything as one order. Positive is paid.
    net_close: Figure
    # Buying back the sold legs alone, at the ask. Zero, known, with no sold leg.
    gross_buy_back: Figure
    quotes: tuple[LegQuality, ...]
    conditions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Leg:
    """One held contract worked out: its close in signed cents, and the quote it stands on."""

    quality: LegQuality
    label: str
    # Paid above zero. ``None`` when the side it needs is no price.
    cents: int | None
    # When the side used was received. ``None`` exactly when ``cents`` is.
    received_ms: int | None
    # Why ``cents`` is ``None``.
    unknown_because: str | None


def exit_estimate(
    legs: Mapping[ContractAtMultiplier, int],
    quotes: Mapping[str, HeldQuote | None],
    *,
    now_ms: int,
    quotes_from: str,
    none_held: str,
    owed_closes: Sequence[OwedClose] | None = (),
) -> ExitEstimate:
    """The cost of getting out of ``legs`` at ``quotes``, as of ``now_ms``.

    ``legs`` is what one bot holds, as ``held_requirement_cents`` takes it:
    signed whole contracts by ``(OCC symbol, multiplier)``. ``quotes`` is the
    newest quote held for each contract by OCC symbol, or ``None``, with each
    side's own receipt time. ``quotes_from`` says where they come from, and
    ``none_held`` why a contract can have none, in the owner's words: they
    become the source of a figure that is known and the reason of one that is
    not. ``owed_closes`` are the closes the Clerk owes this position, or
    ``None`` when they could not be read, which the conditions say.
    """
    held = _held(legs)
    worked = [
        _work(symbol, multiplier, contracts, quotes.get(symbol), now_ms=now_ms, none_held=none_held)
        for (symbol, multiplier), contracts in held.items()
    ]
    sold = [leg for leg in worked if leg.quality.contracts < 0]
    return ExitEstimate(
        net_close=_figure(worked, now_ms=now_ms, quotes_from=quotes_from),
        gross_buy_back=(
            _figure(sold, now_ms=now_ms, quotes_from=quotes_from)
            if sold
            else Figure(0, "known", _NO_SOLD_LEG)
        ),
        quotes=tuple(leg.quality for leg in worked),
        conditions=_conditions(has_sold_leg=bool(sold), owed_closes=owed_closes, now_ms=now_ms),
    )


def _held(legs: Mapping[ContractAtMultiplier, int]) -> dict[ContractAtMultiplier, int]:
    """The contracts actually held, in symbol order. A contract netting to zero holds nothing."""
    held: dict[ContractAtMultiplier, int] = {}
    for (symbol, multiplier), contracts in sorted(legs.items()):
        if isinstance(contracts, bool) or not isinstance(contracts, int):
            raise ValueError(f"{symbol} is held as {contracts!r}: option contracts are held in whole contracts")
        if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
            raise ValueError(f"the multiplier of {symbol} is a whole number, one or more; got {multiplier!r}")
        if contracts:
            held[symbol, multiplier] = contracts
    if not held:
        raise ValueError("an exit estimate is of a position that is held")
    return held


def _work(
    symbol: str, multiplier: int, contracts: int, quote: HeldQuote | None, *, now_ms: int, none_held: str
) -> _Leg:
    """One leg: a sold leg is bought back at its ask and a bought leg sold at its bid."""
    buys = contracts < 0
    side: QuoteSideName = "ask" if buys else "bid"
    label = f"Buy back {-contracts} {symbol} at the ask" if buys else f"Sell {contracts} {symbol} at the bid"

    def unknown(why: str, *, age_ms: int | None, missing: tuple[QuoteSideName, ...], gap: Decimal | None) -> _Leg:
        return _Leg(
            LegQuality(symbol, contracts, "unknown", age_ms, missing, gap), label, None, None, f"{symbol}: {why}",
        )

    if quote is None:
        return unknown(none_held, age_ms=None, missing=("bid", "ask"), gap=None)
    missing: tuple[QuoteSideName, ...] = tuple(
        name for name, held_side in (("bid", quote.bid), ("ask", quote.ask)) if held_side is None
    )
    gap = None if quote.bid is None or quote.ask is None else quote.ask.price - quote.bid.price
    if gap is not None and not gap.is_finite():
        gap = None
    used = quote.ask if buys else quote.bid
    age_ms = None if used is None else now_ms - used.received_ms
    if quote.data_type is not QuoteDataType.LIVE:
        return unknown(
            f"its quote is {quote.data_type.value.replace('_', ' ')} data, not live", age_ms=age_ms, missing=missing, gap=gap
        )
    if gap is not None and gap < 0:
        return unknown("its bid is above its ask, so the quote is crossed", age_ms=age_ms, missing=missing, gap=gap)
    if used is None:
        return unknown(f"no {side} has arrived", age_ms=age_ms, missing=missing, gap=gap)
    if not a_price_to_close_at(used, buys=buys):
        why = "nothing is offered" if used.price == 0 else f"its {side} is not a price"
        return unknown(why, age_ms=age_ms, missing=missing, gap=gap)
    try:
        cents = whole_units(
            used.price,
            units_per_one=multiplier * _CENTS_PER_DOLLAR * abs(contracts),
            what=f"the {side} of {symbol}",
            unit="cents",
        )
    except ValueError:
        return unknown(
            f"its {side} of {used.price} is not a whole number of cents at {multiplier} shares a contract",
            age_ms=age_ms, missing=missing, gap=gap,
        )
    assert age_ms is not None
    state: FigureState = "known" if 0 <= age_ms <= HELD_MARK_MAX_AGE_MS else "stale"
    return _Leg(
        LegQuality(symbol, contracts, state, age_ms, missing, gap),
        label, cents if buys else -cents, used.received_ms, None,
    )


def _figure(legs: Sequence[_Leg], *, now_ms: int, quotes_from: str) -> Figure:
    """The sum of ``legs``' closes: unknown when any leg is, stale when any leg is, and known otherwise."""
    unknown = [leg for leg in legs if leg.cents is None]
    if unknown:
        reasons = "; ".join(str(leg.unknown_because) for leg in unknown)
        return Figure.unknown(f"{quotes_from}, which cannot price this close: {reasons}")
    terms = tuple(Term(leg.label, leg.cents) for leg in legs if leg.cents is not None)
    received = [leg.received_ms for leg in legs if leg.received_ms is not None]
    state: FigureState = "stale" if any(leg.quality.state == "stale" for leg in legs) else "known"
    return Figure(sum(term.amount_cents for term in terms), state, quotes_from, min(received, default=now_ms), terms)


def _conditions(*, has_sold_leg: bool, owed_closes: Sequence[OwedClose] | None, now_ms: int) -> tuple[str, ...]:
    """What is not settled, in the owner's words: the fees, a buy-back alone, the Clerk's own close, the cutoff."""
    lines = [FEES_LINE]
    if has_sold_leg:
        lines.append(BUY_BACK_ALONE_LINE)
    if owed_closes is None:
        lines.append(CLOSE_UNREAD_LINE)
    closes = sorted(set(owed_closes or ()), key=lambda owed: (owed.due_ms, owed.kind.value, owed.left_to_owner, owed.missed))
    for close in closes:
        when = et_when_words(close.due_ms, now_ms=now_ms)
        why = _WHY_OWED[close.kind]
        if close.missed:
            lines.append(
                f"This system's own close of the position was due at {when}: {why}. "
                "That session ended with the position still held."
            )
        elif close.left_to_owner:
            lines.append(
                f"A close of the position is due at {when}: {why}. This system has stopped exiting this bot by "
                "itself, so the close is yours to make."
            )
        elif close.due_ms > now_ms:
            lines.append(f"This system closes the position itself at {when}: {why}.")
        else:
            lines.append(f"This system's own close of the position was due at {when}: {why}.")
    lines.append(CUTOFF_LINE)
    return tuple(lines)


__all__ = [
    "BUY_BACK_ALONE_LINE",
    "CLOSE_UNREAD_LINE",
    "CUTOFF_LINE",
    "FEES_LINE",
    "ExitEstimate",
    "LegQuality",
    "OwedClose",
    "exit_estimate",
]
