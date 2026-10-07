"""The account's option fields: buying power and the two approval levels.

Read against the real sanitized paper-account capture, which carries all
three, and against Alpaca's documented example answer, which leaves one out.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

import pytest

from app.broker.alpaca.adapter import from_alpaca_option_account
from app.broker.alpaca.broker import AlpacaBroker
from app.broker.alpaca.config import AlpacaSettings
from app.broker.contract.errors import BrokerAccountModeDisagreement, BrokerEvidenceUnavailable
from tests.broker.alpaca.conftest import AlpacaFixtureLoader
from tests.broker.alpaca.documented_examples import documented

_OBSERVED = 1_700_000_000_000


class _AccountClient:
    """The client seam: returns the raw account payload the test built."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def get_account(self) -> dict[str, Any]:
        return self.payload


def _broker(payload: dict[str, Any]) -> AlpacaBroker:
    return AlpacaBroker(
        client=_AccountClient(payload),  # type: ignore[arg-type]
        settings=AlpacaSettings(api_key_id="k", api_secret_key="s", mode="paper"),
    )


@pytest.fixture
def captured(load_alpaca_fixture: AlpacaFixtureLoader) -> dict[str, Any]:
    return copy.deepcopy(load_alpaca_fixture("account", "account.json"))


def test_the_captured_paper_account_reads_its_buying_power_and_both_levels(captured: dict[str, Any]) -> None:
    options = from_alpaca_option_account(captured, account_mode="paper", observed_at_ms=_OBSERVED)

    assert options.broker == "alpaca"
    assert options.account_id == "PA0SANITIZED00001"
    assert options.account_mode == "paper"
    assert options.options_buying_power == Decimal("100000")
    assert options.options_approved_level == 3
    assert options.options_trading_level == 3
    assert options.observed_at_ms == _OBSERVED


def test_a_level_alpaca_leaves_out_stays_unknown() -> None:
    """Alpaca's own example answer has a trading level and buying power and no approved level."""
    example = documented("account", "documented_account.json")

    options = from_alpaca_option_account(example, account_mode="paper", observed_at_ms=_OBSERVED)

    assert options.options_buying_power == Decimal("122716.305")
    assert options.options_trading_level == 2
    assert options.options_approved_level is None


def test_an_account_that_states_nothing_about_options_reads_as_unknown_never_zero(captured: dict[str, Any]) -> None:
    for field in ("options_buying_power", "options_approved_level", "options_trading_level"):
        del captured[field]

    options = from_alpaca_option_account(captured, account_mode="paper", observed_at_ms=_OBSERVED)

    assert options.options_buying_power is None
    assert options.options_approved_level is None
    assert options.options_trading_level is None


def test_a_level_of_zero_is_options_turned_off_and_is_kept_as_zero(captured: dict[str, Any]) -> None:
    captured.update(options_approved_level=2, options_trading_level=0)

    options = from_alpaca_option_account(captured, account_mode="paper", observed_at_ms=_OBSERVED)

    assert (options.options_approved_level, options.options_trading_level) == (2, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"options_trading_level": True},
        {"options_trading_level": "3"},
        {"options_trading_level": 2.0},
        {"options_approved_level": 4},
        {"options_approved_level": -1},
        {"options_buying_power": "plenty"},
        {"options_buying_power": True},
        {"options_buying_power": "NaN"},
    ],
)
def test_an_option_field_that_is_not_what_alpaca_documents_is_refused(
    captured: dict[str, Any], change: dict[str, Any]
) -> None:
    """A level above the three Alpaca lists is not passed on as approval for everything."""
    captured.update(change)

    with pytest.raises((TypeError, ValueError)):
        from_alpaca_option_account(captured, account_mode="paper", observed_at_ms=_OBSERVED)


def test_a_paper_account_under_live_mode_is_refused_for_its_option_fields_too(captured: dict[str, Any]) -> None:
    with pytest.raises(BrokerAccountModeDisagreement):
        from_alpaca_option_account(captured, account_mode="live", observed_at_ms=_OBSERVED)


async def test_the_broker_reads_the_option_fields_without_changing_the_served_account(captured: dict[str, Any]) -> None:
    broker = _broker(captured)

    options = await broker.get_option_account()
    served = await broker.get_account()

    assert (options.options_buying_power, options.options_approved_level, options.options_trading_level) == (
        Decimal("100000"),
        3,
        3,
    )
    assert options.account_id == served.account_id
    # The model a route serves gained nothing, so the served contract is what it was.
    assert not any(name.startswith("options_") for name in served.model_dump())


async def test_the_broker_names_unreadable_option_fields_as_unavailable_evidence(captured: dict[str, Any]) -> None:
    captured["options_trading_level"] = "three"

    with pytest.raises(BrokerEvidenceUnavailable, match="account"):
        await _broker(captured).get_option_account()
