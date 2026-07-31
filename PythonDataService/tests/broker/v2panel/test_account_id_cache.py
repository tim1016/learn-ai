"""Regression tests for the account-id resolution cache (2026-07-30).

Every panel/catalog request validated the account with a full Alpaca REST
round-trip (5-15s against the paper API) just to re-learn a static account id.
The cache must (a) skip the broker call within the TTL and (b) miss naturally
when a different port instance is registered (tests, reconnect).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator

import pytest

from app.broker.contract.registry import (
    BrokerRegistry,
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.services.broker_account_snapshot import (
    clear_broker_account_snapshot_cache_for_testing,
)
from app.services.broker_v2_panel import panel_data_source as ds


class _CountingPort:
    broker_id = "alpaca"

    def __init__(self, account_id: str) -> None:
        self._account_id = account_id
        self.get_account_calls = 0

    async def get_account(self) -> object:
        self.get_account_calls += 1

        class _Account:
            account_id = self._account_id

        return _Account()

    def capabilities(self) -> None:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture()
def registry() -> Generator[BrokerRegistry, None, None]:
    reset_broker_registry_for_testing()
    clear_broker_account_snapshot_cache_for_testing()
    try:
        yield get_broker_registry()
    finally:
        clear_broker_account_snapshot_cache_for_testing()
        reset_broker_registry_for_testing()


@pytest.mark.asyncio
async def test_resolve_account_id_cached_within_ttl(registry: BrokerRegistry) -> None:
    port = _CountingPort("PA-CACHED")
    registry.register(port)  # type: ignore[arg-type]

    first = await ds.resolve_account_id("alpaca")
    second = await ds.resolve_account_id("alpaca")

    assert first == second == "PA-CACHED"
    assert port.get_account_calls == 1


@pytest.mark.asyncio
async def test_resolve_account_id_coalesces_concurrent_reads(
    registry: BrokerRegistry,
) -> None:
    port = _CountingPort("PA-COALESCED")
    registry.register(port)  # type: ignore[arg-type]

    first, second = await asyncio.gather(
        ds.resolve_account_id("alpaca"),
        ds.resolve_account_id("alpaca"),
    )

    assert first == second == "PA-COALESCED"
    assert port.get_account_calls == 1


@pytest.mark.asyncio
async def test_resolve_account_id_misses_for_new_port(
    registry: BrokerRegistry,
) -> None:
    old_port = _CountingPort("PA-OLD")
    registry.register(old_port)  # type: ignore[arg-type]
    assert await ds.resolve_account_id("alpaca") == "PA-OLD"

    reset_broker_registry_for_testing()
    new_port = _CountingPort("PA-NEW")
    get_broker_registry().register(new_port)  # type: ignore[arg-type]

    assert await ds.resolve_account_id("alpaca") == "PA-NEW"
    assert new_port.get_account_calls == 1


@pytest.mark.asyncio
async def test_cancelled_waiters_do_not_retain_failed_read(
    registry: BrokerRegistry,
) -> None:
    class _RecoveringPort(_CountingPort):
        def __init__(self) -> None:
            super().__init__("PA-RECOVERED")
            self.started = asyncio.Event()
            self.release_failure = asyncio.Event()
            self.failed = asyncio.Event()

        async def get_account(self) -> object:
            self.get_account_calls += 1
            if self.get_account_calls == 1:
                self.started.set()
                await self.release_failure.wait()
                self.failed.set()
                raise RuntimeError("first read failed")

            class _Account:
                account_id = self._account_id

            return _Account()

    port = _RecoveringPort()
    registry.register(port)  # type: ignore[arg-type]
    first = asyncio.create_task(ds.resolve_account_id("alpaca"))
    second = asyncio.create_task(ds.resolve_account_id("alpaca"))
    await port.started.wait()

    first.cancel()
    second.cancel()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)

    port.release_failure.set()
    await port.failed.wait()
    await asyncio.sleep(0)

    assert await ds.resolve_account_id("alpaca") == "PA-RECOVERED"
    assert port.get_account_calls == 2
