"""Whether the Alpaca account covers an operator's Flatten, judged as the sale is sent (#2839).

An operator's Flatten is sized from the Clerk's own record of the position.
Just before its reducing order goes to a real broker, the Clerk reads the
account -- its open orders, then its positions
(``account_open_work.read_open_orders_then_positions``) -- and sends the order
only if the account still holds what it would sell once every open order that
could take the same shares is set against it. Anything less, and anything the
Clerk cannot work out, refuses the send: nothing reaches the broker.

Cover, not equality. Account reconciliation and the stuck-EXIT watchdog ask
whether the broker's position equals the Clerk's with nothing working on the
symbol (``reconcile.broker_symbol_reader``). A cohort Flatten sends its legs
on one symbol one after another, so that rule would refuse the second leg
behind the first leg's working order. Cover asks only that this sale cannot
carry the account past flat: a sibling's open sell leaves cover for it, and an
unknown open sell takes cover away.
"""

from __future__ import annotations

import math

from app.broker.alpaca.clerk.program_leg import LegRefusal
from app.broker.alpaca.clerk.sqlite.account_open_work import (
    OpenOrdersThenPositions,
    broker_order_in_flight,
    broker_quantity_by_symbol,
    open_order_snapshot_is_full,
)
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON, position_quantity_is_nonzero
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import OrderSide

FLATTEN_NOT_COVERED_AT_BROKER = "FLATTEN_NOT_COVERED_AT_BROKER"
"""Alpaca holds less than the Flatten would sell, once its open orders are set against the position."""

FLATTEN_COVER_UNPROVEN = "FLATTEN_COVER_UNPROVEN"
"""The Clerk could not work out what Alpaca holds free.

No read, a full order page, an open order with no symbol or no quantity, a
position whose side is neither long nor short, or a quantity that is not a
finite number.
"""

_POSITION_SIDES = frozenset({"long", "short"})

_RECONCILE_THEN_PREPARE_AGAIN = "Run Reconcile now, then prepare the flatten again."

FLATTEN_COVER_NEXT_STEPS: dict[str, str] = {
    FLATTEN_NOT_COVERED_AT_BROKER: _RECONCILE_THEN_PREPARE_AGAIN,
    FLATTEN_COVER_UNPROVEN: _RECONCILE_THEN_PREPARE_AGAIN,
}
"""What the operator can do about each refusal, keyed by its code.

The same step for both. A refused Flatten is the one EXIT of the plan it was
prepared as, and a refusal changes nothing that plan is made from: preparing
again before a new reconciliation presents the same plan, which is told this
refusal again with no new read of the account. Only a new reconciliation makes
a new plan, whose Flatten reads the account afresh.
"""

FLATTEN_COVER_HEADLINES: dict[str, str] = {
    FLATTEN_NOT_COVERED_AT_BROKER: (
        "A flatten was not sent: counting its open orders, the Alpaca account holds less than "
        "the flatten would close; the position is still open"
    ),
    FLATTEN_COVER_UNPROVEN: (
        "A flatten was not sent: the Clerk could not confirm what the Alpaca account holds; "
        "the position is still open"
    ),
}
"""The notice's headline for each refusal: what the check found, never that it checked when it could not."""


def cover_after_open_orders(*, position: float, side: OrderSide, competing_unfilled: float) -> float:
    """What the account holds free for a ``side`` reduction, after its open orders.

    Formula: ``cover = s * position - competing_unfilled``, with ``s = +1``
      for a sell and ``-1`` for a buy that covers a short, and
      ``competing_unfilled = sum_i max(quantity_i - filled_i, 0)`` over every
      open order ``i`` in the symbol that is not on the other side. A
      reduction of ``q`` is covered when ``q - cover < POSITION_QTY_EPSILON``,
      every quantity being finite.
    Reference: issue #2839 (owner decision: the sale is covered at Alpaca).
    Canonical implementation: this file.
    Validated against:
      ``tests/broker/alpaca/clerk/sqlite/test_flatten_send_cover.py::test_a_sells_cover_counts_only_what_can_still_take_its_shares``,
      ``::test_a_buy_that_covers_a_short_is_held_to_the_mirrored_rule`` and
      ``::test_a_position_that_is_not_a_finite_number_is_never_covered``.
    """
    signed = position if side is OrderSide.SELL else -position
    return signed - competing_unfilled


