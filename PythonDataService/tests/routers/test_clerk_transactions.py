from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.clerk_transactions import get_clerk_transaction_store, router
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable

pytestmark = pytest.mark.asyncio


class _NoIoStore:
    async def history_page(self, *, account_id: str, limit: int, after):
        assert account_id == "DU1219"
        assert limit == 25
        assert after is None
        return [], 12, 0


class _UnavailableStore:
    async def history_page(self, **kwargs):
        raise ClerkTransactionProjectionUnavailable("disabled")


async def test_history_endpoint_is_bounded_projection_read_only() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clerk_transaction_store] = lambda: _NoIoStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions?limit=25")
    assert response.status_code == 200
    assert response.json() == {
        "projection_available": True, "canonical_fallback_required": False,
        "high_water_journal_seq": 12, "lag_records": 0, "rows": [], "next_cursor": None,
    }


async def test_history_endpoint_reports_unavailable_without_fallback_scan() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clerk_transaction_store] = lambda: _UnavailableStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions")
    assert response.status_code == 503
    assert "canonical acknowledgement remains durable" in response.json()["detail"]
