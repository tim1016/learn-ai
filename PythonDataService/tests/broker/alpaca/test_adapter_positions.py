"""Golden-fixture test: Alpaca position payloads → BrokerPosition.

Every contract field is asserted, including the short-position sign case.

Fixture layout (positions.json):
  [0] — real SPY long position (1 share, HITL #1178)
  [1] — synthetic TSLA short position (-3 shares)
"""

from __future__ import annotations

from app.broker.alpaca.adapter import from_alpaca_position
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_from_alpaca_position_maps_long(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    long_position = load_alpaca_fixture("positions", "positions.json")[0]

    position = from_alpaca_position(long_position, observed_at_ms=_OBSERVED)

    assert position.broker == "alpaca"
    assert position.symbol == "SPY"
    assert position.asset_id == "00000000-0000-0000-0000-000000000001"
    assert position.asset_class == "us_equity"
    assert position.quantity == 1.0
    assert position.side == "long"
    assert position.average_entry_price == 737.91
    # These are fixture values; assert exact conversion from the captured payload
    # so a mapping regression cannot hide behind a type/sign-only assertion.
    assert position.market_value == float(long_position["market_value"])
    assert position.cost_basis == 737.91
    assert position.current_price == float(long_position["current_price"])
    assert position.unrealized_pl == float(long_position["unrealized_pl"])
    assert position.unrealized_plpc == float(long_position["unrealized_plpc"])
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
