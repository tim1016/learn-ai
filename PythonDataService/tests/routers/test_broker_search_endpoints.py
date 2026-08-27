"""Tests for /api/broker/option-contracts (Slice 1F).

Surfaces pinned: happy path (drill-down results round-trip the IBKR
wrapper DTO), 503 when IBKR is disconnected (no fallback to non-broker
data sources), and 422 on an invalid ``right`` query param.

``/api/broker/symbols/search`` was retired along with
``app/broker/ibkr/symbol_search.py`` and the `TokenBucket` throttle it
was the sole user of (PR-B of #1813, 2026-08-27); its 6 tests were
removed with it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.client import NotConnectedError
from app.main import app
from app.schemas.broker_search import OptionContractMatch


class _FakeConnectedClient:
    """Minimal stand-in returned by ``_require_connected_or_503``. The
    actual IBKR calls are monkeypatched at the wrapper level so this
    client just needs to satisfy the connection-state predicates."""

    def is_connected(self) -> bool:
        return True

    def require_connected(self) -> None:
        return None


@pytest.fixture
def _connected_broker(monkeypatch):
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    from app.broker.ibkr import client as ibkr_client_module
    from app.broker.ibkr import config as ibkr_config
    from app.routers import broker as broker_router

    ibkr_config.reset_settings_for_testing()
    fake = _FakeConnectedClient()
    ibkr_client_module.set_client(fake)  # type: ignore[arg-type]
    monkeypatch.setattr(broker_router, "_ibkr_client_factory", lambda: fake)
    # Reset the in-process throttle / cache between tests so token state
    # from a previous test does not bleed into the next assertion.
    broker_router.reset_option_contracts_cache_for_testing()
    yield fake
    ibkr_client_module.set_client(None)
    ibkr_config.reset_settings_for_testing()
    broker_router.reset_option_contracts_cache_for_testing()


def _spy_call_match() -> OptionContractMatch:
    return OptionContractMatch(
        con_id=42,
        symbol="SPY",
        local_symbol="SPY   251219C00650000",
        trading_class="SPY",
        exchange="SMART",
        currency="USD",
        expiry_ms=1_766_188_800_000,
        strike=650.0,
        right="C",
        multiplier=100,
    )


# ─── /option-contracts/{symbol} ────────────────────────────────────────


async def test_option_contracts_returns_qualified_match(monkeypatch, _connected_broker):
    from app.routers import broker as broker_router

    async def fake_search(_client, *, symbol, expiry_ms, strike, right):
        return [_spy_call_match()]

    monkeypatch.setattr(broker_router, "search_option_contracts", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/broker/option-contracts/SPY",
            params={
                "expiry_ms": 1_766_188_800_000,
                "strike": 650.0,
                "right": "C",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {"matches": [_spy_call_match().model_dump()]}


async def test_option_contracts_returns_503_when_disconnected(monkeypatch, _connected_broker):
    from app.routers import broker as broker_router

    async def fake_search(*_args, **_kwargs):
        raise NotConnectedError("offline")

    monkeypatch.setattr(broker_router, "search_option_contracts", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/broker/option-contracts/SPY",
            params={"expiry_ms": 1_766_188_800_000, "strike": 650.0, "right": "C"},
        )

    assert resp.status_code == 503


async def test_option_contracts_validates_right_query_param(monkeypatch, _connected_broker):
    from app.routers import broker as broker_router

    monkeypatch.setattr(
        broker_router,
        "search_option_contracts",
        SimpleNamespace(__call__=lambda *a, **k: []),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get(
            "/api/broker/option-contracts/SPY",
            params={"expiry_ms": 1_766_188_800_000, "strike": 650.0, "right": "X"},
        )

    assert resp.status_code == 422
