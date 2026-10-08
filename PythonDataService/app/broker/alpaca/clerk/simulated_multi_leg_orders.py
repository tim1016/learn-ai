"""The option order of several legs that a simulation fills itself (ADR 0076).

A simulation fills an option order from quotes, never from a bar. The Clerk
binds a quote to the order for each leg before it sends: a quote is bound to
the order's id and the leg's contract. This module fills an opening order of
two to four legs from those quotes:

- **every leg at once and in full, each at its own natural price**: a bought
  leg at its ask and a sold leg at its bid (``app.options.simulated_fill``);
- **or none**. A leg with no quote bound, a contract past its last session,
  a leg nobody bids for, or a quote that gives no price refuses the whole
  order with ``BrokerOrderRejected``, which names the contract. A net price
  the order's limit does not reach ends every leg unfilled on the spot,
  because a simulation cannot rest an order. **Never one leg.**

**In the ledger an order of several legs is its legs' records and nothing
else.** Each leg has one record, shaped as an option order of one leg in
that leg's contract, under the client order id the books give the leg
(``leg_order_ref(parent, n)``) and an execution identity of its own. So the
stock lookups, the open orders and the positions of the port show each leg
in its contract, each leg keeps its own model and quote age, and there is no
record with a blank symbol. All the legs' records are written in one ledger
transaction: they are all there or none is.

A leg's record states the order's own kind and no limit. The limit is the
order's, one signed net price for the whole structure, and is the books' to
keep. A leg taken by itself has none, so its request is written as a leg
filled at market.

The order itself is answered from its legs' records
(``multi_leg_order_view``): a ``BrokerOptionOrder`` with the legs nested
under it, as a broker answers. Its units are what the legs' contracts share:
an order's ratios share no divisor above one, so the greatest common divisor
of the legs' contracts is the units.

A closing order of several legs is not filled here yet. It is refused by
name; the piece that closes a structure adds that branch.

Formula: net price filled = Σ sᵢ · rᵢ · priceᵢ, with sᵢ = +1 for a leg
  bought and −1 for a leg sold and rᵢ its ratio: the same signed sum the
  order's net limit is (``app/broker/contract/options.py``). A limit order
  fills when net price filled ≤ its net limit: it pays no more, or receives
  no less.
Reference: ADR 0076 (what a simulation fills, and what it does not claim).
Canonical implementation: ``app/options/simulated_fill.py`` (each leg's
  price) and ``app/options/requirement.py`` (the net price); this file sums
  the fills by the same rule to hold the order to its limit.
Validated against: tests/broker/alpaca/clerk/test_simulated_multi_leg_orders.py.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from math import gcd
from typing import NamedTuple

from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.alpaca.clerk.sqlite.option_order_legs import leg_order_ref
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedOptionFill, SynthesizedOrderRecord
from app.broker.contract.errors import BrokerOrderRejected
from app.broker.contract.models import BrokerOrder, OrderSide, OrderType
from app.broker.contract.options import (
    BrokerOptionOrder,
    BrokerOptionOrderLeg,
    MultiLegOptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.deadlines import past_last_session
from app.options.exact import exact_sum
from app.options.simulated_fill import SimulatedLegFill, simulated_entry_fills
from app.options.structure import MAX_OPTION_LEGS, ResolvedLeg, ResolvedStructure


class BoundLegQuote(NamedTuple):
    """The quote one leg of an order is to be filled from, as the port bound it."""

    quote: RecordedQuote
    # Where it is kept, when it was read back from the quotes kept.
    quote_ref: str | None
    # The shares one contract's price stands for, as the Clerk's books record it. None for a close.
    multiplier: int | None


# Shapes one leg's own order as the Clerk folds every simulated order: the port's, so each world
# keeps its own order and execution identities.
LegOrderShaper = Callable[[SingleOptionOrderRequest, str, Decimal | None, str], BrokerOrder]


def leg_client_order_ids(parent_client_order_id: str, legs: int = MAX_OPTION_LEGS) -> tuple[str, ...]:
    """The client order ids the legs of the order sent as ``parent_client_order_id`` are recorded under."""
    return tuple(leg_order_ref(parent_client_order_id, number) for number in range(1, legs + 1))


def legs_recorded(
    latest: Mapping[str, SynthesizedOrderRecord], parent_client_order_id: str
) -> tuple[SynthesizedOrderRecord, ...]:
    """The legs' records of the order sent as ``parent_client_order_id``, in leg order; empty when it is not here."""
    found = [latest.get(leg_id) for leg_id in leg_client_order_ids(parent_client_order_id)]
    return tuple(record for record in found if record is not None and record.option is not None)


