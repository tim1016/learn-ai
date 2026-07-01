"""Tests for IBKR_BROKER_ENABLED=false behavior.

When IBKR_BROKER_ENABLED=false:
  - GET /api/broker/health → HTTP 200, disabled=True, connected=False, reason set
  - GET /api/broker/diagnose → discriminated union with disabled=True (DiagnosticReportDisabled)
  - GET /api/broker/account → HTTP 503
  - GET /api/broker/positions → HTTP 503
  - get_client() raises NotConnectedError (client was never set)

Uses httpx.AsyncClient + ASGITransport per repo testing rules.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture(autouse=True)
def broker_disabled(monkeypatch):
    """Set IBKR_BROKER_ENABLED=false and reset settings before / after each test."""
    from app.broker.ibkr import config as ibkr_config

    ibkr_config.reset_settings_for_testing()
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "false")
    ibkr_config.reset_settings_for_testing()

    yield

    ibkr_config.reset_settings_for_testing()


# ---------------------------------------------------------------------------
# /health — must return 200 with disabled=True even when broker is off
# ---------------------------------------------------------------------------


async def test_health_returns_200_when_disabled():
    """Disabled broker: /health must return 200, NOT 503."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/health")

    assert response.status_code == 200


async def test_health_disabled_flag_true():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/health")

    data = response.json()
    assert data["disabled"] is True


async def test_health_connected_false():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/health")

    data = response.json()
    assert data["connected"] is False


async def test_health_reason_field_set():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/health")

    data = response.json()
    assert data.get("reason") is not None
    assert len(data["reason"]) > 0


# ---------------------------------------------------------------------------
# /diagnose — discriminated union: disabled=True variant
# ---------------------------------------------------------------------------


async def test_diagnose_disabled_true():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/diagnose")

    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is True


async def test_diagnose_disabled_has_reason():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/diagnose")

    data = response.json()
    assert "reason" in data
    assert data["reason"]


async def test_diagnose_disabled_has_since_ms():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/diagnose")

    data = response.json()
    assert "since_ms" in data
    assert isinstance(data["since_ms"], int)
    assert data["since_ms"] > 0


async def test_diagnose_no_checks_field():
    """Disabled mode returns DiagnosticReportDisabled which has no 'checks' field."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/diagnose")

    data = response.json()
    assert "checks" not in data


# ---------------------------------------------------------------------------
# /account — must return 503 when disabled
# ---------------------------------------------------------------------------


async def test_account_returns_503_when_disabled():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/account")

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# /positions — must return 503 when disabled
# ---------------------------------------------------------------------------


async def test_positions_returns_503_when_disabled():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/positions")

    assert response.status_code == 503


async def test_account_truth_returns_canonical_503_when_disabled():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/account-truth")

    assert response.status_code == 503
    assert "IBKR_BROKER_ENABLED=false" in response.text


async def test_completed_orders_returns_canonical_503_when_disabled():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/orders/completed")

    assert response.status_code == 503
    assert "IBKR_BROKER_ENABLED=false" in response.text


async def test_order_what_if_returns_canonical_503_when_disabled():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/broker/orders/what-if",
            json={
                "symbol": "SPY",
                "sec_type": "STK",
                "action": "BUY",
                "quantity": 1,
                "order_type": "MKT",
                "confirm_paper": True,
            },
        )

    assert response.status_code == 503
    assert "IBKR_BROKER_ENABLED=false" in response.text


# ---------------------------------------------------------------------------
# get_client() raises NotConnectedError when disabled
# ---------------------------------------------------------------------------


async def test_get_client_raises_not_connected_when_disabled():
    """When disabled, get_client() must raise NotConnectedError (client was never set)."""
    from app.broker.ibkr.client import NotConnectedError, get_client

    with pytest.raises(NotConnectedError):
        get_client()


# ---------------------------------------------------------------------------
# Confirm that enabling flips the behavior (contrast test)
# ---------------------------------------------------------------------------


async def test_health_enabled_disabled_flag_false(monkeypatch):
    """When re-enabled (in-process), /health should return disabled=False."""
    from app.broker.ibkr import config as ibkr_config

    # Override back to enabled for this one test
    ibkr_config.reset_settings_for_testing()
    monkeypatch.setenv("IBKR_BROKER_ENABLED", "true")
    ibkr_config.reset_settings_for_testing()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/health")

    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is False

    # Clean up
    ibkr_config.reset_settings_for_testing()
