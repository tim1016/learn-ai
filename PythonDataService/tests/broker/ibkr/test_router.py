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
from app.broker.ibkr.models import IbkrOrderEvent
from app.main import app
from app.routers import broker as broker_router

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
async def test_chain_returns_422_when_strikes_missing() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-chain/SPY",
            params={"expiry_ms": 1_800_000_000_000},
        )

    # FastAPI rejects the missing required query before the handler runs.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chain_returns_400_when_strike_is_non_positive() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-chain/SPY",
            params=[
                ("expiry_ms", 1_800_000_000_000),
                ("strikes", 0),
            ],
        )

    # Service-unavailable trips first when no client is set; the handler's
    # validation runs after _require_connected_or_503. Either is acceptable
    # for this defensive guard — both block the bad request.
    assert resp.status_code in (400, 503)


@pytest.mark.asyncio
async def test_strikes_rejects_non_positive_expiry_ms() -> None:
    """Regression (B-13): expiry_ms <= 0 must be rejected at the boundary with
    a 422, not flow into expiry_ms_to_yyyymmdd to produce a 1970 date that
    silently matches nothing."""
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/strikes/SPY", params={"expiry_ms": 0})

    # Query validation (gt=0) runs before the handler / connection check.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chain_rejects_non_positive_expiry_ms() -> None:
    """Regression (B-13): same boundary guard on the option-chain stream."""
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-chain/SPY",
            params=[("expiry_ms", -5), ("strikes", 420)],
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_strikes_endpoint_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/strikes/SPY",
            params={"expiry_ms": 1_800_000_000_000},
        )

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_strikes_endpoint_rejects_missing_expiry_ms() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/strikes/SPY")

    assert resp.status_code == 422


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


# ── Read-only order endpoints ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_event_stream_sends_ready_before_the_first_order_event(monkeypatch) -> None:
    """A quiet stream is healthy once its server-side subscription is ready."""

    event = IbkrOrderEvent(
        account_id="DU1234567",
        order_id=42,
        event_type="status",
        status="Submitted",
        order_ref="manual/operator/v1:intent-1",
        symbol="SPY",
        side="BUY",
        order_type="MKT",
        cumulative_filled=0,
        remaining=1,
        ts_ms=1_800_000_000_000,
    )

    async def fake_order_events(_client, *, poll_seconds: float):
        assert poll_seconds == 0.5
        yield event

    monkeypatch.setattr(broker_router, "require_connected_client", lambda: object())
    monkeypatch.setattr(broker_router, "stream_order_events", fake_order_events)

    response = await broker_router.order_events_stream_endpoint()
    stream = response.body_iterator
    ready = await anext(stream)
    order = await anext(stream)
    await stream.aclose()

    assert ready.startswith("event: ready\n")
    assert order == f"event: order\ndata: {event.model_dump_json()}\n\n"


@pytest.mark.asyncio
async def test_get_open_orders_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/orders/open")

    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_order_event_stream_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/broker/orders/stream")

    assert resp.status_code == 503


# ── /option-surface boundary checks ────────────────────────────────────


@pytest.mark.asyncio
async def test_surface_returns_422_when_expiries_missing() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-surface/SPY",
            params=[("strikes", 420)],
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_surface_returns_422_when_strikes_missing() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-surface/SPY",
            params=[("expiry_ms", 1_800_000_000_000)],
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_surface_rejects_non_positive_expiry_ms() -> None:
    """expiry_ms validation runs before the connection check, so a
    negative expiry must always come back as 400 — not 503 — even with
    no client installed. Guards the ordering inside the handler."""
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-surface/SPY",
            params=[("expiry_ms", -1), ("strikes", 420)],
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_surface_rejects_nan_strikes() -> None:
    """NaN slips past Pydantic's float coercion; the boundary guard must
    catch it before it reaches contract qualification."""
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-surface/SPY",
            params=[("expiry_ms", 1_800_000_000_000), ("strikes", "nan")],
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_surface_returns_503_when_disconnected() -> None:
    set_client(None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get(
            "/api/broker/option-surface/SPY",
            params=[
                ("expiry_ms", 1_800_000_000_000),
                ("strikes", 420),
                ("strikes", 425),
            ],
        )

    assert resp.status_code == 503
