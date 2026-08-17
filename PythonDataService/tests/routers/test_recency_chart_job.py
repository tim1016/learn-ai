"""POST /api/jobs-internal/recency-chart — the thin job-entry wrapper.

The actual grid execution, statistics, fingerprinting, and persistence are
covered exhaustively at their own seams (tests/research/recency/). This
file only proves the HTTP boundary: request validation and the eager,
pre-dispatch grid-size rejection (D11) — a malformed sweep must never even
reach a worker thread.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.routers.jobs import _ms_to_date_str


def test_ms_to_date_str_resolves_the_et_calendar_date_not_utc() -> None:
    """Window bounds feed EngineBacktestRequest.from_date/to_date, an ET-anchored
    trading date (.claude/rules/temporal-rigor.md) — must not drift a day off UTC.
    """
    # 2026-06-11 02:30 UTC is 2026-06-10 22:30 EDT (UTC-4): the ET calendar
    # date trails the UTC one across this boundary.
    ms = int(datetime(2026, 6, 11, 2, 30, tzinfo=UTC).timestamp() * 1000)
    assert _ms_to_date_str(ms) == "2026-06-10"


@pytest.mark.asyncio
async def test_rejects_a_grid_past_the_sanity_ceiling_before_queuing() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-1",
                "strategies": [
                    {
                        "strategyKey": "ema_crossover_2_bps",
                        "paramRanges": {
                            "gapBps": {"type": "low_high_step", "low": 0.0, "high": 10_000_000.0, "step": 0.0001}
                        },
                    }
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 400
    assert "sanity ceiling" in response.json()["detail"]


@pytest.mark.asyncio
async def test_rejects_an_inverted_low_high_range() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-2",
                "strategies": [
                    {
                        "strategyKey": "ema_crossover_2_bps",
                        "paramRanges": {"gapBps": {"type": "low_high_step", "low": 5.0, "high": 1.0, "step": 1.0}},
                    }
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_rejects_empty_symbols() -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/jobs-internal/recency-chart",
            json={
                "jobId": "job-3",
                "strategies": [
                    {"strategyKey": "ema_crossover_2_bps", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": [],
                "windowStartMs": 0,
                "windowEndMs": 1,
            },
        )
    assert response.status_code == 422  # Pydantic min_length violation
