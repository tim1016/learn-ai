"""Tests for the broker-neutral contract models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerPosition,
)


def _account(**overrides: object) -> BrokerAccountSnapshot:
    base = dict(
        broker="alpaca",
        account_id="PA123",
        account_status="ACTIVE",
        currency="USD",
        cash=1000.0,
        equity=1500.0,
        buying_power=3000.0,
        portfolio_value=1500.0,
        long_market_value=500.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=1_600_000_000_000,
        observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerAccountSnapshot(**base)  # type: ignore[arg-type]


def test_account_snapshot_round_trips_snake_case() -> None:
    dumped = _account().model_dump()

    assert dumped["account_id"] == "PA123"
    assert dumped["buying_power"] == 3000.0
    assert dumped["observed_at_ms"] == 1_700_000_000_000


def test_contract_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _account(unexpected="boom")


def test_created_at_ms_is_nullable() -> None:
    assert _account(created_at_ms=None).created_at_ms is None


def test_position_carries_signed_quantity_and_unrealized_pl() -> None:
    position = BrokerPosition(
        broker="alpaca",
        symbol="AAPL",
        asset_id="asset-1",
        asset_class="us_equity",
        quantity=-5.0,
        side="short",
        average_entry_price=190.0,
        market_value=-940.0,
        cost_basis=-950.0,
        current_price=188.0,
        unrealized_pl=10.0,
        unrealized_plpc=0.0105,
        observed_at_ms=1_700_000_000_000,
    )

    assert position.quantity == -5.0
    assert position.side == "short"


def test_order_defaults_to_no_events() -> None:
    order = BrokerOrder(
        broker="alpaca",
        order_id="o-1",
        client_order_id=None,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=1.0,
        filled_quantity=0.0,
        limit_price=None,
        stop_price=None,
        filled_avg_price=None,
        status="new",
        submitted_at_ms=1_700_000_000_000,
        created_at_ms=1_700_000_000_000,
        updated_at_ms=1_700_000_000_000,
        filled_at_ms=None,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=1_700_000_000_000,
    )

    assert order.events == []


def test_order_event_is_a_lifecycle_row() -> None:
    event = BrokerOrderEvent(
        event_type="fill",
        occurred_at_ms=1_700_000_000_500,
        price=421.5,
        quantity=1.0,
    )

    assert event.event_type == "fill"
    assert event.occurred_at_ms == 1_700_000_000_500


def test_clock_evidence_is_vendor_shaped() -> None:
    clock = BrokerClockEvidence(
        broker="alpaca",
        is_open=True,
        vendor_timestamp_ms=1_700_000_000_000,
        next_open_ms=1_700_050_000_000,
        next_close_ms=1_700_020_000_000,
        observed_at_ms=1_700_000_000_000,
    )

    assert clock.is_open is True
    assert clock.next_open_ms == 1_700_050_000_000


def test_capabilities_are_frozen_data() -> None:
    caps = BrokerCapabilities(
        broker="alpaca",
        paper_only=True,
        supports_fractional=True,
        supports_extended_hours=True,
        supported_order_types=("market", "limit"),
        data_feed="iex",
        bars_may_gap=True,
        max_stream_symbols=30,
        max_concurrent_streams=1,
        rest_rate_limit_per_min=200,
    )

    assert caps.bars_may_gap is True
    assert caps.max_stream_symbols == 30
    with pytest.raises(ValidationError):
        caps.bars_may_gap = False  # frozen
