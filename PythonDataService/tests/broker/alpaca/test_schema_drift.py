"""Schema-drift guard (spec §9).

Recursively diffs the key sets of the captured raw Alpaca payloads against the
alpaca-py model field names (and aliases). If Alpaca ships a field the SDK does
not know, this fails and **names** the offending keys. The capture journal
always keeps everything regardless; adapter tests separately prove the fields
the broker contract intentionally maps.

Parameterized over all six endpoint families; it auto-covers each as its
fixtures land. When a real capture (HITL slice #1178) surfaces an unknown key,
this is the test that fails first.
"""

from __future__ import annotations

from typing import Any

import pytest
from alpaca.trading.models import (
    Asset,
    Clock,
    NonTradeActivity,
    Order,
    Position,
    TradeAccount,
    TradeActivity,
    TradeUpdate,
)
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from pydantic import BaseModel

from app.broker.alpaca.adapter import to_alpaca_order_request
from app.broker.contract.models import BrokerOrderLeg
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

# family → (fixture filename, alpaca-py model(s) that define its known fields)
_FAMILIES: dict[str, tuple[str, tuple[type[BaseModel], ...]]] = {
    "account": ("account.json", (TradeAccount,)),
    "positions": ("positions.json", (Position,)),
    "orders": ("orders.json", (Order,)),
    "activities": ("activities.json", (TradeActivity, NonTradeActivity)),
    "assets": ("assets.json", (Asset,)),
    "clock": ("clock.json", (Clock,)),
}


def _known_names(model: type[BaseModel]) -> set[str]:
    """Field names plus every alias (Alpaca's `class` → `asset_class`, etc.)."""
    names: set[str] = set()
    for field_name, field in model.model_fields.items():
        names.add(field_name)
        for candidate in (field.alias, field.serialization_alias):
            if isinstance(candidate, str):
                names.add(candidate)
        validation_alias = field.validation_alias
        if isinstance(validation_alias, str):
            names.add(validation_alias)
        elif validation_alias is not None:
            names.update(
                choice
                for choice in getattr(validation_alias, "choices", [])
                if isinstance(choice, str)
            )
    return names


def _payload_keys(obj: Any) -> set[str]:
    """Every dict key in a payload, at any nesting depth."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys |= _payload_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _payload_keys(item)
    return keys


def unknown_keys(payload: Any, models: tuple[type[BaseModel], ...]) -> set[str]:
    """Keys present in the payload that no SDK model defines."""
    known: set[str] = set().union(*(_known_names(model) for model in models))
    return _payload_keys(payload) - known


@pytest.mark.parametrize("family", list(_FAMILIES))
def test_captured_payload_has_no_schema_drift(
    family: str,
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    filename, models = _FAMILIES[family]
    payload = load_alpaca_fixture(family, filename)

    drift = unknown_keys(payload, models)

    assert drift == set(), (
        f"{family}: Alpaca payload carries keys the SDK model(s) do not define: "
        f"{sorted(drift)}. Either the SDK is behind (upgrade alpaca-py and extend "
        f"the adapter) or the fixture is wrong."
    )


def test_schema_drift_is_detected_and_named() -> None:
    # A field the SDK has never seen must surface by name for compatibility review.
    payload = {"id": "abc", "cash": "1000", "brand_new_alpaca_field": 1}

    drift = unknown_keys(payload, (TradeAccount,))

    assert drift == {"brand_new_alpaca_field"}


def test_asset_class_alias_is_recognized() -> None:
    # Alpaca's raw `class` key is aliased to `asset_class` in the SDK — the
    # guard must treat it as known, not as drift.
    assert "class" in _known_names(Asset)


def test_order_submit_body_keys_are_all_known_to_the_sdk() -> None:
    # Outbound drift guard: every key we POST to /v2/orders must be a field the
    # SDK's order-request model defines. If a future alpaca-py renames one (or
    # we typo a key), this fails and names it — the write-path twin of the
    # inbound capture-drift guard above.
    body = to_alpaca_order_request(
        BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        client_order_id="manual/inkant/v1:abc123",
    )

    unknown = set(body) - _known_names(MarketOrderRequest)

    assert unknown == set(), (
        f"order-submit body carries keys the SDK order model does not define: "
        f"{sorted(unknown)}. Either alpaca-py renamed a field or the adapter "
        f"emits a wrong key."
    )
    # The market contract keys are exactly these — pin them so a silent drop surfaces.
    assert set(body) == {
        "symbol",
        "qty",
        "side",
        "type",
        "time_in_force",
        "client_order_id",
    }


def test_limit_order_submit_body_keys_are_all_known_to_the_sdk() -> None:
    # The limit-order twin: the added ``limit_price`` key must be a field the
    # SDK's limit-order-request model defines, and the body pins the full key set.
    body = to_alpaca_order_request(
        BrokerOrderLeg(
            symbol="SPY",
            side="sell",
            quantity=2,
            order_type="limit",
            limit_price=240.5,
            time_in_force="gtc",
        ),
        client_order_id="manual/inkant/v1:abc123",
    )

    unknown = set(body) - _known_names(LimitOrderRequest)

    assert unknown == set(), (
        f"limit order-submit body carries keys the SDK order model does not "
        f"define: {sorted(unknown)}. Either alpaca-py renamed a field or the "
        f"adapter emits a wrong key."
    )
    assert set(body) == {
        "symbol",
        "qty",
        "side",
        "type",
        "time_in_force",
        "limit_price",
        "client_order_id",
    }


def test_trade_updates_frame_has_no_schema_drift(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    # S4 inbound drift guard: every key in a trade_updates event's ``data``
    # payload (the envelope wrapper keys AND the nested order object) must be a
    # field the SDK's TradeUpdate + Order models define. If Alpaca adds a
    # lifecycle-event key alpaca-py doesn't know, this fails and names it.
    frames = load_alpaca_fixture("trade_updates", "trade_updates.json")
    models = (TradeUpdate, Order)
    for frame in frames:
        # The ``stream`` envelope key is the websocket framing, not an SDK model
        # field — the SDK parses ``msg["data"]`` into a TradeUpdate — so drift is
        # checked against the ``data`` payload only.
        assert set(frame) == {"stream", "data"}
        drift = unknown_keys(frame["data"], models)
        assert drift == set(), (
            "trade_updates: Alpaca event carries keys the SDK model(s) do not "
            f"define: {sorted(drift)}. Either alpaca-py is behind (upgrade + "
            f"extend the adapter) or the fixture is wrong."
        )
