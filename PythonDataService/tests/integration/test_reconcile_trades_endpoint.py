"""PR B (2026-05-19) Phase 4 — POST /api/lean-sidecar/reconcile-trades.

Contract tests for the trade-reconciliation endpoint that the .NET
``RunCompareService`` delegates to.  The endpoint wraps the canonical
``reconcile_trade_lists`` helper from ``lean_sidecar_compare_service`` and
returns the trade-diff shape spec § 6.5 calls for:
``matched_pairs``/``python_only``/``lean_only``/``first_divergence``.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_reconcile_trades_endpoint_returns_matched_pairs() -> None:
    """Two trades with a 0.02 exit-price drift land in ``matched_pairs``
    with category ``fill_price_drift`` (atol 0.01 < 0.02)."""
    from app.main import app

    left = [
        {
            "trade_number": 1,
            "entry_ms_utc": 1736773800000,
            "exit_ms_utc": 1736775000000,
            "quantity": "100",
            "entry_price": "100.00",
            "exit_price": "100.50",
            "pnl": "50.00",
        },
    ]
    right = [
        {
            "trade_number": 1,
            "entry_ms_utc": 1736773800000,
            "exit_ms_utc": 1736775000000,
            "quantity": "100",
            "entry_price": "100.00",
            "exit_price": "100.52",
            "pnl": "52.00",
        },
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/lean-sidecar/reconcile-trades",
            json={"left": left, "right": right, "fill_price_atol": "0.01"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "matched_pairs" in body
    assert "python_only" in body
    assert "lean_only" in body
    # 0.02 exit-price delta > 0.01 atol -> category fill_price_drift.
    assert len(body["matched_pairs"]) == 1
    pair = body["matched_pairs"][0]
    assert pair["category"] == "fill_price_drift"
    assert pair["trade_number"] == 1


@pytest.mark.asyncio
async def test_reconcile_trades_endpoint_empty_lists() -> None:
    """Empty input lists return all-empty containers, not nulls."""
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/lean-sidecar/reconcile-trades",
            json={"left": [], "right": [], "fill_price_atol": "0.01"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["matched_pairs"] == []
    assert body["python_only"] == []
    assert body["lean_only"] == []
    assert body["first_divergence"] is None


@pytest.mark.asyncio
async def test_reconcile_trades_endpoint_unmatched_one_side() -> None:
    """Trade present only on the left lands in ``python_only``."""
    from app.main import app

    left = [
        {
            "trade_number": 1,
            "entry_ms_utc": 1736773800000,
            "exit_ms_utc": 1736775000000,
            "quantity": "100",
            "entry_price": "100.00",
            "exit_price": "100.50",
            "pnl": "50.00",
        },
        {
            "trade_number": 2,
            "entry_ms_utc": 1736776800000,
            "exit_ms_utc": 1736778000000,
            "quantity": "100",
            "entry_price": "100.50",
            "exit_price": "101.00",
            "pnl": "50.00",
        },
    ]
    right = [
        {
            "trade_number": 1,
            "entry_ms_utc": 1736773800000,
            "exit_ms_utc": 1736775000000,
            "quantity": "100",
            "entry_price": "100.00",
            "exit_price": "100.50",
            "pnl": "50.00",
        },
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/lean-sidecar/reconcile-trades",
            json={"left": left, "right": right, "fill_price_atol": "0.01"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["matched_pairs"]) == 1
    assert len(body["python_only"]) == 1
    assert body["python_only"][0]["trade_number"] == 2
    assert body["lean_only"] == []


@pytest.mark.asyncio
async def test_reconcile_trades_endpoint_populates_first_divergence() -> None:
    """``first_divergence`` points at the earliest non-matched pair."""
    from app.main import app

    left = [
        {
            "trade_number": 1,
            "entry_ms_utc": 1736773800000,
            "exit_ms_utc": 1736775000000,
            "quantity": "100",
            "entry_price": "100.00",
            "exit_price": "100.50",
            "pnl": "50.00",
        },
        {
            "trade_number": 2,
            "entry_ms_utc": 1736776800000,
            "exit_ms_utc": 1736778000000,
            "quantity": "100",
            "entry_price": "100.50",
            "exit_price": "101.00",
            "pnl": "50.00",
        },
    ]
    right = [
        {
            "trade_number": 1,
            "entry_ms_utc": 1736773800000,
            "exit_ms_utc": 1736775000000,
            "quantity": "100",
            "entry_price": "100.00",
            "exit_price": "100.50",
            "pnl": "50.00",
        },
        {
            "trade_number": 2,
            "entry_ms_utc": 1736776800000,
            "exit_ms_utc": 1736778000000,
            "quantity": "100",
            "entry_price": "100.50",
            "exit_price": "101.05",
            "pnl": "55.00",
        },
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/lean-sidecar/reconcile-trades",
            json={"left": left, "right": right, "fill_price_atol": "0.01"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["first_divergence"] is not None
    assert body["first_divergence"]["trade_index"] == 1
    assert body["first_divergence"]["category"] == "fill_price_drift"
