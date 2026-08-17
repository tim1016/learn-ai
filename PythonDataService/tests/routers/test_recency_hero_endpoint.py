"""POST /api/research/recency/hero — Python-owned visible-window selection."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.main import app


@pytest.mark.asyncio
async def test_recency_hero_selects_the_in_window_winner() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/research/recency/hero",
            json={
                "from_ms": 100,
                "to_ms": 1_000,
                "candidates": [
                    {
                        "recency_run_id": 1,
                        "symbol": "SPY",
                        "strategy_key": "ema",
                        "params_hash": "low",
                        "trades": [{"entry_ms": 500, "pnl": -10.0}],
                    },
                    {
                        "recency_run_id": 2,
                        "symbol": "SPY",
                        "strategy_key": "ema",
                        "params_hash": "high",
                        "trades": [{"entry_ms": 500, "pnl": 25.0}],
                    },
                ],
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "heroes": [
            {
                "recency_run_id": 2,
                "symbol": "SPY",
                "strategy_key": "ema",
                "params_hash": "high",
                "total_pnl": 25.0,
            }
        ]
    }


@pytest.mark.asyncio
async def test_recency_hero_rejects_an_inverted_window() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/research/recency/hero",
            json={"from_ms": 1_000, "to_ms": 100, "candidates": []},
        )

    assert response.status_code == 422
