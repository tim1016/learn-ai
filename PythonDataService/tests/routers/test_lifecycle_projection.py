from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.lifecycle_projection import router
from app.schemas.lifecycle_projection import LifecycleProjectionEventRow
from app.services.lifecycle_projection_store import (
    LifecycleProjectionUnavailable,
    get_lifecycle_projection_store,
)

pytestmark = pytest.mark.asyncio


class _FakeStore:
    async def select_timeline(
        self,
        *,
        account_id: str | None = None,
        strategy_instance_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[LifecycleProjectionEventRow]:
        assert account_id == "DU123"
        assert strategy_instance_id == "bot-a"
        assert run_id is None
        assert limit == 5
        return [
            LifecycleProjectionEventRow(
                id=1,
                account_id="DU123",
                strategy_instance_id="bot-a",
                run_id="run-1",
                event_id="intent_wal:run-1:2:ACK_FAILED_UNCERTAIN",
                event_type="BrokerOrderUncertain",
                category="order",
                node_id="ack_or_reconcile",
                status="blocked",
                severity="warning",
                ts_ms=1_700_000_000_000,
                ts_ms_resolved=True,
                source_artifact="/tmp/run-1/intent_events.jsonl",
                source_type="broker_ack",
                source_rank=50,
                source_seq=2,
                summary="Broker acknowledgement failed; submit outcome is uncertain.",
                operator_next_step="PROBE_BROKER_BEFORE_RETRY",
                receipt_payload={"intent_id": "intent-2"},
                evidence_refs=[{"source": "intent_wal"}],
                inserted_at_ms=1_700_000_000_100,
                updated_at_ms=1_700_000_000_100,
            )
        ]

    async def select_safety_triage(
        self,
        *,
        account_id: str | None = None,
        strategy_instance_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        event_type: str | None = None,
        node_id: str | None = None,
        severity: str | None = None,
        limit: int = 100,
    ) -> list[LifecycleProjectionEventRow]:
        assert account_id == "DU123"
        assert strategy_instance_id == "bot-a"
        assert run_id == "run-1"
        assert status == "blocked"
        assert event_type == "BrokerOrderUncertain"
        assert node_id == "ack_or_reconcile"
        assert severity == "warning"
        assert limit == 10
        return []


class _UnavailableStore:
    async def select_timeline(self, **_kwargs):
        raise LifecycleProjectionUnavailable("disabled")

    async def select_safety_triage(self, **_kwargs):
        raise LifecycleProjectionUnavailable("disabled")


def _app_with_store(store: object) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_lifecycle_projection_store] = lambda: store
    return app


async def test_timeline_endpoint_renders_backend_authored_rows() -> None:
    app = _app_with_store(_FakeStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/lifecycle-projection/timeline",
            params={"account_id": "DU123", "strategy_instance_id": "bot-a", "limit": 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["projection_available"] is True
    assert payload["canonical_fallback_required"] is False
    assert payload["rows"][0]["summary"] == "Broker acknowledgement failed; submit outcome is uncertain."
    assert payload["rows"][0]["operator_next_step"] == "PROBE_BROKER_BEFORE_RETRY"
    assert payload["rows"][0]["receipt_payload"]["intent_id"] == "intent-2"


async def test_safety_triage_endpoint_applies_bounded_filters() -> None:
    app = _app_with_store(_FakeStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/lifecycle-projection/safety-triage",
            params={
                "account_id": "DU123",
                "strategy_instance_id": "bot-a",
                "run_id": "run-1",
                "status": "blocked",
                "event_type": "BrokerOrderUncertain",
                "node_id": "ack_or_reconcile",
                "severity": "warning",
                "limit": 10,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "projection_available": True,
        "canonical_fallback_required": False,
        "rows": [],
    }


async def test_safety_triage_rejects_non_safety_severity() -> None:
    app = _app_with_store(_FakeStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/lifecycle-projection/safety-triage", params={"severity": "info"})

    assert response.status_code == 422


async def test_projection_unavailable_returns_503_for_fallback() -> None:
    app = _app_with_store(_UnavailableStore())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/lifecycle-projection/timeline", params={"account_id": "DU123"})

    assert response.status_code == 503
    assert "canonical file-backed status" in response.json()["detail"]