def flatten_cover_refusal(
    *,
    symbol: str,
    side: OrderSide,
    quantity: float,
    observed: OpenOrdersThenPositions | BrokerError | None,
) -> LegRefusal | None:
    """Why a ``side`` reduction of ``quantity`` ``symbol`` must not be sent, or ``None`` to send it.

    ``observed`` is the account read taken for this send, the error that read
    ended in -- alone, or beside the open orders when it was the positions
    that could not be read -- or ``None`` when the Clerk had no port to read
    with, which refuses like any other read it could not make.

    An open order competes for the position unless it is in another symbol
    or provably on the other side: a sell whoever placed it, and equally an
    order whose side the broker left unreadable. One that states no quantity
    refuses the send, because what it takes cannot be subtracted. So does one
    whose symbol could not be read: the adapter reads a missing symbol as
    blank, which is how Alpaca lists a multi-leg parent and equally how a
    malformed order in this very symbol reads, and an order row carries no
    order class and no legs to tell the two apart.

    Every row of the position in the symbol must state a side that is
    exactly long or short. The signed quantity reads every side but short as
    long, so a short reported under any other side would cover a sale.

    Every quantity in the rule must be a finite number -- the position, each
    competing order's quantity and filled quantity, and the reduction's own,
    which must also be above zero. One that is not refuses as unproven: a
    ``NaN`` compares false both ways, an infinite position would cover
    anything, and any account covers a reduction of nothing, so none may read
    as covered.
    """
    if not (math.isfinite(quantity) and quantity > 0):
        return _unproven(
            f"This flatten's own {symbol} quantity ({quantity:g}) is not a positive, finite number of shares; "
            "nothing was sent."
        )
    selling = side is OrderSide.SELL
    unconfirmed = (
        "so the Clerk could not confirm the account "
        + (
            f"holds the {quantity:g} {symbol} this flatten would sell"
            if selling
            else f"is short the {quantity:g} {symbol} this flatten would buy back"
        )
        + "; nothing was sent."
    )
    if observed is None:
        return _unproven(
            f"This Clerk has no connection to read the Alpaca account as it sends, {unconfirmed}"
        )
    if isinstance(observed, BrokerError):
        return _account_unread(observed, unconfirmed)
    broker_orders, broker_positions = observed
    if isinstance(broker_positions, BrokerError):
        return _account_unread(broker_positions, unconfirmed)
    if open_order_snapshot_is_full(broker_orders):
        return _unproven(
            f"Alpaca returned {len(broker_orders)} open orders, the most one read returns, "
            f"so there may be more than the Clerk saw, and {unconfirmed}"
        )
    for order in broker_orders:
        if broker_order_in_flight(order) and not order.symbol.strip():
            return _unproven(
                f"An open order at Alpaca ({order.order_id}) names no symbol the Clerk could read, "
                f"so whether it is working on {symbol} is unknown, and {unconfirmed} "
                "A flatten on this account is refused for as long as that order is open at Alpaca."
            )
    other_side = OrderSide.BUY if selling else OrderSide.SELL
    competing = [
        order
        for order in broker_orders
        if broker_order_in_flight(order)
        and order.symbol.upper() == symbol.upper()
        and order.side.lower() != other_side.value
    ]
    competing_unfilled = 0.0
    for order in competing:
        if order.quantity is None:
            return _unproven(
                f"An open {symbol} order at Alpaca ({order.order_id}) states no share quantity, "
                f"so how much of the position it takes is unknown, and {unconfirmed}"
            )
        if not (math.isfinite(order.quantity) and math.isfinite(order.filled_quantity)):
            return _unproven(
                f"An open {symbol} order at Alpaca ({order.order_id}) states a quantity that is not "
                f"a finite number, so how much of the position it takes is unknown, and {unconfirmed}"
            )
        competing_unfilled += max(order.quantity - order.filled_quantity, 0.0)
    for held in broker_positions:
        if held.symbol.upper() == symbol.upper() and held.side.lower() not in _POSITION_SIDES:
            return _unproven(
                f"Alpaca reported its {symbol} position with the side {held.side!r}, which is "
                f"neither long nor short, {unconfirmed}"
            )
    position = broker_quantity_by_symbol(broker_positions).get(symbol.upper(), 0.0)
    if not math.isfinite(position):
        return _unproven(
            f"Alpaca reported its {symbol} position as {position:g}, which is not a finite number "
            f"of shares, {unconfirmed}"
        )
    shortfall = quantity - cover_after_open_orders(
        position=position, side=side, competing_unfilled=competing_unfilled
    )
    # Covered only when this is affirmatively true: every quantity above is
    # finite, and a comparison that is not true refuses.
    if shortfall < POSITION_QTY_EPSILON:
        return None
    already_taken = (
        f" with {competing_unfilled:g} of it already in open {side.value} orders"
        if position_quantity_is_nonzero(competing_unfilled)
        else ""
    )
    short_of = (
        f"less than the {quantity:g} this flatten would sell"
        if selling
        else f"which does not cover the {quantity:g} this flatten would buy back"
    )
    return LegRefusal(
        reason_code=FLATTEN_NOT_COVERED_AT_BROKER,
        explanation=(
            f"Alpaca holds {_held_text(position, symbol)}{already_taken}, {short_of}; "
            "nothing was sent."
        ),
        next_step=FLATTEN_COVER_NEXT_STEPS[FLATTEN_NOT_COVERED_AT_BROKER],
    )


