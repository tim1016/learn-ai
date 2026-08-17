from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from httpx import AsyncClient

from app.main import app
from app.services.chart_indicator_service import CHART_INDICATOR_NAMES, get_chart_indicator_service


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
    body = response.json()
    assert body["symbol"] == "SPY"
    assert len(body["indicators"]) == 1
    indicator = body["indicators"][0]
    assert {key: value for key, value in indicator.items() if key != "data"} == {
        "id": "sma_2",
        "panel": "main",
        "type": "line",
        "color": "#FF9800",
        "refs": [],
        "default_visible": None,
    }
    assert indicator["data"][0]["t"] == 1_700_000_120_000
    np.testing.assert_allclose(
        [indicator["data"][0]["value"]],
        [101.0],
        atol=1e-9,
        rtol=0,
    )
    assert service.request == (
        "SPY",
        request["bars"],
        request["indicators"],
    )


@pytest.mark.anyio
async def test_chart_indicators_rejects_an_unknown_indicator(client: AsyncClient) -> None:
    response = await client.post(
        "/api/chart/indicators",
        json={
            "symbol": "SPY",
            "bars": [
                {"t": 1_700_000_060_000, "o": 99, "h": 101, "l": 98, "c": 100, "v": 10},
            ],
            "indicators": [{"name": "not_real", "params": {}}],
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "unknown chart indicator: not_real"}


@pytest.mark.anyio
@pytest.mark.parametrize("parameter", [True, False])
async def test_chart_indicators_rejects_boolean_numeric_parameters(
    client: AsyncClient,
    parameter: bool,
) -> None:
    response = await client.post(
        "/api/chart/indicators",
        json={
            "symbol": "SPY",
            "bars": [
                {"t": 1_700_000_060_000, "o": 99, "h": 101, "l": 98, "c": 100, "v": 10},
            ],
            "indicators": [{"name": "sma", "params": {"length": parameter}}],
        },
    )

    assert response.status_code == 422
    assert "indicator parameter length must be a finite number" in response.text


@pytest.mark.anyio
async def test_chart_indicators_rejects_non_finite_numeric_parameters(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/chart/indicators",
        content=(
            b'{"symbol":"SPY","bars":[{"t":1700000060000,"o":99,"h":101,'
            b'"l":98,"c":100,"v":10}],"indicators":[{"name":"bbands",'
            b'"params":{"std":NaN}}]}'
        ),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "indicator parameter std must be a finite number" in response.text


@pytest.mark.anyio
async def test_chart_indicators_rejects_oversized_integer_parameters_without_500(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/chart/indicators",
        json={
            "symbol": "SPY",
            "bars": [
                {"t": 1_700_000_060_000, "o": 99, "h": 101, "l": 98, "c": 100, "v": 10},
            ],
            "indicators": [{"name": "sma", "params": {"length": 10**1_000}}],
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "parameter sma.length must be between 1 and 500"}


@pytest.mark.anyio
async def test_supported_chart_indicators_returns_only_proven_catalog_entries(
    client: AsyncClient,
) -> None:
    response = await client.get("/api/chart/indicators/supported")

    assert response.status_code == 200
    assert response.json() == {"names": sorted(CHART_INDICATOR_NAMES)}
    assert "ha" not in response.json()["names"]
