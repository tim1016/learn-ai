"""Close the contracts an EXIT captured, without resolving the plan again (ADR 0076).

Formula: closing units = gcd(abs(held contracts)); closing ratios = abs(held)
  / units. Every side reverses the signed holding. An uneven entry therefore
  closes exactly its confirmed contracts, including a lone residual leg.
Reference: ADR 0076 decisions 16 and 27; Alpaca's coprime order ratios.
Canonical implementation: this composition; requests use
  ``app/broker/contract/options.py::closing_order``.
Validated against: tests/broker/alpaca/clerk/sqlite/test_multi_leg_close.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import gcd

from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryLegFacts, option_entry_of_order, option_exit_facts
from app.broker.alpaca.clerk.sqlite.order_evidence import entry_order_symbols
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import OrderSide
from app.broker.contract.options import MultiLegOptionOrderRequest, OptionOrderRequest, closing_order
from app.options.quotes import HeldQuote
from app.options.structure import ResolvedLeg, ResolvedStructure


def captured_positions(
    repo: ClerkSqliteRepository, strategy_instance_id: str, entries: Sequence[OrderResource],
) -> dict[str, float]:
    """Every captured symbol, including zero holdings. The caller holds the custody fence."""
    from app.broker.alpaca.clerk.options_legs import allocation_positions

    positions: dict[str, float] = {}
    for entry in entries:
        option = option_entry_of_order(repo, entry.order_ref)
        if option is not None and getattr(option, "options_attempt", None) is not None:
            for symbol, quantity in allocation_positions(repo, strategy_instance_id, entry.order_ref).items():
                positions[symbol] = positions.get(symbol, 0) + quantity
        else:
            positions.update({
                symbol: repo.position(strategy_instance_id, symbol)
                for symbol in entry_order_symbols(repo, entry.order_ref)
            })
    return positions


def captured_option_legs(
    repo: ClerkSqliteRepository, entries: Sequence[OrderResource],
) -> dict[str, OptionEntryLegFacts]:
    """The newest recorded contract and quote for each captured option symbol."""
    return {
        leg.contract.occ_symbol: leg
        for order in entries
        if (entry := option_entry_of_order(repo, order.order_ref)) is not None
        for leg in entry.legs
    }


def close_held_options(
    repo: ClerkSqliteRepository, strategy_instance_id: str, entries: Sequence[OrderResource],
) -> tuple[OptionOrderRequest, tuple[str, ...]]:
    """One exact market close and its leg ids. Raises on fractional or unrecorded holdings."""
    kept = captured_option_legs(repo, entries)
    held = {symbol: qty for symbol, qty in captured_positions(repo, strategy_instance_id, entries).items() if qty}
    if not held or any(symbol not in kept or not float(qty).is_integer() for symbol, qty in held.items()):
        raise ValueError("an option close needs whole confirmed contracts recorded by its captured entries")
    units = gcd(*(abs(int(qty)) for qty in held.values()))
    legs = tuple(
        ResolvedLeg(
            leg_id=kept[symbol].leg_id, contract=kept[symbol].contract,
            position="long" if qty > 0 else "short", ratio=abs(int(qty)) // units,
        )
        for symbol, qty in held.items()
    )
    return closing_order(ResolvedStructure(legs=legs), units=units), tuple(leg.leg_id for leg in legs)


@dataclass(frozen=True)
class OptionClose:
    request: OptionOrderRequest
    quotes: dict[str, tuple[HeldQuote, ...]]
    usable_generation: int | None


def order_reductions(request: OptionOrderRequest) -> tuple[tuple[str, OrderSide, int], ...]:
    """The exact quantities the request asks to reduce, by contract and closing side."""
    if isinstance(request, MultiLegOptionOrderRequest):
        return tuple((leg.contract.occ_symbol, leg.side, leg.ratio * request.quantity) for leg in request.legs)
    return ((request.contract.occ_symbol, request.side, request.quantity),)


def recorded_option_close(repo: ClerkSqliteRepository, effect_operation_id: str, created: ExitReducingOrderCreatedFacts) -> OptionClose | None:
    """Read the immutable request and the quotes its captured entries and decision kept."""
    from app.broker.alpaca.clerk.sqlite.facts import ExitAcceptedFacts

    entries = [order for order in repo.orders_for_effect_operation(effect_operation_id) if order.role == "ENTRY"]
    kept = captured_option_legs(repo, entries)
    if not kept:
        return None
    request = created.the_option_order
    if request is None:
        # Older and single-contract EXIT facts keep their original wire shape.
        from app.broker.contract.models import OrderType
        from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest

        leg = kept.get(created.symbol)
        if leg is None:
            raise ValueError(f"no entry of this EXIT's opened {created.symbol}")
        closes = OrderSide.SELL if leg.position == "long" else OrderSide.BUY
        if closes.value != created.side.lower():
            raise ValueError(f"closing what the entry opened is a {closes.value}, not a {created.side.lower()}")
        if not float(created.quantity).is_integer():
            raise ValueError("an option close is a whole number of confirmed contracts")
        request = SingleOptionOrderRequest(
            contract=kept[created.symbol].contract, side=OrderSide(created.side.lower()),
            intent=OptionPositionIntent.CLOSE, quantity=int(created.quantity), order_type=OrderType.MARKET,
        )
    acceptance = repo.first_effect_transition(effect_operation_id=effect_operation_id, transition_kind="EXIT_ACCEPTED")
    assert acceptance is not None
    decided = option_exit_facts(ExitAcceptedFacts.from_facts_json(acceptance["facts_json"]))
    return OptionClose(
        request=request,
        quotes={
            symbol: tuple(quote for quote in (
                None if decided is None else decided.quote_for(symbol), leg.quote.held,
            ) if quote is not None)
            for symbol, leg in kept.items()
        },
        usable_generation=None if decided is None else decided.generation,
    )
