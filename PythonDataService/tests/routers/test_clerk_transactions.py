from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.clerk_transactions import get_clerk_transaction_store, router
from app.schemas.clerk_transaction_projection import ClerkTransactionRow
from app.services.clerk_transaction_projection import ClerkTransactionProjectionUnavailable

pytestmark = pytest.mark.asyncio


class _NoIoStore:
    async def history_page(self, *, account_id: str, limit: int, after):
        assert account_id == "DU1219"
        assert limit == 25
        assert after is None
        return [], 12, 0

    async def feed_status(self, account_id: str) -> tuple[str, str, str, int | None, int | None, bool]:
        return "live", "Live", "Durable Clerk callback projection is current.", 12, 0, False

    async def transaction_detail(self, *, account_id: str, transaction_id: str) -> ClerkTransactionRow | None:
        assert account_id == "DU1219"
        assert transaction_id == "ctxn_opaque"
        return ClerkTransactionRow(
            transaction_id=transaction_id, account_id=account_id, journal_seq=12,
            recorded_at_ms=1_700_000_000_000, transaction_kind="manual_ibkr_acknowledgement",
            strategy_instance_id="manual", run_id="manual", intent_id="intent:opaque",
            order_ref="manual/v1:opaque", lifecycle_state="submitted",
            receipt={"order_ref": "manual/v1:opaque"}, events=[],
        )


class _UnavailableStore:
    async def history_page(self, **kwargs):
        raise ClerkTransactionProjectionUnavailable("disabled")

    async def transaction_detail(self, **kwargs):
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
        "feed_state": "live", "feed_headline": "Live", "feed_detail": "Durable Clerk callback projection is current.",
        "high_water_journal_seq": 12, "lag_records": 0, "lag_is_lower_bound": False, "rows": [], "next_cursor": None,
    }


async def test_history_endpoint_reports_unavailable_without_fallback_scan() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clerk_transaction_store] = lambda: _UnavailableStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions")
    assert response.status_code == 200
    assert response.json()["feed_state"] == "projection_unavailable"
    assert response.json()["canonical_fallback_required"] is True


async def test_selected_transaction_endpoint_reads_only_the_requested_projection_row() -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_clerk_transaction_store] = lambda: _NoIoStore()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/accounts/DU1219/transactions/ctxn_opaque")
    assert response.status_code == 200
    assert response.json()["receipt"] == {"order_ref": "manual/v1:opaque"}
