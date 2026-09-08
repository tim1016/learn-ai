"""Synthetic fill models for authorities that never submit (ADR 0059 D5.5).

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


def limit_touch_fill(
    leg: BrokerOrderLeg,
    *,
    decision_bar_end_ms: int,
    bars: Sequence[RetainedSourceBar],
    cancel_at_ms: int,
) -> SyntheticFill | None:
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
