"""Synthetic fill models for authorities that never submit (ADR 0059 D5.5).

Two models live here, and they are the whole set: ``immediate_fill_price``,
which is what the ``sim:`` world can actually do (it cannot rest an order, so a
leg either fills at the decision bar's close or is cancelled on the spot —
ruling R9), and ``limit_touch_fill``, the resting model the slice-4 shadow port
will call. They share a home because they answer the same question — would this
leg have filled, and at what price — and a second answer living inside
``synthetic_broker`` is how the two drift.

Formula (immediate_fill_price):
    A MARKET leg fills at the bar's close. A LIMIT leg fills at the bar's close
    iff that close is at or through its limit: ``close <= limit`` for a buy,
    ``close >= limit`` for a sell. Otherwise no fill.
Formula (limit_touch):
    Let B be the retained bars with ``start_ms >= decision_bar_end_ms`` and
    ``end_ms <= cancel_at_ms``, in ascending order. A buy fills on the first
    b in B with ``b.low <= limit``; a sell on the first with ``b.high >=
    limit``. The fill price is the limit; the fill instant is that bar's
    close. No such bar: no fill (the vendor would have cancelled).
Reference:
    ADR 0059 Decision 5.5 — eligibility starts with the first bar after the
    decision bar (its own range predates the order); ADR 0002's third
    invariant (fills are explicit about the model that produced them).
Canonical implementation: this file.
Validated against:
    tests/broker/alpaca/clerk/test_fill_models.py
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.broker.contract.models import BrokerOrderLeg, OrderSide, OrderType
from app.services.source_bar_ledger import RetainedSourceBar


@dataclass(frozen=True)
class SyntheticFill:
    filled_at_ms: int
    price: Decimal
    bar_ref: str


def immediate_fill_price(leg: BrokerOrderLeg, close: Decimal) -> Decimal | None:
    """The price this leg fills at against one bar's close, or ``None`` for no fill.

    The ``sim:`` world's whole fill model: it cannot rest an order, so a leg
    either transacts at the decision bar's close or is cancelled on the spot
    with zero fills (ruling R9). A MARKET leg always fills; a LIMIT leg fills
    only if the close is at or through its limit — the boundary is inclusive,
    since a close exactly at the limit is a marketable price.
    """
    if leg.order_type is OrderType.MARKET:
        return close
    if leg.limit_price is None:
        return None
    limit = Decimal(str(leg.limit_price))
    marketable = close <= limit if leg.side is OrderSide.BUY else close >= limit
    return close if marketable else None


def limit_touch_fill(
    leg: BrokerOrderLeg,
    *,
    decision_bar_end_ms: int,
    bars: Sequence[RetainedSourceBar],
    cancel_at_ms: int,
) -> SyntheticFill | None:
    """The first bar after the decision bar that reaches ``leg``'s limit, if any.

    ``cancel_at_ms`` is the instant the vendor would have cancelled, and the
    eligibility window is **half-open at its start, closed at its end**: a bar
    whose ``end_ms`` equals ``cancel_at_ms`` closed exactly as the order died
    and is still eligible; the next one is not. Eligibility begins at the first
    bar with ``start_ms >= decision_bar_end_ms`` — the decision bar's own range
    predates the order and can never fill it (ADR 0059 D5.5).
    """
    if leg.order_type is not OrderType.LIMIT or leg.limit_price is None:
        raise ValueError("limit_touch prices limit legs only")
    limit = Decimal(str(leg.limit_price))
    for bar in sorted(bars, key=lambda candidate: candidate.start_ms):
        if bar.start_ms < decision_bar_end_ms:
            continue
        if bar.end_ms > cancel_at_ms:
            return None
        touched = bar.low <= limit if leg.side is OrderSide.BUY else bar.high >= limit
        if touched:
            return SyntheticFill(filled_at_ms=bar.end_ms, price=limit, bar_ref=bar.bar_ref)
    return None
