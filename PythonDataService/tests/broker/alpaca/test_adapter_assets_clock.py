"""Golden-fixture tests: Alpaca asset + clock payloads → contract models.

The clock is surfaced strictly as vendor evidence — nothing in session or
calendar logic reads it as authority (documented in the broker-contract-v2 ADR;
asserted here at the model level).
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.adapter import (
    from_alpaca_asset,
    from_alpaca_clock,
    rfc3339_to_ms,
)
from app.broker.contract.models import BrokerClockEvidence
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_from_alpaca_asset_maps_every_field_active(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    active = load_alpaca_fixture("assets", "assets.json")[0]

    asset = from_alpaca_asset(active)

    assert asset.broker == "alpaca"
    assert asset.asset_id == "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"
    assert asset.symbol == "AAPL"
    assert asset.name == "Apple Inc. Common Stock"
    # Alpaca's raw "class" key maps to asset_class.
    assert asset.asset_class == "us_equity"
    assert asset.exchange == "NASDAQ"
    assert asset.status == "active"
    assert asset.tradable is True
    assert asset.fractionable is True
    assert asset.shortable is True
    assert asset.marginable is True


def test_from_alpaca_asset_inactive(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    inactive = load_alpaca_fixture("assets", "assets.json")[1]

    asset = from_alpaca_asset(inactive)

    assert asset.symbol == "OLDCO"
    assert asset.status == "inactive"
    assert asset.tradable is False
    assert asset.shortable is False


def test_from_alpaca_asset_accepts_the_sdk_alias_key() -> None:
    # Robust to the SDK-serialized form (`asset_class`) as well as the raw `class`.
    asset = from_alpaca_asset(
        {"id": "a", "symbol": "AAPL", "asset_class": "us_equity", "status": "active"}
    )

    assert asset.asset_class == "us_equity"


def test_from_alpaca_asset_missing_class_fails_loud() -> None:
    # No sentinel default — a missing class raises, so a schema change surfaces.
    with pytest.raises(KeyError):
        from_alpaca_asset({"id": "a", "symbol": "AAPL", "status": "active"})


def test_from_alpaca_clock_is_vendor_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("clock", "clock.json")

    clock = from_alpaca_clock(payload, observed_at_ms=_OBSERVED)

    assert isinstance(clock, BrokerClockEvidence)
    assert clock.broker == "alpaca"
    assert clock.is_open is True
    assert clock.vendor_timestamp_ms == rfc3339_to_ms("2022-04-28T10:00:00.123456-04:00")
    assert clock.next_open_ms == rfc3339_to_ms("2022-04-29T09:30:00-04:00")
    assert clock.next_close_ms == rfc3339_to_ms("2022-04-28T16:00:00-04:00")
    assert clock.observed_at_ms == _OBSERVED
