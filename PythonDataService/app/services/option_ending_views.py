"""The served view of an option position that ended with no order of the bot's (ADR 0076 decision 22).

One function, read by History and by Activity, so both show the same closed
set, the same fields and the same sentence. The sentence is written here,
beside the view, from the books' own record. It names the contract by no
code: the screen writes the contract's name itself, with the one shared
renderer, from the contract the view serves.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.broker.alpaca.clerk.money import money_context, normalize_money, notional
from app.broker.alpaca.clerk.sqlite.option_event_books import RecordedOptionEnding
from app.broker.alpaca.clerk.sqlite.option_event_folds import (
    OWNER_WORD_ROWS_BOOKED,
    OWNER_WORD_ROWS_NOT_BOOKED,
    OptionEnding,
    OptionPositionEndedFacts,
)
from app.options.contract import OptionContract
from app.schemas.option_endings import OptionEndingContract, OptionEndingSettlementPrice, OptionEndingView
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

_MINUTE_MS = 60_000


def usd(cents: int) -> str:
    """Whole cents written out in dollars, as every option view writes money: ``$1,050.00``, ``-$58,500.00``."""
    return f"{'-' if cents < 0 else ''}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def price_usd(price: Decimal) -> str:
    """A price a share written out in dollars: to the cent when it is whole cents, in full when it is finer.

    ``585`` is ``$585.00`` and ``0.4`` is ``$0.40``. An average price finer
    than a cent, which a sale in two fills can have, keeps its digits:
    ``0.405`` is ``$0.405``.
    """
    with money_context():
        amount = normalize_money(price)
        cents = amount * 100
        if cents == cents.to_integral_value():
            return usd(int(cents))
        return f"{'-' if amount < 0 else ''}${format(abs(amount), 'f')}"


def _contracts(count: int) -> str:
    return f"{count} contract{'' if count == 1 else 's'}"


def _before_the_close(before_close_ms: int) -> str:
    """How long before the close a price is from, in words: "at the close", or the whole minutes before it."""
    minutes = before_close_ms // _MINUTE_MS
    if minutes == 0:
        return "at the close"
    return f"{minutes} minute{'' if minutes == 1 else 's'} before the close"


def _close_cash_cents(facts: OptionPositionEndedFacts, close_price: Decimal) -> int | None:
    """What the close came to for the bot's contracts, signed as its cash moved. None when not whole cents.

    Formula: contracts x close price a share x the contract's multiplier, in
      cents: received for a bought lot that was sold, paid for a sold lot
      bought back.
    Reference: ADR 0076 decision 13 (an option's notional).
    Canonical implementation: ``app/broker/alpaca/clerk/money.py::notional``. Nothing is worked out here but the sign.
    Validated against: tests/broker/alpaca/clerk/sqlite/test_option_event_screens.py
      (a sale at 0.40: $40.00 in, and $40.00 out for a buy-back; a settlement at 5.25: $525.00).
    """
    with money_context():
        cents = notional(facts.contracts, close_price, multiplier=facts.multiplier) * 100
        if cents != cents.to_integral_value():
            return None
        return int(cents) if facts.held == "bought" else -int(cents)


def _settled_from(simulation: dict[str, Any] | None) -> OptionEndingSettlementPrice | None:
    """The price a simulation's own record says it settled from, or None when the record names none."""
    if simulation is None or simulation.get("underlying_price") is None:
        return None
    price = Decimal(simulation["underlying_price"])
    return OptionEndingSettlementPrice(
        underlying=simulation["underlying"], price=price, price_usd=price_usd(price),
        bar_ended_at_ms=simulation["bar_end_ms"], last_session_close_ms=simulation["last_session_close_ms"],
        before_close_ms=simulation["bar_before_close_ms"],
    )


def describe_option_ending(facts: OptionPositionEndedFacts) -> str:
    """How one option position ended, in one sentence the owner reads. Written here, never on a screen.

    A simulation's settlement says so, with the underlying's price it read
    and how long before the close that price is from: no broker reported
    it, and a real account would have ended otherwise. An owner's ending
    made beside rows of the broker's says what they came to: the broker did
    report an end, and these books could not book it, or booked it and
    still held contracts it did not count.
    """
    ended_by = OptionEnding(facts.ended_by)
    one = facts.contracts == 1
    what, was = _contracts(facts.contracts), "was" if one else "were"
    read = _settled_from(facts.simulation)
    from_price = (
        "" if read is None
        else f" It read {read.underlying} at {read.price_usd}, {_before_the_close(read.before_close_ms)} of the "
        "contract's last session."
    )
    if ended_by is OptionEnding.EXPIRED:
        if facts.simulated:
            return f"{what} expired worth nothing. A simulation settled it by its own rule.{from_price}"
        return f"{what} expired worth nothing."
    if ended_by is OptionEnding.SETTLED_IN_CASH:
        close_price = Decimal(facts.close_price)
        cash = _close_cash_cents(facts, close_price)
        came_to = "" if cash is None else f": {usd(cash)}"
        return (
            f"{what} {was} sold by a simulation at {'its' if one else 'their'} exercise value, "
            f"{price_usd(close_price)} a share{came_to}. That is the simulation's rule, not a broker's.{from_price}"
        )
    if ended_by is OptionEnding.ENDED_BY_OWNER:
        word = facts.owner_word or {}
        why = ""
        if facts.simulated:
            why = " A simulation had no price to settle it from."
        if word.get(OWNER_WORD_ROWS_BOOKED):
            why += " The broker's record of the contract's end was booked and counted fewer contracts than the books held."
        if word.get(OWNER_WORD_ROWS_NOT_BOOKED):
            why += " The broker's record of how it ended could not be booked."
        return f"{what} {was} ended at the owner's word and counted as worth nothing. No settlement could be made.{why}"
    if ended_by is OptionEnding.SOLD_OUTSIDE:
        close_price = Decimal(facts.close_price)
        cash = _close_cash_cents(facts, close_price)
        done = "sold" if facts.held == "bought" else "bought back"
        came_to = "" if cash is None else f": {usd(abs(cash))}"
        return f"{what} {was} {done} at {price_usd(close_price)} a share by an order the Clerk did not place{came_to}."
    shares = facts.shares or {}
    quantity = shares.get("quantity", 0)
    cash_cents = shares.get("cash_cents", 0)
    return (
        f"{what} {was} {ended_by.value}: {abs(quantity)} {shares.get('symbol', '')} "
        f"{'bought' if quantity > 0 else 'sold'} at {price_usd(Decimal(shares.get('price', '0')))} a share, "
        f"{usd(abs(cash_cents))} {'paid' if cash_cents < 0 else 'received'}."
    )


def option_ending_view(ending: RecordedOptionEnding) -> OptionEndingView:
    """One recorded ending as a screen reads it: every figure as a field, and one sentence."""
    facts = ending.facts
    contract = OptionContract.from_occ_symbol(facts.contract_symbol)
    shares = facts.shares
    share_price = None if shares is None else Decimal(shares["price"])
    close_price = Decimal(facts.close_price)
    closed_at_a_price = close_price > 0
    close_cash = _close_cash_cents(facts, close_price) if closed_at_a_price else None
    word = facts.owner_word
    return OptionEndingView(
        kind=facts.ended_by,
        contract=OptionEndingContract(
            occ_symbol=contract.occ_symbol, underlying=contract.root, expiry_ms=contract.expiry_ms,
            right=contract.right, strike=contract.strike,
        ),
        contracts=facts.contracts,
        held=facts.held,
        # The row's own date. A sale by an order has none: it ended on the New York date it is counted on.
        ended_on_ms=(
            facts.event_date_ms
            if facts.event_date_ms is not None
            else et_midnight_ms(et_date_at_ms(facts.booked_as_of_ms))
        ),
        recorded_at_ms=ending.recorded_at_ms,
        simulated=facts.simulated,
        shares=None if shares is None else shares["quantity"],
        share_symbol=None if shares is None else shares["symbol"],
        share_price=share_price,
        share_price_usd=None if share_price is None else price_usd(share_price),
        share_cash_cents=None if shares is None else shares["cash_cents"],
        share_cash_usd=None if shares is None else usd(shares["cash_cents"]),
        close_price=close_price if closed_at_a_price else None,
        close_price_usd=price_usd(close_price) if closed_at_a_price else None,
        close_cash_cents=close_cash,
        close_cash_usd=None if close_cash is None else usd(close_cash),
        settled_from=_settled_from(facts.simulation),
        ended_by_operator=None if word is None else word["operator"],
        no_settlement_reason=None if word is None else word["why_no_settlement"],
        headline=describe_option_ending(facts),
    )


__all__ = ["describe_option_ending", "option_ending_view", "price_usd", "usd"]
