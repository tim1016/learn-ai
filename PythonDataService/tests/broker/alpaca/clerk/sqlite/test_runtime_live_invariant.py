"""A live sqlite facade cannot exist without its envelope and its arming gate (slice 7, R4)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES, _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0


def _facade(repo: ClerkSqliteRepository, **kwargs) -> SqliteAlpacaClerkFacade:
    broker = _LiveBroker(now_ms=T0)
    return SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, authority_kind="sqlite", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"account_mode": "live"},
        {"account_mode": "live", "live_envelope": LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False)},
        {"account_mode": "live", "live_arming": ArmingGate()},
    ],
    ids=["nothing", "envelope-only", "gate-only"],
)
def test_a_live_facade_refuses_to_exist_without_both_gates(envelope_repo: ClerkSqliteRepository, kwargs) -> None:
    with pytest.raises(AccountAuthorityIdentityError):
        _facade(envelope_repo, **kwargs)


def test_a_live_facade_with_both_gates_exposes_them_and_its_mode(envelope_repo: ClerkSqliteRepository) -> None:
    gate = ArmingGate()
    facade = _facade(
        envelope_repo,
        account_mode="live",
        live_envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False),
        live_arming=gate,
    )
    assert facade.live_arming is gate
    assert facade.account_mode == "live"


def test_a_paper_facade_is_untouched(envelope_repo: ClerkSqliteRepository) -> None:
    facade = _facade(envelope_repo, account_mode="paper")
    assert facade.live_arming is None
    assert facade.account_mode == "paper"
