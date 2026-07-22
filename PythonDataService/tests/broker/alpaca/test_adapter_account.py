"""Golden-fixture test: Alpaca account payload → BrokerAccountSnapshot.

Every contract field is asserted (this is where "100% payload mapping" is
proven). Runs against the representative `pending-real-capture` fixture;
regenerated against a real sanitized capture in HITL slice #1178.
"""

from __future__ import annotations

from app.broker.alpaca.adapter import from_alpaca_account, rfc3339_to_ms

_OBSERVED = 1_700_000_000_000


def test_from_alpaca_account_maps_every_field(load_alpaca_fixture) -> None:
    payload = load_alpaca_fixture("account", "account.json")

    snapshot = from_alpaca_account(payload, observed_at_ms=_OBSERVED)

    assert snapshot.broker == "alpaca"
    assert snapshot.account_id == "PA3ALPACAPAPER1"
    assert snapshot.account_status == "ACTIVE"
    assert snapshot.currency == "USD"
    assert snapshot.cash == 98765.43
    assert snapshot.equity == 100123.45
    assert snapshot.buying_power == 197530.86
    assert snapshot.portfolio_value == 100123.45
    assert snapshot.long_market_value == 1358.02
    assert snapshot.short_market_value == 0.0
    assert snapshot.pattern_day_trader is False
    assert snapshot.trading_blocked is False
    assert snapshot.account_blocked is False
    assert snapshot.created_at_ms == rfc3339_to_ms("2021-03-16T18:38:01.942282Z")
    assert snapshot.observed_at_ms == _OBSERVED


def test_observed_at_defaults_to_now(load_alpaca_fixture) -> None:
    payload = load_alpaca_fixture("account", "account.json")

    snapshot = from_alpaca_account(payload)

    assert snapshot.observed_at_ms > 1_600_000_000_000


def test_missing_created_at_is_none(load_alpaca_fixture) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload.pop("created_at")

    assert from_alpaca_account(payload, observed_at_ms=_OBSERVED).created_at_ms is None
