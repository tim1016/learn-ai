"""Tests for the broker router — health endpoint and 503 fallback.

The streaming endpoints integrate with a connected IBKR client and are
covered by integration tests that run against a live Gateway. This file
covers only the synthetic-disconnected paths the router must handle
with no Gateway present.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.client import set_client
from app.main import app

# ── Phase 1 endpoints ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_returns_disconnected_when_no_client(monkeypatch) -> None:
    set_client(None)
    monkeypatch.setenv("IBKR_MODE", "paper")
    monkeypatch.setenv("IBKR_PORT", "4002")

    # Force settings reset so monkeypatched env applies.
    from app.broker.ibkr import config as cfg

    cfg.reset_settings_for_testing()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            resp = await ac.get("/api/broker/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["connected"] is False
        assert body["mode"] == "paper"
        assert body["port"] == 4002
        assert body["account_id"] is None
    finally:
        # Drop the cached settings so subsequent tests see whatever env
        # the surrounding fixtures set, not the IBKR_PORT=4002 from above.
        cfg.reset_settings_for_testing()


@pytest.mark.asyncio
async def test_expirations_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/expirations/SPY")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_chain_returns_400_when_strike_max_lt_min() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-chain/SPY",
            params={
                "expiry_ms": 1_800_000_000_000,
                "strike_min": 500,
                "strike_max": 400,
            },
        )

    assert resp.status_code == 400


# ── Phase 2a endpoints ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_account_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/account")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_positions_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/positions")

    assert resp.status_code == 503


# ── Phase 2b endpoints ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pnl_account_stream_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/pnl/stream")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_pnl_positions_stream_returns_422_when_no_con_ids() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/pnl/positions/stream")

    # FastAPI rejects missing required query before the handler runs.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pnl_positions_stream_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/pnl/positions/stream?con_ids=700001&con_ids=700002"
        )

    assert resp.status_code == 503


# ── Phase 3a endpoints ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_orders_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/broker/orders",
            json={
                "symbol": "SPY",
                "sec_type": "STK",
                "action": "BUY",
                "quantity": 1,
                "order_type": "MKT",
                "confirm_paper": True,
            },
        )

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_post_orders_rejects_missing_confirm_paper_field() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/broker/orders",
            json={
                "symbol": "SPY",
                "sec_type": "STK",
                "action": "BUY",
                "quantity": 1,
                "order_type": "MKT",
                # confirm_paper omitted entirely
            },
        )

    # FastAPI validates required fields before the handler runs.
    assert resp.status_code == 422


# ── Phase 3b endpoints ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_open_orders_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/orders/open")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_delete_order_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete("/api/broker/orders/42")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_order_event_stream_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/orders/stream")

    assert resp.status_code == 503
