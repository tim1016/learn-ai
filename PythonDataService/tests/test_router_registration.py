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
