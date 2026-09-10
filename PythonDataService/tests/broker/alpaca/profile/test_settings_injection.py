"""Mode and settings are injected into broker and client construction.

Before this package ``AlpacaBroker`` reached for the process-wide settings
singleton for its capability descriptor and for the account mode it hands the
adapter, while ``AlpacaTradingClient`` already accepted an injected object. The
inconsistency meant a broker built for one profile's context would still have
read another configuration's mode. These tests pin the injected path, the
unchanged lazy fallback that lets a credential-free service boot, and the
end-to-end refusal that mode injection has to keep working.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.broker.alpaca.broker import (
    ALPACA_LIVE_CAPABILITIES,
    ALPACA_PAPER_CAPABILITIES,
    AlpacaBroker,
)
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import AlpacaSettings
from app.broker.alpaca.profile.credentials import AlpacaCredentialEnvironment
from app.broker.alpaca.profile.runtime_context import (
    AlpacaRuntimeContext,
    resolve_runtime_context,
)
from app.broker.contract.errors import BrokerAccountModeDisagreement
from tests.broker.alpaca.profile.conftest import (
    COMPLETE_ENVELOPE,
    DEFAULT_SLOT_KEY,
    DEFAULT_SLOT_SECRET,
    LIVE_SLOT_KEY,
    LIVE_SLOT_SECRET,
)


def _refuse_singleton() -> AlpacaSettings:  # pragma: no cover - must not run
    raise AssertionError("an injected broker read the process-wide settings singleton")


def _both_slots() -> AlpacaCredentialEnvironment:
    return AlpacaCredentialEnvironment(
        api_key_id=DEFAULT_SLOT_KEY,
        api_secret_key=DEFAULT_SLOT_SECRET,
        credential_live_key_id=LIVE_SLOT_KEY,
        credential_live_secret_key=LIVE_SLOT_SECRET,
    )


def _context(mode: str) -> AlpacaRuntimeContext:
    if mode == "paper":
        return resolve_runtime_context(
            endpoint_mode="paper", credential_slot="default", environment=_both_slots()
        )
    return resolve_runtime_context(
        endpoint_mode="live",
        credential_slot="live",
        live_envelope=COMPLETE_ENVELOPE,
        environment=_both_slots(),
    )


def _client_returning(payload: dict[str, Any]) -> Any:
    client = MagicMock()

    async def _get_account() -> dict[str, Any]:
        return payload

    client.get_account = _get_account
    return client


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("paper", ALPACA_PAPER_CAPABILITIES), ("live", ALPACA_LIVE_CAPABILITIES)],
)
def test_an_injected_context_selects_the_capability_descriptor(
    monkeypatch: pytest.MonkeyPatch, mode: str, expected: Any
) -> None:
    monkeypatch.setattr("app.broker.alpaca.broker.get_alpaca_settings", _refuse_singleton)

    broker = AlpacaBroker(MagicMock(), settings=_context(mode).settings)

    assert broker.capabilities() is expected


async def test_an_injected_context_supplies_the_account_mode(
    monkeypatch: pytest.MonkeyPatch, load_alpaca_fixture: Any
) -> None:
    monkeypatch.setattr("app.broker.alpaca.broker.get_alpaca_settings", _refuse_singleton)
    broker = AlpacaBroker(
        _client_returning(load_alpaca_fixture("account", "account.json")),
        settings=_context("paper").settings,
    )

    snapshot = await broker.get_account()

    assert snapshot.account_mode == "paper"


async def test_an_injected_live_context_still_refuses_a_paper_shaped_account(
    monkeypatch: pytest.MonkeyPatch, load_alpaca_fixture: Any
) -> None:
    # The ADR 0059 D1 refusal must survive the move to injected settings: the
    # mode that selected the endpoint is configuration truth, and the account
    # number's shape is a refusal input.
    monkeypatch.setattr("app.broker.alpaca.broker.get_alpaca_settings", _refuse_singleton)
    broker = AlpacaBroker(
        _client_returning(load_alpaca_fixture("account", "account.json")),
        settings=_context("live").settings,
    )

    with pytest.raises(BrokerAccountModeDisagreement):
        await broker.get_account()


def test_a_broker_without_injected_settings_still_defers_to_the_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = AlpacaSettings(
        api_key_id=DEFAULT_SLOT_KEY, api_secret_key=DEFAULT_SLOT_SECRET, mode="paper"
    )
    monkeypatch.setattr("app.broker.alpaca.broker.get_alpaca_settings", lambda: settings)

    assert AlpacaBroker(MagicMock()).capabilities() is ALPACA_PAPER_CAPABILITIES


def test_constructing_a_broker_never_reads_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Registration happens at startup on a credential-free service; reading
    # settings eagerly would refuse to boot it.
    monkeypatch.setattr("app.broker.alpaca.broker.get_alpaca_settings", _refuse_singleton)

    assert AlpacaBroker().broker_id == "alpaca"


def test_a_broker_hands_its_injected_settings_to_the_client_it_builds() -> None:
    context = _context("live")

    client = AlpacaBroker(settings=context.settings)._client

    assert isinstance(client, AlpacaTradingClient)
    assert client.bound_settings is context.settings


def test_a_broker_refuses_settings_that_disagree_with_its_client() -> None:
    # A broker and its client are one binding. Accepting two would let the port
    # stamp one mode on a snapshot the client fetched from the other mode's
    # endpoint — the cross-contamination injected settings exist to prevent.
    paper = _context("paper")
    live = _context("live")

    with pytest.raises(ValueError, match="one binding"):
        AlpacaBroker(AlpacaTradingClient(settings=paper.settings), settings=live.settings)


def test_a_broker_accepts_a_client_bound_to_the_same_settings() -> None:
    context = _context("live")

    broker = AlpacaBroker(
        AlpacaTradingClient(settings=context.settings), settings=context.settings
    )

    assert broker.capabilities() is ALPACA_LIVE_CAPABILITIES
