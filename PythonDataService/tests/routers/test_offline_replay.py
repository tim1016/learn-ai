from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.offline_replay import router
from app.schemas.offline_replay import (
    OfflineReplayCatalogResponse,
    OfflineReplayCatalogSession,
    OfflineReplaySessionListResponse,
    OfflineReplaySessionResponse,
)
from app.services.offline_replay_service import (
    OfflineReplayConflictError,
    OfflineReplayNotFoundError,
    get_offline_replay_service,
)

SESSION_OPEN_MS = 1_775_000_000_000


def _session(status: str = "running") -> OfflineReplaySessionResponse:
    return OfflineReplaySessionResponse(
        session_id="replay-1",
        status=status,
        strategy_key="ema_crossover_signal",
        session_date_ms=SESSION_OPEN_MS,
        session_open_ms=SESSION_OPEN_MS,
        session_close_ms=SESSION_OPEN_MS + 390 * 60_000,
        warmup_end_ms=SESSION_OPEN_MS + 225 * 60_000,
        playback_end_ms=SESSION_OPEN_MS + 285 * 60_000,
        playhead_ms=SESSION_OPEN_MS + 230 * 60_000,
        playback_minutes=60,
        speed=60,
        created_at_ms=SESSION_OPEN_MS,
        started_at_ms=SESSION_OPEN_MS,
        completed_at_ms=None,
        symbols=["SPY", "TSLA"],
        bots=[
            {
                "symbol": symbol,
                "status": "running",
                "run_id": f"replay-1-{symbol.lower()}",
                "bars_processed": 230,
                "decisions_emitted": 1,
                "orders_submitted": 0,
                "fills_observed": 0,
                "open_position_quantity": 0,
                "final_equity_usd": Decimal("100000"),
                "data_sha256": "abc",
            }
            for symbol in ("SPY", "TSLA")
        ],
    )


class _FakeService:
    async def catalog(self) -> OfflineReplayCatalogResponse:
        return OfflineReplayCatalogResponse(
            sessions=[
                OfflineReplayCatalogSession(
                    session_date_ms=SESSION_OPEN_MS,
                    session_open_ms=SESSION_OPEN_MS,
                    session_close_ms=SESSION_OPEN_MS + 390 * 60_000,
                    eligible=True,
                    cached_symbols=["SPY"],
                )
            ],
            recommended_session_date_ms=SESSION_OPEN_MS,
        )

    def list_sessions(self) -> OfflineReplaySessionListResponse:
        return OfflineReplaySessionListResponse(sessions=[_session()])

    async def create_session(self, request: object) -> OfflineReplaySessionResponse:
        return _session()

    def get_session(self, session_id: str) -> OfflineReplaySessionResponse:
        if session_id == "missing":
            raise OfflineReplayNotFoundError(session_id)
        return _session()

    async def command(self, session_id: str, request: object) -> OfflineReplaySessionResponse:
        if session_id == "terminal":
            raise OfflineReplayConflictError("terminal replay sessions cannot accept commands")
        return _session("paused")


@pytest.fixture
def replay_app() -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(router, prefix="/api/offline-replay")
    test_app.dependency_overrides[get_offline_replay_service] = _FakeService
    return test_app


@pytest.mark.asyncio
async def test_catalog_and_launch_contracts_use_numeric_timestamps(
    replay_app: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=replay_app),
        base_url="http://test",
    ) as client:
        catalog = await client.get("/api/offline-replay/catalog")
        launched = await client.post(
            "/api/offline-replay/sessions",
            json={
                "session_date_ms": SESSION_OPEN_MS,
                "symbols": ["SPY", "TSLA"],
                "playback_minutes": 60,
                "speed": 60,
            },
        )

    assert catalog.status_code == 200
    assert isinstance(catalog.json()["sessions"][0]["session_open_ms"], int)
    assert launched.status_code == 202
    assert launched.json()["symbols"] == ["SPY", "TSLA"]


@pytest.mark.asyncio
async def test_command_conflict_and_missing_session_are_typed(
    replay_app: FastAPI,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=replay_app),
        base_url="http://test",
    ) as client:
        missing = await client.get("/api/offline-replay/sessions/missing")
        conflict = await client.post(
            "/api/offline-replay/sessions/terminal/commands",
            json={"action": "pause"},
        )

    assert missing.status_code == 404
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_launch_rejects_a_missing_symbol(replay_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=replay_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/offline-replay/sessions",
            json={
                "session_date_ms": SESSION_OPEN_MS,
                "symbols": ["SPY"],
                "playback_minutes": 30,
                "speed": 60,
            },
        )

    assert response.status_code == 422
    assert "requires exactly SPY and TSLA" in response.text
