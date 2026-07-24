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

# Fields Alpaca has added since alpaca-py 0.43.5 was released, reviewed as of
# 2026-07-24 and intentionally NOT mapped to contract fields because they have
# no operational meaning to our layer (metadata, administrative, or redundant).
_INTENTIONALLY_UNMAPPED: dict[str, frozenset[str]] = {
    "account": frozenset({
        "admin_configurations",       # internal admin config blob
        "balance_asof",               # date string (redundant; equity is live)
        "crypto_tier",                # crypto account tier (phase 1: no crypto)
        "effective_buying_power",     # regt_buying_power alias
        "intraday_adjustments",       # intraday P&L adjustment string
        "pending_reg_taf_fees",       # accrued regulatory/TAF fee estimate
        "position_market_value",      # duplicate of long_market_value for longs
        "user_configurations",        # user-level config blob
    }),
    "orders": frozenset({
        "source",     # internal Alpaca order-source code (not operator-visible)
        "subtag",     # internal sub-tag (not operator-visible)
    }),
    "activities": frozenset({
        "amount",      # gross trade amount = price × qty (redundant; contract maps price + quantity separately)
        "created_at",  # activity creation time (distinct from transaction_time)
        "currency",    # currency code (always "USD" for equity paper accounts)
    }),
    "assets": frozenset({
        "borrow_status",             # "easy_to_borrow" / "hard_to_borrow" / "not_available"
        "margin_requirement_long",   # margin req % as a string (already in maintenance_margin_requirement)
        "margin_requirement_short",  # margin req % for shorts
    }),
}

# trade_updates websocket fields Alpaca added since alpaca-py 0.43.5, intentionally
# not mapped: they are internal event-routing metadata or settlement bookkeeping.
_TRADE_UPDATES_INTENTIONALLY_UNMAPPED: frozenset[str] = frozenset({
    "at",                   # server-side event receipt time (distinct from order timestamp)
    "cancel_requested_at",  # time the cancel was requested (not in SDK Order model; tracked in order object)
    "event_id",             # internal event correlation ID (not exposed in contract)
    "settle_date",          # trade settlement date string (T+1/T+2; not part of event contract)
})


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

    drift = unknown_keys(payload, models) - _INTENTIONALLY_UNMAPPED.get(family, frozenset())

    assert drift == set(), (
        f"{family}: Alpaca payload carries UNKNOWN (not intentionally unmapped) keys: "
        f"{sorted(drift)}. Either upgrade alpaca-py and extend the adapter, or add to "
        f"_INTENTIONALLY_UNMAPPED with a comment explaining why."
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
    #
    # Only frames with stream == "trade_updates" carry event data the adapter
    # processes. Authorization and listening framing frames use a different schema
    # (no "event" key; carry "action" / "streams" instead) and are not checked.
    frames = load_alpaca_fixture("trade_updates", "trade_updates.json")
    models = (TradeUpdate, Order)
    for frame in frames:
        assert set(frame) == {"stream", "data"}
        if frame["stream"] != "trade_updates":
            continue
        drift = unknown_keys(frame["data"], models) - _TRADE_UPDATES_INTENTIONALLY_UNMAPPED
        assert drift == set(), (
            "trade_updates: Alpaca event carries UNKNOWN (not intentionally unmapped) "
            f"keys: {sorted(drift)}. Either alpaca-py is behind (upgrade + extend the "
            f"adapter) or the fixture is wrong."
        )
