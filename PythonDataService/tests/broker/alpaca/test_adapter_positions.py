"""Golden-fixture test: Alpaca position payloads → BrokerPosition.

Every contract field is asserted, including the short-position sign case.
Runs against the representative `pending-real-capture` fixture.
"""

from __future__ import annotations

from app.broker.alpaca.adapter import from_alpaca_position
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_from_alpaca_position_maps_long(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    long_position = load_alpaca_fixture("positions", "positions.json")[0]

    position = from_alpaca_position(long_position, observed_at_ms=_OBSERVED)

    assert position.broker == "alpaca"
    assert position.symbol == "AAPL"
    assert position.asset_id == "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415"
    assert position.asset_class == "us_equity"
    assert position.quantity == 10.0
    assert position.side == "long"
    assert position.average_entry_price == 135.80
    assert position.market_value == 1358.02
    assert position.cost_basis == 1358.00
    assert position.current_price == 135.802
    assert position.unrealized_pl == 0.02
    assert position.unrealized_plpc == 0.0000147
    assert position.observed_at_ms == _OBSERVED


def test_from_alpaca_position_maps_short_with_signed_quantity(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    short_position = load_alpaca_fixture("positions", "positions.json")[1]

    position = from_alpaca_position(short_position, observed_at_ms=_OBSERVED)

    assert position.symbol == "TSLA"
    assert position.quantity == -3.0
    assert position.side == "short"
    assert position.market_value == -735.00


def test_missing_optional_fields_become_none(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    payload = dict(load_alpaca_fixture("positions", "positions.json")[0])
    payload.pop("current_price")
    payload.pop("unrealized_plpc")
    payload["asset_id"] = None

    position = from_alpaca_position(payload, observed_at_ms=_OBSERVED)

    assert position.current_price is None
    assert position.unrealized_plpc is None
    assert position.asset_id is None
