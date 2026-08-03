"""The Alpaca capability descriptor must not advertise unbuildable order types.

Callers gate on ``capabilities()`` (design decision D2), so an advertised order
type that ``BrokerOrderLeg`` cannot construct is a false ``yes`` — a caller
asking "can I place a stop order?" would be told it can, then hit a
``ValidationError`` at construction time. These tests pin the advertised set to
the constructible ``OrderType`` so the two cannot silently drift apart.
"""

from __future__ import annotations

from app.broker.alpaca.broker import ALPACA_PAPER_CAPABILITIES
from app.broker.contract.models import BrokerOrderLeg, OrderType


def test_advertised_order_types_match_the_constructible_enum() -> None:
    buildable = {member.value for member in OrderType}

    assert set(ALPACA_PAPER_CAPABILITIES.supported_order_types) == buildable


def test_every_advertised_order_type_builds_a_valid_leg() -> None:
    for order_type in ALPACA_PAPER_CAPABILITIES.supported_order_types:
        kwargs: dict[str, object] = {
            "symbol": "SPY",
            "side": "buy",
            "quantity": 1,
            "order_type": order_type,
        }
        if order_type == "limit":
            kwargs["limit_price"] = 100.0

        # Must not raise: an advertised type that cannot construct a leg is a
        # dishonest capability descriptor.
        BrokerOrderLeg(**kwargs)
