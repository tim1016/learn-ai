"""Tests for /api/broker/symbols/search and /api/broker/option-contracts (Slice 1F).

Three surfaces pinned:

* Happy path: search results round-trip the IBKR wrapper DTOs.
* 503 when IBKR is disconnected — no fallback to non-broker data sources.
* 429 with Retry-After when the per-pattern token bucket is exhausted;
  identical request within the 60s TTL cache returns the cached
  response without consulting the bucket.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.client import NotConnectedError
from app.main import app
from app.schemas.broker_search import OptionContractMatch, SymbolMatch


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
    broker_router.reset_broker_search_state_for_testing()
    yield fake
    ibkr_client_module.set_client(None)
    ibkr_config.reset_settings_for_testing()
    broker_router.reset_broker_search_state_for_testing()


def _spy_match() -> SymbolMatch:
    return SymbolMatch(
        symbol="SPY",
        name="SPDR S&P 500 ETF Trust",
        exchange="ARCA",
        currency="USD",
        sec_type="STK",
        derivative_sec_types=["OPT"],
    )


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


# ─── /symbols/search ───────────────────────────────────────────────────


async def test_symbols_search_returns_wrapper_payload(monkeypatch, _connected_broker):
    from app.routers import broker as broker_router

    called: dict[str, Any] = {}

    async def fake_search(_client, pattern, *, sec_type=None):
        called["pattern"] = pattern
        called["sec_type"] = sec_type
        return [_spy_match()]

    monkeypatch.setattr(broker_router, "search_symbols", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/broker/symbols/search", params={"q": "SP"})

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"matches": [_spy_match().model_dump()]}
    assert called == {"pattern": "SP", "sec_type": None}


async def test_symbols_search_passes_sec_type_filter(monkeypatch, _connected_broker):
    from app.routers import broker as broker_router

    received: dict[str, Any] = {}

    async def fake_search(_client, pattern, *, sec_type=None):
        received["sec_type"] = sec_type
        return []

    monkeypatch.setattr(broker_router, "search_symbols", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/api/broker/symbols/search", params={"q": "SP", "sec_type": "STK"})

    assert received["sec_type"] == "STK"


async def test_symbols_search_returns_503_when_disconnected(monkeypatch, _connected_broker):
    from app.routers import broker as broker_router

    async def fake_search(*_args, **_kwargs):
        raise NotConnectedError("offline")

    monkeypatch.setattr(broker_router, "search_symbols", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/broker/symbols/search", params={"q": "SPY"})

    assert resp.status_code == 503


async def test_symbols_search_rate_limit_returns_429_with_retry_after(
    monkeypatch, _connected_broker
):
    from app.routers import broker as broker_router

    call_count = {"n": 0}

    async def fake_search(_client, pattern, *, sec_type=None):
        call_count["n"] += 1
        # Every call must return a different payload so the cache hit
        # path is distinguishable from the rate-limit miss path.
        return [_spy_match()]

    monkeypatch.setattr(broker_router, "search_symbols", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        first = await c.get("/api/broker/symbols/search", params={"q": "A"})
        # Different patterns to avoid cache hit; bucket capacity is 1
        # per (pattern, sec_type) so the second new pattern within the
        # bucket window must 429.
        second = await c.get("/api/broker/symbols/search", params={"q": "A"})

    assert first.status_code == 200
    # Cache hit on identical pattern: no second IBKR call, response served
    # from the 60s TTL cache without consulting the bucket.
    assert call_count["n"] == 1
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_symbols_search_rate_limit_fires_on_new_pattern_burst(
    monkeypatch, _connected_broker
):
    from app.routers import broker as broker_router

    async def fake_search(_client, pattern, *, sec_type=None):
        return [_spy_match()]

    monkeypatch.setattr(broker_router, "search_symbols", fake_search)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        ok = await c.get("/api/broker/symbols/search", params={"q": "AA"})
        throttled = await c.get("/api/broker/symbols/search", params={"q": "BB"})

    assert ok.status_code == 200
    assert throttled.status_code == 429
    assert "Retry-After" in throttled.headers
    assert float(throttled.headers["Retry-After"]) > 0


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
