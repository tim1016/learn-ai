"""The Signal bot's held contracts, read beside its revision-bound custody (#3368)."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryLegFacts, held_option_legs, option_entry_of_order
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.options.contract import OptionContract
from app.schemas.action_plan import TradedOptionLegView
from app.schemas.broker_bots import BotStatusView


def with_held_structure(status: BotStatusView, repository: ClerkSqliteRepository) -> BotStatusView:
    traded = status.traded
    if status.bot_kind != "signal" or traded is None or traded.kind != "option":
        return status
    held = held_option_legs(repository, status.strategy_instance_id)
    terms: dict[tuple[str, int], OptionEntryLegFacts] = {}
    for order in reversed(repository.entry_orders_for_strategy(status.strategy_instance_id)):
        entry = option_entry_of_order(repository, order.order_ref)
        for leg in () if entry is None else entry.legs:
            key = (leg.contract.occ_symbol, leg.multiplier)
            if key in held and key not in terms:
                terms[key] = leg
        if len(terms) == len(held):
            break
    legs = [
        TradedOptionLegView(
            leg_id=None if key not in terms else terms[key].leg_id,
            contract=OptionContract.from_occ_symbol(key[0]),
            symbol=key[0],
            side="long" if quantity > 0 else "short",
            ratio=None if key not in terms else terms[key].ratio,
            multiplier=key[1],
            quantity=abs(quantity),
        )
        for key, quantity in held.items()
    ]
    notice = "No option contracts held. Contracts are selected when an entry is accepted." if not legs else None
    if len(terms) != len(held):
        notice = "Some held contracts have no readable entry leg or ratio. Quantities are the Clerk's holdings."
    return status.model_copy(update={"traded": traded.model_copy(update={"legs": legs, "legs_notice": notice})})
