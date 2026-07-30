"""Regression tests for the account-id resolution cache (2026-07-30).

Every panel/catalog request validated the account with a full Alpaca REST
round-trip (5-15s against the paper API) just to re-learn a static account id.
The cache must (a) skip the broker call within the TTL and (b) miss naturally
when a different port instance is registered (tests, reconnect).
"""

from __future__ import annotations

import pytest

from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.services.broker_v2_panel import panel_data_source as ds


class _CountingPort:
    broker_id = "alpaca"

    def __init__(self, account_id: str) -> None:
        self._account_id = account_id
        self.get_account_calls = 0

    async def get_account(self):
        self.get_account_calls += 1

        class _Account:
            account_id = self._account_id

        return _Account()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture()
def registry():
    reset_broker_registry_for_testing()
    try:
        yield get_broker_registry()
    finally:
        reset_broker_registry_for_testing()


@pytest.mark.asyncio
async def test_resolve_account_id_cached_within_ttl(registry) -> None:
    port = _CountingPort("PA-CACHED")
    registry.register(port)  # type: ignore[arg-type]

    first = await ds.resolve_account_id("alpaca")
    second = await ds.resolve_account_id("alpaca")

    assert first == second == "PA-CACHED"
    assert port.get_account_calls == 1


@pytest.mark.asyncio
async def test_resolve_account_id_misses_for_new_port(registry) -> None:
    old_port = _CountingPort("PA-OLD")
    registry.register(old_port)  # type: ignore[arg-type]
    assert await ds.resolve_account_id("alpaca") == "PA-OLD"

    reset_broker_registry_for_testing()
    new_port = _CountingPort("PA-NEW")
    get_broker_registry().register(new_port)  # type: ignore[arg-type]

    assert await ds.resolve_account_id("alpaca") == "PA-NEW"
    assert new_port.get_account_calls == 1
