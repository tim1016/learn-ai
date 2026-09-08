"""Manual tickets stay regular-session, and their instruction hash survives the new leg field."""

from __future__ import annotations

import hashlib

from app.broker.alpaca.clerk.sqlite.facts import leg_instruction_payload
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.contract.models import BrokerOrderLeg


def test_instruction_payload_omits_the_flag_for_a_regular_leg() -> None:
    leg = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1)

    payload = leg_instruction_payload(leg)

    assert "extended_hours" not in payload
    # Byte-identical to the pre-slice-3 hash input for the same leg.
    legacy = {k: v for k, v in leg.model_dump(mode="json").items() if k != "extended_hours"}
    assert hashlib.sha256(canonicalize(payload).encode("utf-8")).hexdigest() == (
        hashlib.sha256(canonicalize(legacy).encode("utf-8")).hexdigest()
    )


def test_instruction_payload_keeps_the_flag_for_an_extended_leg() -> None:
    leg = BrokerOrderLeg(
        symbol="SPY", side="buy", quantity=1, order_type="limit", limit_price=100.25, extended_hours=True
    )

    assert leg_instruction_payload(leg)["extended_hours"] is True
