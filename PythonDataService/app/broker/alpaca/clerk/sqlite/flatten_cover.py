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

from app.broker.alpaca.clerk.program_leg import LegRefusal
from app.broker.alpaca.clerk.sqlite.account_open_work import (
    broker_order_in_flight,
    broker_quantity_by_symbol,
    open_order_snapshot_is_full,
)
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerOrder, BrokerPosition, OrderSide

FLATTEN_NOT_COVERED_AT_BROKER = "FLATTEN_NOT_COVERED_AT_BROKER"
"""Alpaca holds less than the Flatten would sell, once its open orders are set against the position."""

FLATTEN_COVER_UNPROVEN = "FLATTEN_COVER_UNPROVEN"
"""The Clerk could not work out what Alpaca holds free: no read, a full order page, an order with no quantity."""

FLATTEN_COVER_NEXT_STEPS: dict[str, str] = {
    FLATTEN_NOT_COVERED_AT_BROKER: (
        "Run Reconcile now to see what differs at Alpaca, then prepare the flatten again."
    ),
    FLATTEN_COVER_UNPROVEN: (
        "Prepare the flatten again; if it is refused again, run Reconcile now."
    ),
}
"""What the operator can do about each refusal, keyed by its code.

One step per code, so a refusal read back from the EXIT it failed
(``exit_resolution.flatten_send_refusal``) carries the step it was raised with.
"""

type AccountOpenWork = tuple[list[BrokerOrder], list[BrokerPosition]]
"""The account's open orders and positions, read in that sequence."""


def cover_after_open_orders(*, position: float, side: OrderSide, competing_unfilled: float) -> float:
    """What the account holds free for a ``side`` reduction, after its open orders.

    Formula: ``cover = s * position - competing_unfilled``, with ``s = +1``
      for a sell and ``-1`` for a buy that covers a short, and
      ``competing_unfilled = sum_i max(quantity_i - filled_i, 0)`` over every
      open order ``i`` in the symbol that is not on the other side. A
      reduction of ``q`` is covered when ``q - cover < POSITION_QTY_EPSILON``.
    Reference: issue #2839 (owner decision: the sale is covered at Alpaca).
    Canonical implementation: this file.
    Validated against:
      ``tests/broker/alpaca/clerk/sqlite/test_flatten_send_cover.py::test_a_sells_cover_counts_only_what_can_still_take_its_shares``
      and ``::test_a_buy_that_covers_a_short_is_held_to_the_mirrored_rule``.
    """
    signed = position if side is OrderSide.SELL else -position
    return signed - competing_unfilled


def flatten_cover_refusal(
    *,
    symbol: str,
    side: OrderSide,
    quantity: float,
    observed: AccountOpenWork | BrokerError | None,
) -> LegRefusal | None:
    """Why a ``side`` reduction of ``quantity`` ``symbol`` must not be sent, or ``None`` to send it.

    ``observed`` is the account read taken for this send, the error that read
    ended in, or ``None`` when the Clerk had no port to read with -- which
    refuses like any other read it could not make.

    An open order competes for the position unless it is in another symbol
    or provably on the other side: a sell whoever placed it, and equally an
    order whose side the broker left unreadable. One that states no quantity
    refuses the send, because what it takes cannot be subtracted. An order
    naming no symbol at all (a multi-leg parent, whose legs are listed as
    their own orders) competes for nothing.
    """
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
        return _unproven(
            "The Clerk could not read the Alpaca account just before sending this flatten "
            f"({observed}), {unconfirmed}"
        )
    broker_orders, broker_positions = observed
    if open_order_snapshot_is_full(broker_orders):
        return _unproven(
            f"Alpaca returned {len(broker_orders)} open orders, the most one read returns, "
            f"so there may be more than the Clerk saw, and {unconfirmed}"
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
        competing_unfilled += max(order.quantity - order.filled_quantity, 0.0)
    position = broker_quantity_by_symbol(broker_positions).get(symbol.upper(), 0.0)
    shortfall = quantity - cover_after_open_orders(
        position=position, side=side, competing_unfilled=competing_unfilled
    )
    if shortfall <= 0 or not position_quantity_is_nonzero(shortfall):
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
    "FLATTEN_COVER_NEXT_STEPS",
    "FLATTEN_COVER_UNPROVEN",
    "FLATTEN_NOT_COVERED_AT_BROKER",
    "AccountOpenWork",
    "cover_after_open_orders",
    "flatten_cover_refusal",
]