def filled_multi_leg_order(
    order: MultiLegOptionOrderRequest,
    *,
    client_order_id: str,
    bound: Mapping[str, BoundLegQuote],
    at_ms: int,
    broker_id: str,
    shape_leg_order: LegOrderShaper,
) -> tuple[tuple[BrokerOrder, SynthesizedOptionFill], ...]:
    """Each leg's order and how it was priced, for an opening order of several legs: every leg, or a refusal.

    ``bound`` is the quote bound for each leg, by its contract's symbol.
    What comes back is one (order, fill) a leg, in leg order, for the
    caller to append in one ledger transaction. Every leg is filled, or,
    for a limit the net price does not reach, every leg is ended unfilled.

    Raises ``BrokerOrderRejected``, naming the contract, when a leg cannot
    be priced. Nothing is then recorded of any leg.
    """
    intents = {leg.intent for leg in order.legs}
    if intents != {OptionPositionIntent.OPEN}:
        raise BrokerOrderRejected(
            "The simulation fills an opening order of several legs. An order of several legs that closes is not "
            "one it fills yet, and it invents no fill.",
            broker=broker_id,
        )
    for leg in order.legs:
        symbol = leg.contract.occ_symbol
        if past_last_session(leg.contract.expiry_date, at_ms):
            raise BrokerOrderRejected(
                f"{symbol} expired on {leg.contract.expiry_date.isoformat()} and its last session is over. "
                "Nothing trades in it, so no leg of this order was filled.",
                broker=broker_id,
            )
        quoted = bound.get(symbol)
        if quoted is None:
            raise BrokerOrderRejected(
                f"The simulation was handed no quote of {symbol} to fill this order's leg from, and it invents no "
                "fill. No leg of the order was filled.",
                broker=broker_id,
            )
        if quoted.multiplier is None:
            # Its position could be counted only as shares: a hundred times too small.
            raise BrokerOrderRejected(
                f"The simulation was not told how many shares one contract of {symbol} stands for, so it could "
                "not count the position this leg opens. No leg of the order was filled.",
                broker=broker_id,
            )
    # The fill model names a leg by its id; here that is the contract's own symbol.
    structure = ResolvedStructure(
        legs=tuple(
            ResolvedLeg(
                leg_id=leg.contract.occ_symbol,
                contract=leg.contract,
                position="long" if leg.side is OrderSide.BUY else "short",
                ratio=leg.ratio,
            )
            for leg in order.legs
        )
    )
    try:
        fills = simulated_entry_fills(
            structure, {symbol: quoted.quote.held for symbol, quoted in bound.items()}, units=order.quantity, at_ms=at_ms
        )
    except ValueError as exc:
        raise BrokerOrderRejected(
            f"The quotes handed over cannot fill this order: {exc}. No leg of the order was filled.", broker=broker_id
        ) from exc
    for fill in fills:
        if fill.price is None or fill.quote_age_ms is None or fill.price <= 0:
            # The books record no fill at a price of zero.
            raise BrokerOrderRejected(
                f"Nothing gives a price above zero to {fill.side} {fill.contract.occ_symbol} at. No leg of the "
                "order was filled.",
                broker=broker_id,
            )
    within_limit = order.net_limit_price is None or net_price_filled(order, fills) <= order.net_limit_price
    legs = []
    for number, (leg, fill) in enumerate(zip(order.legs, fills, strict=True), start=1):
        quoted = bound[leg.contract.occ_symbol]
        request = SingleOptionOrderRequest(
            contract=leg.contract, side=leg.side, intent=leg.intent, quantity=fill.contracts,
            # A leg has no limit of its own: the limit is the order's, a net price.
            order_type=OrderType.MARKET,
        )
        price = fill.price if within_limit else None
        legs.append((
            shape_leg_order(request, leg_order_ref(client_order_id, number), price, order.order_type.value),
            SynthesizedOptionFill(
                request=request,
                quote=quoted.quote,
                multiplier=quoted.multiplier,
                quote_ref=quoted.quote_ref,
                fill_model=fill.model if within_limit else None,
                fill_price=price,
                quote_age_ms=fill.quote_age_ms if within_limit else None,
                filled_at_ms=at_ms if within_limit else None,
            ),
        ))
    return tuple(legs)


