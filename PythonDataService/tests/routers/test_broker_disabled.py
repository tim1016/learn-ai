"""The broker health read reports when the IBKR connection is disabled."""

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


async def test_health_reports_disabled_connection():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/health")

    assert response.status_code == 200
    data = response.json()
    assert data["disabled"] is True
    assert data["connected"] is False
    assert data.get("reason") is not None
    assert len(data["reason"]) > 0


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
