"""GET /api/brokers/{broker}/fees/session-reconciliation."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.routers import brokers as brokers_router
from app.utils.session_anchors import et_midnight_ms

SESSION_OPEN_MS = session_open_ms_utc(date(2026, 9, 8))
PATH = "/api/brokers/alpaca/fees/session-reconciliation"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(brokers_router.router)
    return app


class _Port:
    async def list_activities(self, *, after_ms: int | None = None, limit: int = 100) -> list:
        return []


@pytest.mark.parametrize(
    "session_open_ms",
    [
        SESSION_OPEN_MS + 60_000,  # a minute after the open is not the session anchor
        et_midnight_ms(date(2026, 9, 8)),  # ET midnight is a different anchor
        session_open_ms_utc(date(2026, 9, 4)) + 3 * 24 * 60 * 60 * 1000,  # Labor Day 2026-09-07
    ],
)
async def test_rejects_a_value_that_is_not_a_trading_days_session_open(session_open_ms: int) -> None:
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(PATH, params={"session_open_ms": session_open_ms})

    assert response.status_code == 422
    assert "session open" in response.json()["detail"]


async def test_reports_unavailable_without_an_active_sqlite_clerk(monkeypatch: pytest.MonkeyPatch) -> None:
    set_active_clerk_runtime(None)
    monkeypatch.setattr(brokers_router, "_resolve_port", lambda broker: _Port())

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(PATH, params={"session_open_ms": SESSION_OPEN_MS})

    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "unavailable"
    assert body["session_open_ms"] == SESSION_OPEN_MS
    assert body["predicted"] is None
