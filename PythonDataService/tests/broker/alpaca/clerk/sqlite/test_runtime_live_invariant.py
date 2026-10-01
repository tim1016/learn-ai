"""A live sqlite facade cannot exist without its envelope (ADR 0059 D4; the arming gate is retired, #2629)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from tests.broker.alpaca.clerk.live_envelope_fixtures import _LiveBroker
from tests.broker.alpaca.clerk.sqlite.conftest import ENVELOPE_T0 as T0


def _facade(repo: ClerkSqliteRepository, **kwargs) -> SqliteAlpacaClerkFacade:
    broker = _LiveBroker(now_ms=T0)
    return SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, authority_kind="sqlite", **kwargs)


def test_a_live_facade_refuses_to_exist_without_its_envelope(envelope_repo: ClerkSqliteRepository) -> None:
    with pytest.raises(AccountAuthorityIdentityError, match="requires a risk envelope"):
        _facade(envelope_repo, account_mode="live")
