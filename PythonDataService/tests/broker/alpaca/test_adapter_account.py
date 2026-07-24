"""Golden-fixture test: Alpaca account payload → BrokerAccountSnapshot.

Every contract field is asserted (this is where "100% payload mapping" is
proven). Runs against the real sanitized paper-account capture (HITL #1178).
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.adapter import from_alpaca_account, rfc3339_to_ms
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


def test_from_alpaca_account_maps_every_field(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("account", "account.json")

    snapshot = from_alpaca_account(payload, observed_at_ms=_OBSERVED)

    assert snapshot.broker == "alpaca"
    assert snapshot.account_id == "PA0SANITIZED00001"
    assert snapshot.account_status == "ACTIVE"
    assert snapshot.currency == "USD"
    assert snapshot.cash == 100000.0
    assert snapshot.equity == 100000.0
    assert snapshot.buying_power == 400000.0
    assert snapshot.portfolio_value == 100000.0
    assert snapshot.long_market_value == 0.0
    assert snapshot.short_market_value == 0.0
    # pattern_day_trader is absent from the real fixture; adapter returns None.
    assert snapshot.pattern_day_trader is None
    assert snapshot.trading_blocked is False
    assert snapshot.account_blocked is False
    assert snapshot.created_at_ms == rfc3339_to_ms("2026-07-22T00:40:26.776619Z")
    assert snapshot.observed_at_ms == _OBSERVED


def test_observed_at_defaults_to_now(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    payload = load_alpaca_fixture("account", "account.json")

    snapshot = from_alpaca_account(payload)

    assert snapshot.observed_at_ms > 1_600_000_000_000


def test_missing_created_at_is_none(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload.pop("created_at")

    assert from_alpaca_account(payload, observed_at_ms=_OBSERVED).created_at_ms is None


def test_missing_pattern_day_trader_preserves_unknown(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    # The real fixture already lacks pattern_day_trader; adapter must return None.
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload.pop("pattern_day_trader", None)

    snapshot = from_alpaca_account(payload, observed_at_ms=_OBSERVED)

    assert snapshot.pattern_day_trader is None


def test_malformed_pattern_day_trader_is_rejected(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["pattern_day_trader"] = "false"

    with pytest.raises(TypeError, match="boolean or null"):
        from_alpaca_account(payload, observed_at_ms=_OBSERVED)
