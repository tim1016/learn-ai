"""Adapter golden-fixture tests for ``trade_updates`` event mapping (S4).

Loads the representative ``trade_updates`` frames and asserts
``from_alpaca_trade_update`` maps each event kind to a ``BrokerOrderEvent`` with
the right event type, ``int64`` ms UTC instant, and (with explicit tolerances)
the right numeric price/quantity — the per-execution fill on a fill/partial_fill,
the order's cumulative figures otherwise.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.broker.alpaca import adapter
from app.broker.alpaca.adapter import from_alpaca_trade_update, rfc3339_to_ms
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

# Broker figures are float display values (contract §money), not ported math, so
# a tight-but-explicit tolerance pins mapping fidelity without Decimal rigor.
_ATOL = 1e-9
_RTOL = 0.0


@pytest.fixture
def frames(load_alpaca_fixture: AlpacaFixtureLoader) -> list[dict[str, Any]]:
    return load_alpaca_fixture("trade_updates", "trade_updates.json")


def _data_for(frames: list[dict[str, Any]], event: str) -> dict[str, Any]:
    return next(frame["data"] for frame in frames if frame["data"]["event"] == event)


def test_new_event_maps_with_no_fill_price(frames: list[dict[str, Any]]) -> None:
    event = from_alpaca_trade_update(_data_for(frames, "new"))

    assert event.event_type == "new"
    assert event.occurred_at_ms == rfc3339_to_ms("2021-03-16T18:38:01.942282Z")
    # A ``new`` event has no per-execution price and the order has not filled.
    assert event.price is None
    assert event.quantity is None


def test_partial_fill_maps_the_execution_slice(frames: list[dict[str, Any]]) -> None:
    event = from_alpaca_trade_update(_data_for(frames, "partial_fill"))

    assert event.event_type == "partial_fill"
    assert event.occurred_at_ms == rfc3339_to_ms("2021-03-16T18:38:02.001000Z")
    # The per-execution price/qty (135.75 × 4), NOT the order's cumulative avg.
    assert event.price is not None and math.isclose(event.price, 135.75, abs_tol=_ATOL, rel_tol=_RTOL)
    assert event.quantity is not None and math.isclose(event.quantity, 4.0, abs_tol=_ATOL, rel_tol=_RTOL)


def test_fill_maps_the_final_execution_slice(frames: list[dict[str, Any]]) -> None:
    event = from_alpaca_trade_update(_data_for(frames, "fill"))

    assert event.event_type == "fill"
    assert event.occurred_at_ms == rfc3339_to_ms("2021-03-16T18:38:02.123456Z")
    # The final execution slice (135.83 × 6), distinct from the 135.80 avg.
    assert event.price is not None and math.isclose(event.price, 135.83, abs_tol=_ATOL, rel_tol=_RTOL)
    assert event.quantity is not None and math.isclose(event.quantity, 6.0, abs_tol=_ATOL, rel_tol=_RTOL)


def test_canceled_maps_with_cumulative_fallback(frames: list[dict[str, Any]]) -> None:
    event = from_alpaca_trade_update(_data_for(frames, "canceled"))

    assert event.event_type == "canceled"
    assert event.occurred_at_ms == rfc3339_to_ms("2021-03-17T14:05:00.000000Z")
    # No per-execution figures; the order never filled, so both fall back to None.
    assert event.price is None
    assert event.quantity is None


def test_rejected_maps(frames: list[dict[str, Any]]) -> None:
    event = from_alpaca_trade_update(_data_for(frames, "rejected"))

    assert event.event_type == "rejected"
    assert event.occurred_at_ms == rfc3339_to_ms("2021-03-17T15:00:00.000000Z")
    assert event.price is None
    assert event.quantity is None


def test_all_fixture_frames_map(frames: list[dict[str, Any]]) -> None:
    # Every representative frame maps without raising — a coverage sweep.
    mapped = [from_alpaca_trade_update(frame["data"]) for frame in frames]
    assert [e.event_type for e in mapped] == [
        "new",
        "partial_fill",
        "fill",
        "canceled",
        "rejected",
    ]
    # Every instant is a positive int64 ms.
    assert all(isinstance(e.occurred_at_ms, int) and e.occurred_at_ms > 0 for e in mapped)


def test_unrecognized_event_is_surfaced_by_name() -> None:
    # A vendor event the adapter does not know must raise by name — a new
    # lifecycle event is a schema signal, never silently coerced.
    with pytest.raises(ValueError, match="brand_new_event"):
        from_alpaca_trade_update(
            {"event": "brand_new_event", "timestamp": "2021-03-16T18:38:02Z", "order": {}}
        )


def test_missing_timestamp_fails_fast() -> None:
    with pytest.raises(ValueError, match="missing its timestamp"):
        from_alpaca_trade_update({"event": "fill", "order": {"status": "filled"}})


def test_event_names_match_documented_set() -> None:
    # Pin the documented event vocabulary so a silent drop of a known event surfaces.
    assert {"new", "fill", "partial_fill", "canceled", "expired", "rejected", "replaced"}.issubset(
        adapter.ALPACA_TRADE_UPDATE_EVENTS
    )
