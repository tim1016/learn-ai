from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.routers.engine import router


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = FastAPI()
    app.include_router(router, prefix="/api/engine")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_get_lean_source_does_not_require_a_launcher(client: AsyncClient) -> None:
    response = await client.get("/api/engine/strategies/ema_crossover_signal/lean-source")

    assert response.status_code == 200
    body = response.json()
    assert body["strategy_name"] == "ema_crossover_signal"
    assert body["template"] == "ema_crossover_signal"
    assert body["language"] == "python"
    assert "class MyAlgorithm(QCAlgorithm)" in body["source"]
    assert len(body["source_sha256"]) == 64


async def test_get_lean_source_returns_404_when_strategy_has_no_twin(client: AsyncClient) -> None:
    response = await client.get("/api/engine/strategies/spy_orb/lean-source")

    assert response.status_code == 404
    assert response.json()["detail"] == "Strategy 'spy_orb' has no registered LEAN validation source"
