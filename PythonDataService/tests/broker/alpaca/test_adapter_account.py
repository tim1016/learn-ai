"""Golden-fixture test: Alpaca account payload → BrokerAccountSnapshot.

Every contract field is asserted (this is where "100% payload mapping" is
proven). Runs against the real sanitized paper-account capture (HITL #1178).
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.adapter import from_alpaca_account, rfc3339_to_ms
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.config import AlpacaSettings
from app.broker.contract.errors import (
    BrokerAccountModeDisagreement,
    BrokerEvidenceUnavailable,
)
from tests.broker.alpaca.conftest import AlpacaFixtureLoader

_OBSERVED = 1_700_000_000_000


class _AccountClient:
    """The client seam: returns the raw account payload the test built."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def get_account(self) -> dict[str, object]:
        return self.payload


# Every live envelope value is required configuration (ADR 0059 D4).
_LIVE_ENVELOPE = {
    "live_loss_fraction": 0.02,
    "live_loss_usd": 500.0,
    "live_xh_entry_bps": 10.0,
    "live_xh_exit_bps": 10.0,
}


def _broker(payload: dict[str, object], mode: str = "paper") -> AlpacaBroker:
    envelope = _LIVE_ENVELOPE if mode == "live" else {}
    return AlpacaBroker(
        client=_AccountClient(payload),  # type: ignore[arg-type]
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode=mode, **envelope),
    )


