"""Regression tests for router registration in app/main.py.

Background: commit 88b48ac (2026-04-12, "feat: Add implied volatility
surface module") rewrote main.py and silently dropped the
`quantlib_options` router registration. Four endpoints
(`/api/quantlib/{status,price,strategy,compare}`) returned 404 in
production until the pricing-lab UI surfaced it ~3 weeks later.

These tests guard against the same shape of bug recurring on any
router whose registration disappears during a refactor.
"""

import pytest


@pytest.mark.anyio
async def test_quantlib_router_is_mounted(client):
    """Hit a no-body endpoint on the quantlib router and assert it's
    not 404. /status is the cheapest probe — it returns 200 whether
    QuantLib is installed or not (the body just reports availability).
    """
    response = await client.get("/api/quantlib/status")
    assert response.status_code != 404, (
        "GET /api/quantlib/status returned 404 — quantlib_options router "
        "is missing from app/main.py. See commit 88b48ac for the original "
        "regression."
    )
    assert response.status_code == 200
    body = response.json()
    assert "available" in body
    assert "engines" in body


@pytest.mark.anyio
async def test_return_distribution_router_is_mounted(client):
    """A typed NOT_CAPTURED answer proves the route exists; a missing
    registration would surface as FastAPI's plain {"detail": "Not Found"}
    404 instead (same class of bug as the quantlib probe above)."""
    response = await client.post(
        "/api/research/return-distribution",
        json={"symbol": "SPY", "from_date": "2024-07-01", "to_date": "2024-08-01"},
    )
    body = response.json()
    if response.status_code == 404:
        detail = body["detail"]
        assert isinstance(detail, dict) and detail.get("error_code") == "NOT_CAPTURED", (
            "POST /api/research/return-distribution returned an untyped 404 — "
            "return_distribution router is missing from app/main.py"
        )
    else:
        assert response.status_code == 200, body
