from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.main import app
from app.services.chart_indicator_service import get_chart_indicator_service


class _FakeChartIndicatorService:
    def __init__(self) -> None:
        self.request: tuple[str, list[dict[str, int | float]], list[dict[str, Any]]] | None = None

    def compute(
        self,
        symbol: str,
        bars: list[dict[str, int | float]],
        indicators: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        self.request = (symbol, bars, indicators)
        return "SPY", [
            {
                "id": "sma_2",
                "panel": "main",
                "type": "line",
                "color": "#FF9800",
                "data": [{"t": bars[-1]["t"], "value": 101.0}],
                "refs": [],
            }
        ]


@pytest.mark.anyio
async def test_chart_indicators_returns_typed_series_from_exact_caller_bars(
    client: AsyncClient,
) -> None:
    service = _FakeChartIndicatorService()
    app.dependency_overrides[get_chart_indicator_service] = lambda: service
    request = {
        "symbol": "SPY",
        "bars": [
            {"t": 1_700_000_060_000, "o": 99, "h": 101, "l": 98, "c": 100, "v": 10},
            {"t": 1_700_000_120_000, "o": 100, "h": 103, "l": 99, "c": 102, "v": 11},
        ],
        "indicators": [{"name": "sma", "params": {"length": 2}}],
    }

    try:
        response = await client.post("/api/chart/indicators", json=request)
    finally:
        app.dependency_overrides.pop(get_chart_indicator_service, None)

    assert response.status_code == 200
    assert response.json() == {
        "symbol": "SPY",
        "indicators": [
            {
                "id": "sma_2",
                "panel": "main",
                "type": "line",
                "color": "#FF9800",
                "data": [{"t": 1_700_000_120_000, "value": 101.0}],
                "refs": [],
                "default_visible": None,
            }
        ],
    }
    assert service.request == (
        "SPY",
        request["bars"],
        request["indicators"],
    )
