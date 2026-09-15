"""`/api/chart` numeric-window authority and the range-presets endpoint.

The chart request's ``start_ms_utc``/``end_ms_utc`` are declared additive-
first fields (data-lab workspace redesign PRD §12); these tests pin that the
router actually honors them — per-field precedence over the date strings,
floored to UTC calendar dates, with an inverted numeric window refused as
``INVALID_RANGE`` — and that ``GET /api/chart/range-presets`` is a thin,
session-parameterized transport over the calendar resolver.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.routers import chart as chart_router
from app.services import chart_service

# UTC-midnight anchors: 2026-08-01 and 2026-09-11.
AUG_1_MS = 1785561600000
SEP_11_MS = 1789161600000

_REQUEST: dict[str, Any] = {
    "ticker": "SPY",
    "from_date": "2025-11-26",
    "to_date": "2025-12-01",
    "timeframe": "1D",
    "session": "rth",
    "adjusted": False,
    "indicators": [],
}


@pytest.fixture
def api() -> FastAPI:
    app = FastAPI()
    app.include_router(chart_router.router, prefix="/api/chart")
    return app


_received_windows: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _stub_chart_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the fetch: the chart service receives the window and answers
    with the minimal payload the /data response model accepts, so the test
    observes only the router's date resolution. The received window is
    recorded for assertions. Patched on the router module — it imported the
    name."""
    chart_service._resample_cache.clear()
    chart_service._indicator_cache.clear()
    _received_windows.clear()

    def _stub(**kwargs: Any) -> dict[str, Any]:
        _received_windows.append(kwargs)
        return {
            "bars": [],
            "indicators": [],
            "quality": {
                "raw_bar_count": 0,
                "duplicates_removed": 0,
                "gaps_found": 0,
                "largest_gap_minutes": 0,
                "missing_sessions": 0,
                "session_coverage_pct": 0.0,
                "synthetic_bars": 0,
                "resampled_bar_count": 0,
                "gap_details": [],
                "missing_session_dates": [],
                "flat_bars_detected": 0,
                "ohlc_violations_detected": 0,
                "out_of_order_fixed": 0,
            },
            "allowed_timeframes": ["1D"],
            "estimated_bars_per_timeframe": {"1D": 1},
            "recommended_timeframe": "1D",
            "meta": {"cached_resample": False, "cached_indicators": False},
        }

    monkeypatch.setattr(chart_router, "get_chart_data", _stub)


async def _post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/api/chart/data", json=body)


@pytest.mark.asyncio
async def test_numeric_window_overrides_both_date_strings(api: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await _post(
            client,
            {**_REQUEST, "start_ms_utc": AUG_1_MS, "end_ms_utc": SEP_11_MS},
        )
    assert response.status_code == 200
    assert _received_windows[-1]["from_date"] == "2026-08-01"
    assert _received_windows[-1]["to_date"] == "2026-09-11"


@pytest.mark.asyncio
async def test_numeric_precedence_is_per_field(api: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await _post(client, {**_REQUEST, "end_ms_utc": SEP_11_MS})
    assert response.status_code == 200
    received = _received_windows[-1]
    assert received["from_date"] == _REQUEST["from_date"]
    assert received["to_date"] == "2026-09-11"


@pytest.mark.asyncio
async def test_inverted_numeric_window_is_invalid_range(api: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await _post(
            client,
            {**_REQUEST, "start_ms_utc": SEP_11_MS, "end_ms_utc": AUG_1_MS},
        )
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "INVALID_RANGE"


@pytest.mark.asyncio
async def test_date_strings_still_drive_the_window_without_ms_fields(api: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await _post(client, _REQUEST)
    assert response.status_code == 200
    received = _received_windows[-1]
    assert received["from_date"] == _REQUEST["from_date"]
    assert received["to_date"] == _REQUEST["to_date"]


# ──────────────────────────────────────────────
# GET /api/chart/range-presets
# ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_range_presets_endpoint_returns_the_resolver_output(api: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/chart/range-presets")
    assert response.status_code == 200
    body = response.json()
    assert [p["key"] for p in body["presets"]] == [key for key, _label, _count in chart_service.RANGE_PRESETS]
    for preset in body["presets"]:
        assert preset["session_count"] >= 1
        assert isinstance(preset["start_ms_utc"], int)
        assert isinstance(preset["end_ms_utc"], int)
        # Temporal wire values are ms-only: no date strings may appear on the
        # contract (AGENTS.md hard rule on ISO-free wire).
        assert "start_date" not in preset
        assert "end_date" not in preset
        # Same estimator /allowed-timeframes uses; a window of all full
        # sessions yields exactly session_count daily bars (rth), an
        # early-close half-day one fewer.
        assert 0 < preset["estimated_bars_per_timeframe"]["1D"] <= preset["session_count"]


@pytest.mark.asyncio
async def test_range_presets_endpoint_rejects_unknown_session_values(api: FastAPI) -> None:
    """`?session=typo` is a validation error, not a silent trip into the
    extended-hours estimate branch."""
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/chart/range-presets", params={"session": "regular"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_range_presets_endpoint_accepts_the_session_parameter(api: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/chart/range-presets", params={"session": "extended"})
    assert response.status_code == 200
    assert len(response.json()["presets"]) == len(chart_service.RANGE_PRESETS)


@pytest.mark.asyncio
async def test_range_presets_endpoint_maps_resolver_refusal_to_invalid_range(
    api: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(_now_ms: int, *, session: str = "rth") -> list[dict[str, Any]]:
        raise ValueError("calendar reaches only 3 sessions back")

    monkeypatch.setattr(chart_router, "resolve_range_presets", _refuse)
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get("/api/chart/range-presets")
    assert response.status_code == 400
    assert response.json()["detail"]["error_code"] == "INVALID_RANGE"
