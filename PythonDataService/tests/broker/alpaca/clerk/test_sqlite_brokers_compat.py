"""Existing Broker Desk routes fail closed under SQLite authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.sqlite.projection_errors import ProjectionReadError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_uncertainty
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.config import settings
from app.routers import brokers as brokers_router
from app.routers.brokers import router
from app.security.data_plane_control import CONTROL_SECRET_HEADER


class _UnusedBroker:
    broker_id = "alpaca"

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("generic recovery must not contact the broker")


@pytest.fixture()
def sqlite_desk(tmp_path: Path):
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-SQLITE-DESK",
        artifacts_root=tmp_path,
    )
    repo.register_strategy_instance(
        strategy_instance_id="spy-bot",
        symbol="SPY",
        config_hash="config-1",
    )
    raise_uncertainty(
        repo,
        strategy_instance_id="spy-bot",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        headline="Order outcome remains unknown",
        explanation="The broker has not proven a terminal outcome.",
        operator_impact="Only this bot is blocked from new exposure.",
        next_step="Use the evidence-bound recovery actions.",
        evidence_refs=("order:one",),
    )
    broker = _UnusedBroker()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=broker,  # type: ignore[arg-type]
        trade=broker,  # type: ignore[arg-type]
        stream_health=StreamHealthGate(
            market_data=lambda: ChannelHealth(
                stream="market_data",
                healthy=True,
                reason="",
                observed_at_ms=10,
            ),
            execution=lambda: ChannelHealth(
                stream="execution",
                healthy=True,
                reason="",
                observed_at_ms=11,
            ),
        ),
    )
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=facade)
    )
    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)
        repo.close()


@pytest.mark.asyncio
async def test_existing_reads_project_active_sqlite_authority(sqlite_desk: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk), base_url="http://test"
    ) as client:
        status = await client.get("/api/brokers/alpaca/clerk/status")
        diagnosis = await client.get(
            "/api/brokers/alpaca/clerk/custody-diagnosis"
        )

    assert status.status_code == 200
    assert status.json()["authority_kind"] == "sqlite"
    assert status.json()["channel_healths"] == [
        {
            "stream": "market_data",
            "healthy": True,
            "reason": "",
            "observed_at_ms": 10,
        },
        {
            "stream": "execution",
            "healthy": True,
            "reason": "",
            "observed_at_ms": 11,
        },
    ]
    assert diagnosis.status_code == 200
    assert diagnosis.json()["authority_kind"] == "sqlite"
    assert diagnosis.json()["in_sync"] is False
    assert diagnosis.json()["resolvable"] is False
    assert diagnosis.json()["divergences"][0]["kind"] == "needs_review"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/brokers/alpaca/clerk/status",
        "/api/brokers/alpaca/clerk/custody-diagnosis",
    ],
)
@pytest.mark.parametrize("projection_failure", ["read_error", "missing"])
async def test_existing_reads_map_sqlite_projection_failures_to_typed_unavailable(
    sqlite_desk: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    projection_failure: str,
) -> None:
    """Both projection failure modes must fail closed on both retained reads."""

    def unreadable_projection(**_kwargs: Any) -> NoReturn:
        raise ProjectionReadError("simulated malformed durable projection")

    def missing_projection(**_kwargs: Any) -> None:
        return None

    projection_reader = (
        unreadable_projection
        if projection_failure == "read_error"
        else missing_projection
    )
    monkeypatch.setattr(brokers_router, "sqlite_projection", projection_reader)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk), base_url="http://test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "reason": "sqlite_projection_unavailable",
        "message": "The Account Clerk order record could not be read safely.",
        "next_step": "Keep broker actions blocked and retry after the Clerk projection is repaired.",
    }


@pytest.mark.asyncio
async def test_generic_recovery_routes_are_absent_under_sqlite(
    sqlite_desk: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=sqlite_desk), base_url="http://test"
    ) as client:
        headers = {CONTROL_SECRET_HEADER: settings.DATA_PLANE_CONTROL_SECRET}
        clear = await client.post(
            "/api/brokers/alpaca/clerk/clear-hold",
            json={"operator": "ops", "reason": "unsafe generic request"},
            headers=headers,
        )
        resolve = await client.post(
            "/api/brokers/alpaca/clerk/resolve",
            json={
                "reason": "unsafe generic request",
                "snapshot_version": "stale",
                "confirmation_token": "RESOLVE",
                "idempotency_key": "old-path",
            },
            headers=headers,
        )

    assert clear.status_code == resolve.status_code == 404