def test_from_alpaca_account_maps_every_field(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("account", "account.json")

    snapshot = from_alpaca_account(payload, account_mode="paper", observed_at_ms=_OBSERVED)

    assert snapshot.broker == "alpaca"
    assert snapshot.account_id == "PA0SANITIZED00001"
    assert snapshot.account_mode == "paper"
    assert snapshot.account_status == "ACTIVE"
    assert snapshot.currency == "USD"
    assert snapshot.cash == 100000.0
    assert snapshot.equity == 100000.0
    assert snapshot.buying_power == 400000.0
    assert snapshot.portfolio_value == 100000.0
    assert snapshot.long_market_value == 0.0
    assert snapshot.short_market_value == 0.0
    # Margin fields are ingested to prove the cash bound, never to use it
    # (ADR 0059 D7). Values are the sanitized fixture's own.
    assert snapshot.multiplier == 4.0
    assert snapshot.regt_buying_power == 200000.0
    assert snapshot.maintenance_margin == 0.0
    assert snapshot.initial_margin == 0.0
    assert snapshot.sma == 100000.0
    assert snapshot.last_equity == 100000.0
    assert snapshot.trading_blocked is False
    assert snapshot.account_blocked is False
    assert snapshot.created_at_ms == rfc3339_to_ms("2026-07-22T00:40:26.776619Z")
    assert snapshot.observed_at_ms == _OBSERVED


def test_observed_at_defaults_to_now(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    payload = load_alpaca_fixture("account", "account.json")

    snapshot = from_alpaca_account(payload, account_mode="paper")

    assert snapshot.observed_at_ms > 1_600_000_000_000


def test_missing_created_at_is_none(load_alpaca_fixture: AlpacaFixtureLoader) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload.pop("created_at")

    assert (
        from_alpaca_account(payload, account_mode="paper", observed_at_ms=_OBSERVED).created_at_ms
        is None
    )


@pytest.mark.parametrize(
    ("field", "value", "cause_type", "cause"),
    [
        pytest.param("cash", True, TypeError, "not a boolean", id="boolean-cash"),
        pytest.param("equity", False, TypeError, "not a boolean", id="boolean-equity"),
        pytest.param("last_equity", True, TypeError, "not a boolean", id="boolean-last-equity"),
        pytest.param("cash", "not-a-number", ValueError, "'not-a-number'", id="unparseable-cash"),
        pytest.param("equity", None, TypeError, "NoneType", id="null-equity"),
    ],
)
async def test_broker_names_a_malformed_account_response_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    field: str,
    value: object,
    cause_type: type[Exception],
    cause: str,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload[field] = value

    with pytest.raises(BrokerEvidenceUnavailable, match="account data this app could not read") as info:
        await _broker(payload).get_account()

    assert info.value.http_status == 503
    # The plain message and ``detail`` stay owner copy: a router returns both.
    # The technical reason rides on the exception chain for the operator's log.
    assert info.value.detail is not None
    assert type(info.value.__cause__).__name__ not in info.value.detail
    assert isinstance(info.value.__cause__, cause_type)
    assert cause in str(info.value.__cause__)


async def test_broker_names_a_missing_account_field_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload.pop("equity")

    with pytest.raises(BrokerEvidenceUnavailable, match="account data this app could not read") as info:
        await _broker(payload).get_account()

    assert info.value.detail is not None
    assert "KeyError" not in info.value.detail
    assert isinstance(info.value.__cause__, KeyError)
    assert info.value.__cause__.args == ("equity",)


_UNUSABLE_ACCOUNT_NUMBERS = [
    pytest.param(None, id="null"),
    pytest.param("", id="blank"),
    pytest.param("   ", id="whitespace"),
    pytest.param(12345, id="number"),
    pytest.param(True, id="boolean"),
]


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("account_number", _UNUSABLE_ACCOUNT_NUMBERS)
def test_adapter_refuses_an_account_number_that_is_not_text(
    load_alpaca_fixture: AlpacaFixtureLoader,
    mode: str,
    account_number: object,
) -> None:
    """A null number once became live account "None" (#2627); no mode maps it."""
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["account_number"] = account_number

    with pytest.raises((TypeError, ValueError), match="account_number"):
        from_alpaca_account(payload, account_mode=mode, observed_at_ms=_OBSERVED)


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("account_number", _UNUSABLE_ACCOUNT_NUMBERS)
async def test_broker_names_an_unusable_account_number_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    mode: str,
    account_number: object,
) -> None:
    """Malformed evidence in every mode, never a snapshot or a mode disagreement (#2627)."""
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["account_number"] = account_number

    with pytest.raises(BrokerEvidenceUnavailable, match="account data this app could not read") as info:
        await _broker(payload, mode).get_account()

    assert info.value.http_status == 503
    assert "account_number" in str(info.value.__cause__)


@pytest.mark.parametrize("status", [None, "", "   "], ids=["null", "blank", "whitespace"])
async def test_broker_names_an_account_with_no_status_as_unavailable_evidence(
    load_alpaca_fixture: AlpacaFixtureLoader,
    status: object,
) -> None:
    """A null status once became account status "None" (#2643)."""
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["status"] = status

    with pytest.raises(BrokerEvidenceUnavailable, match="account data this app could not read") as info:
        await _broker(payload).get_account()

    assert isinstance(info.value.__cause__, ValueError)
    assert "'status'" in str(info.value.__cause__)


def test_live_mode_maps_live_and_a_non_pa_account_number(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["account_number"] = "9LIVE0001"

    snapshot = from_alpaca_account(payload, account_mode="live", observed_at_ms=_OBSERVED)

    assert snapshot.account_mode == "live"
    assert snapshot.account_id == "9LIVE0001"


def test_live_mode_refuses_a_paper_shaped_account_number(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = load_alpaca_fixture("account", "account.json")  # PA0SANITIZED00001

    with pytest.raises(BrokerAccountModeDisagreement) as info:
        from_alpaca_account(payload, account_mode="live", observed_at_ms=_OBSERVED)

    assert info.value.reason_code == "LIVE_MODE_DISAGREEMENT"
    assert info.value.http_status == 409
    assert "PA" in (info.value.detail or "")


def test_paper_mode_refuses_a_live_shaped_account_number(
    load_alpaca_fixture: AlpacaFixtureLoader,
) -> None:
    payload = dict(load_alpaca_fixture("account", "account.json"))
    payload["account_number"] = "9LIVE0001"

    with pytest.raises(BrokerAccountModeDisagreement):
        from_alpaca_account(payload, account_mode="paper", observed_at_ms=_OBSERVED)
