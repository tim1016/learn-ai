"""POST /api/lean-sidecar/compare endpoint tests."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


def _trade(
    n: int,
    entry_ms: int,
    exit_ms: int,
    entry: float,
    exit_price: float,
    qty: float = 10,
) -> dict:
    return {
        "trade_number": n,
        "entry_ms_utc": entry_ms,
        "exit_ms_utc": exit_ms,
        "entry_price": entry,
        "exit_price": exit_price,
        "quantity": qty,
        "pnl": (exit_price - entry) * qty,
        "signal_reason": "test",
        "is_synthetic_exit": False,
    }


@pytest.mark.asyncio
async def test_compare_endpoint_returns_no_divergences_for_identical_trades() -> None:
    trades = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, 101.0, 102.0),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/compare",
            json={"left_trades": trades, "right_trades": trades},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["divergences"] == []
    assert body["first_divergence_ms_utc"] is None


@pytest.mark.asyncio
async def test_compare_endpoint_returns_divergences_for_mismatch() -> None:
    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0)]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.10, 101.00)]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/compare",
            json={"left_trades": left, "right_trades": right, "fill_price_atol": 0.01},
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["divergences"]) >= 1
    assert any(d["category"] == "FILL_PRICE_DRIFT" for d in body["divergences"])
    assert body["first_divergence_ms_utc"] is not None


@pytest.mark.asyncio
async def test_compare_endpoint_accepts_empty_lists() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/compare",
            json={"left_trades": [], "right_trades": []},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["divergences"] == []
    assert body["first_divergence_ms_utc"] is None


@pytest.mark.asyncio
async def test_compare_endpoint_validates_missing_fields() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/compare",
            json={"left_trades": [{"trade_number": 1}], "right_trades": []},  # missing fields
        )
    # Pydantic should reject this with 422.
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_compare_endpoint_default_tolerance() -> None:
    """Omitting fill_price_atol should default to 0.01."""
    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0)]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.005, 101.0)]  # within 0.01
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/lean-sidecar/compare",
            json={"left_trades": left, "right_trades": right},
        )
    body = response.json()
    assert all(d["category"] != "FILL_PRICE_DRIFT" for d in body["divergences"])
