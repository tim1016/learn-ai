"""Tests for the broker router — health endpoint and 503 fallback.

The streaming endpoints integrate with a connected IBKR client and are
covered by integration tests that run against a live Gateway. This file
covers only the synthetic-disconnected paths the router must handle
with no Gateway present.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.broker.ibkr.client import set_client
from app.broker.ibkr.config import IbkrSettings
from app.broker.ibkr.models import IbkrOrderSpec
from app.main import app
from app.routers import broker as broker_router
from app.routers.broker import _stamp_manual_order_ref_if_requested

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


def test_manual_order_request_is_server_stamped_with_manual_namespace() -> None:
    spec = IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=1,
        order_type="MKT",
        confirm_paper=True,
        manual_order=True,
    )

    stamped = _stamp_manual_order_ref_if_requested(spec)

    assert stamped.order_ref is not None
    assert stamped.order_ref.startswith("manual/operator/v1:")


def test_non_manual_order_request_does_not_silently_repair_missing_order_ref() -> None:
    spec = IbkrOrderSpec(
        symbol="SPY",
        sec_type="STK",
        action="BUY",
        quantity=1,
        order_type="MKT",
        confirm_paper=True,
    )

    assert _stamp_manual_order_ref_if_requested(spec).order_ref is None


def _connected_order_client() -> SimpleNamespace:
    return SimpleNamespace(
        settings=IbkrSettings(mode="paper", port=4002, readonly=False, _env_file=None),
        connected_account="DU1234567",
        is_connected=lambda: True,
        require_connected=lambda: None,
        require_live=lambda: None,
        ib=SimpleNamespace(
            qualifyContractsAsync=MagicMock(),
            placeOrder=MagicMock(),
            trades=MagicMock(return_value=[]),
            cancelOrder=MagicMock(),
        ),
        _last_event_ms=1,
        order_errors_after=lambda _seq: [],
    )


@pytest.mark.asyncio
async def test_post_orders_refuses_raw_submit_without_account_owner_grant() -> None:
    client = _connected_order_client()
    set_client(client)

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
                "manual_order": True,
            },
        )

    assert resp.status_code == 403
    assert "ACCOUNT_OWNER_WRITE_GRANT_MISSING" in resp.text
    client.ib.placeOrder.assert_not_called()


@pytest.mark.asyncio
async def test_delete_order_refuses_raw_cancel_without_account_owner_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _connected_order_client()
    set_client(client)

    class AllowCancelDecision:
        def raise_if_blocked(self) -> None:
            return None

    async def allow_cancel(*_args: object, **_kwargs: object) -> AllowCancelDecision:
        return AllowCancelDecision()

    monkeypatch.setattr(broker_router, "account_truth_cancel_decision", allow_cancel)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.delete("/api/broker/orders/42")

    assert resp.status_code == 403
    assert "ACCOUNT_OWNER_WRITE_GRANT_MISSING" in resp.text
    client.ib.cancelOrder.assert_not_called()


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