def net_price_filled(order: MultiLegOptionOrderRequest, fills: Sequence[SimulatedLegFill]) -> Decimal:
    """What one structure was filled at, a share: paid above zero, received below, by the order's own sign rule."""
    terms = []
    for leg, fill in zip(order.legs, fills, strict=True):
        assert fill.price is not None
        terms.append((leg.ratio if leg.side is OrderSide.BUY else -leg.ratio, fill.price))
    return exact_sum(terms)


def multi_leg_order_view(
    legs: Sequence[SynthesizedOrderRecord], *, parent_client_order_id: str, parent_order_id: str, broker_id: str,
    observed_at_ms: int,
) -> BrokerOptionOrder:
    """The order of several legs whose legs' records are ``legs``, as the option port answers about it.

    The parent with each leg nested under it. It is filled when every leg
    is, and ended unfilled otherwise: a simulation never fills one leg.
    """
    requests = []
    for record in legs:
        assert record.option is not None
        requests.append(record.option.request)
    units = gcd(*(request.quantity for request in requests))
    every_leg_filled = all(record.option is not None and record.option.fill_price is not None for record in legs)
    first = legs[0].order
    net = None
    if every_leg_filled:
        net = exact_sum([
            (
                (request.quantity // units) * (1 if request.side is OrderSide.BUY else -1),
                record.option.fill_price,  # type: ignore[union-attr, list-item]
            )
            for request, record in zip(requests, legs, strict=True)
        ])
    return BrokerOptionOrder(
        broker=broker_id,
        order_id=parent_order_id,
        client_order_id=parent_client_order_id,
        multi_leg=True,
        contract=None,
        side=None,
        intent=None,
        order_type=first.order_type,
        time_in_force="day",
        quantity=units,
        filled_quantity=units if every_leg_filled else 0,
        # The order's limit is the books' to keep; this ledger holds each leg and no net limit.
        limit_price=None,
        filled_avg_price=net,
        status="filled" if every_leg_filled else "canceled",
        submitted_at_ms=first.submitted_at_ms,
        created_at_ms=first.created_at_ms,
        updated_at_ms=max(record.order.updated_at_ms or 0 for record in legs),
        filled_at_ms=first.filled_at_ms if every_leg_filled else None,
        canceled_at_ms=None if every_leg_filled else first.canceled_at_ms,
        expired_at_ms=None,
        legs=tuple(
            BrokerOptionOrderLeg(
                order_id=record.order.order_id,
                client_order_id=record.order.client_order_id,
                contract=request.contract,
                side=request.side,
                intent=request.intent,
                ratio=request.quantity // units,
                quantity=request.quantity,
                filled_quantity=request.quantity if record.option.fill_price is not None else 0,  # type: ignore[union-attr]
                filled_avg_price=record.option.fill_price,  # type: ignore[union-attr]
                status=record.order.status,
                submitted_at_ms=record.order.submitted_at_ms,
                created_at_ms=record.order.created_at_ms,
                updated_at_ms=record.order.updated_at_ms,
                filled_at_ms=record.order.filled_at_ms,
                canceled_at_ms=record.order.canceled_at_ms,
                expired_at_ms=record.order.expired_at_ms,
            )
            for request, record in zip(requests, legs, strict=True)
        ),
        observed_at_ms=observed_at_ms,
    )


__all__ = [
    "BoundLegQuote",
    "LegOrderShaper",
    "filled_multi_leg_order",
    "leg_client_order_ids",
    "legs_recorded",
    "multi_leg_order_view",
    "net_price_filled",
]
