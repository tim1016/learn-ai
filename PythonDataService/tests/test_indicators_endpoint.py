"""Smoke tests for /indicators endpoints."""

from __future__ import annotations

import pytest


def _bar(ts_ms: int, open_: float = 100.0, close: float | None = None) -> dict:
    close_val = close if close is not None else open_ + 0.5
    return {
        "timestamp": ts_ms,
        "open": open_,
        "high": open_ + 1.0,
        "low": open_ - 1.0,
        "close": close_val,
        "volume": 1_000_000,
    }


def _synthetic_bars(count: int) -> list[dict]:
    # 1-minute bars starting at 2024-01-01 00:00 UTC; int64 ms per numerical-rigor.md.
    base_ts_ms = 1_704_067_200_000
    return [_bar(base_ts_ms + i * 60_000, open_=100.0 + 0.1 * i) for i in range(count)]


@pytest.mark.anyio
async def test_calculate_indicators_rejects_unknown_indicator(client):
    response = await client.post(
        "/api/indicators/calculate",
        json={
            "ticker": "SPY",
            "bars": _synthetic_bars(30),
            "indicators": [{"name": "not_a_real_indicator", "window": 14}],
        },
    )

    # Pydantic validation error bubbles up as 422.
    assert response.status_code == 422


@pytest.mark.anyio
async def test_calculate_indicators_rejects_empty_bars(client):
    response = await client.post(
        "/api/indicators/calculate",
        json={
            "ticker": "SPY",
            "bars": [],
            "indicators": [{"name": "sma", "window": 14}],
        },
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_calculate_indicators_returns_success_shape(client):
    response = await client.post(
        "/api/indicators/calculate",
        json={
            "ticker": "SPY",
            "bars": _synthetic_bars(30),
            "indicators": [{"name": "sma", "window": 10}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["ticker"] == "SPY"
    assert isinstance(body["indicators"], list | dict)
