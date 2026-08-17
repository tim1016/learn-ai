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


class TestValidateBeforeDispatch:
    """A direct or stale client can submit a request that only passes range
    arithmetic (D11) but is otherwise guaranteed to fail every child
    backtest. These must be rejected before a durable launch is created,
    not discovered N-runs-deep into a dispatched grid."""

    async def _post(self, body: dict) -> httpx.Response:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return await client.post("/api/jobs-internal/recency-chart", json=body)

    @pytest.mark.asyncio
    async def test_preflight_returns_expected_run_count_without_queuing(self) -> None:
        body = {
            "jobId": "job-preflight",
            "strategies": [
                {
                    "strategyKey": "ema_crossover_2_bps",
                    "paramRanges": {"gap_bps": {"type": "value_list", "values": [1.0, 2.0]}},
                }
            ],
            "symbols": ["SPY", "QQQ"],
            "windowStartMs": 0,
            "windowEndMs": 1,
        }
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/jobs-internal/recency-chart/validate", json=body)

        assert response.status_code == 200
        assert response.json() == {"expected_runs": 4}

    @pytest.mark.asyncio
    async def test_rejects_an_unknown_strategy_key(self) -> None:
        response = await self._post(
            {
                "jobId": "job-unknown-strategy",
                "strategies": [
                    {"strategyKey": "not_a_real_strategy", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            }
        )
        assert response.status_code == 400
        assert "unknown strategy_key" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_an_out_of_bounds_parameter_value(self) -> None:
        response = await self._post(
            {
                "jobId": "job-oob-param",
                "strategies": [
                    {
                        "strategyKey": "ema_crossover_2_bps",
                        "paramRanges": {"gapBps": {"type": "value_list", "values": [150.0]}},  # le=100.0
                    }
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
            }
        )
        assert response.status_code == 400
        assert "parameters invalid" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_an_unsupported_data_policy_label(self) -> None:
        response = await self._post(
            {
                "jobId": "job-bad-policy",
                "strategies": [
                    {"strategyKey": "ema_crossover_2_bps", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": ["SPY"],
                "windowStartMs": 0,
                "windowEndMs": 1,
                "dataPolicy": "ibkr-raw-regular-minute",
            }
        )
        assert response.status_code == 400
        assert "data_policy" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_rejects_a_window_start_not_before_window_end(self) -> None:
        response = await self._post(
            {
                "jobId": "job-bad-window",
                "strategies": [
                    {"strategyKey": "ema_crossover_2_bps", "paramRanges": {"gapBps": {"type": "value_list", "values": [2.0]}}}
                ],
                "symbols": ["SPY"],
                "windowStartMs": 1000,
                "windowEndMs": 1000,
            }
        )
        assert response.status_code == 400
        assert "window_start_ms" in response.json()["detail"]
