"""What the order one option decision made has filled, as the Clerk's books have it (ADR 0076).

A caller that handed the Clerk a decision asks what came of it by the
decision's own name. It does not take an order out of the receipt by where
it stands there: an EXIT's receipt names the entry it closes before the
order that closed it, and names only the entry while no closing order has
been made.

The answer is read from the books. A decision's own order is the one its
effect made: the opening order of an ENTER, the closing order of an EXIT.
Its fills are the fills in effect for that order, oldest first, each on the
contract and with the multiplier the books record. Where a simulation
answered the order, its record of how it priced the fill goes with it.

Nothing is computed here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.enter import enter_command_id

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
    from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOptionFill

# The role of the order a decision's effect makes, by the command's action.
_OWN_ORDER_ROLE = {"ENTER": "ENTRY", "EXIT": "REDUCING"}


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionDecisionFill:
    """One fill of the order an option decision made."""

    # The order that filled: an ENTER's opening order, an EXIT's closing order.
    order_ref: str
    # The contract's OCC symbol, and the shares one contract's premium stands for, as the books record it.
    contract: str
    multiplier: int
    side: Literal["buy", "sell"]
    # Contracts filled, and the premium in dollars a share, as the books' fill row carries them.
    quantity: float
    price: float
    filled_at_ms: int
    # How a simulation priced the order, and how old the quote side it used was then. None for an order
    # no simulation answered.
    fill_model: str | None = None
    quote_age_ms: int | None = None


def option_fills_of_decision(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    durable_decision_id: str,
    how_simulated: Mapping[str, SynthesizedOptionFill],
) -> tuple[OptionDecisionFill, ...]:
    """The fills of the order ``durable_decision_id`` made for one bot, oldest first.

    Empty when the Clerk knows no such decision, refused it, has made no
    order for it yet (an EXIT held for the session's open), or the order has
    not filled. A decision in shares has no option fill.
    """
    command = repo.get_command(enter_command_id(strategy_instance_id, durable_decision_id))
    role = None if command is None else _OWN_ORDER_ROLE.get(command.action)
    if command is None or role is None or command.effect_operation_id is None:
        return ()
    own = {
        order.order_ref
        for order in repo.orders_for_effect_operation(command.effect_operation_id)
        if order.role == role
    }
    if not own:
        return ()
    own.update(leg.order_ref for parent in tuple(own) for leg in repo.order_legs(parent))
    with repo._write_lock:
        records = effective_fill_records(
            repo._conn, account_id=repo.account_id, strategy_instance_ids=(strategy_instance_id,)
        )
    fills: list[OptionDecisionFill] = []
    for record in records:
        if record.order_ref not in own or record.multiplier is None:
            continue
        simulated = how_simulated.get(record.order_ref)
        fills.append(
            OptionDecisionFill(
                order_ref=record.order_ref,
                contract=record.symbol,
                multiplier=record.multiplier,
                side=record.side.value,
                quantity=record.quantity,
                price=record.fill_price,
                filled_at_ms=record.filled_at_ms,
                fill_model=None if simulated is None or simulated.fill_model is None else simulated.fill_model.value,
                quote_age_ms=None if simulated is None else simulated.quote_age_ms,
            )
        )
    return tuple(fills)


__all__ = ["OptionDecisionFill", "option_fills_of_decision"]