def account_lists_order(
    observed: OpenOrdersThenPositions | BrokerError | None, *, client_order_id: str
) -> bool:
    """Whether the account read lists an order carrying ``client_order_id`` (#2839).

    Asked of the Flatten's own reducing order before the cover rule: an order
    the read shows under that identity reached the broker, after an exact
    lookup answered that it had not. It is the sale itself, not a competitor
    for the position, so the caller neither sends it again nor refuses it.

    Answered from the open orders alone, whatever the positions read after
    them came to: a failed positions read refuses a send, and must never hide
    an order that is already working.
    """
    if observed is None or isinstance(observed, BrokerError):
        return False
    broker_orders, _broker_positions = observed
    return any(order.client_order_id == client_order_id for order in broker_orders)


def _account_unread(error: BrokerError, unconfirmed: str) -> LegRefusal:
    return _unproven(
        "The Clerk could not read the Alpaca account just before sending this flatten "
        f"({error}), {unconfirmed}"
    )


def _unproven(explanation: str) -> LegRefusal:
    return LegRefusal(
        reason_code=FLATTEN_COVER_UNPROVEN,
        explanation=explanation,
        next_step=FLATTEN_COVER_NEXT_STEPS[FLATTEN_COVER_UNPROVEN],
    )


def _held_text(position: float, symbol: str) -> str:
    """The broker's signed position in plain words: ``10 SPY``, ``no SPY``, ``a short of 10 SPY``."""
    if not position_quantity_is_nonzero(position):
        return f"no {symbol}"
    return f"{position:g} {symbol}" if position > 0 else f"a short of {abs(position):g} {symbol}"


__all__ = [
    "FLATTEN_COVER_HEADLINES",
    "FLATTEN_COVER_NEXT_STEPS",
    "FLATTEN_COVER_UNPROVEN",
    "FLATTEN_NOT_COVERED_AT_BROKER",
    "account_lists_order",
    "cover_after_open_orders",
    "flatten_cover_refusal",
]
