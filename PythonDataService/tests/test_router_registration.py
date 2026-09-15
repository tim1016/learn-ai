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
async def test_return_distribution_router_is_mounted(client, monkeypatch):
    """Any typed answer proves the route exists; a missing registration
    would surface as FastAPI's plain {"detail": "Not Found"} 404 instead
    (same class of bug as the quantlib probe above). The business outcome
    (200 / 400 INSUFFICIENT_COVERAGE / 404 NOT_CAPTURED) depends on lake
    contents and belongs to tests/routers/test_return_distribution_endpoint.py;
    the capture boundary is stubbed so the probe never reaches the provider."""
    from app.services import return_distribution_service

    async def _stub_capture(**kwargs: object) -> return_distribution_service.CaptureReceipt:
        return return_distribution_service.CaptureReceipt(
            status="skipped", fetched_artifact_count=0, detail="stubbed for registration probe"
        )

    monkeypatch.setattr(return_distribution_service, "_capture_missing_sessions", _stub_capture)
    response = await client.post(
        "/api/research/return-distribution",
        json={"symbol": "SPY", "from_ms_utc": 1719792000000, "to_ms_utc": 1722470399999},
    )
    body = response.json()
    # An unmatched route always answers FastAPI's plain {"detail": "Not Found"}.
    assert body != {"detail": "Not Found"}, (
        "POST /api/research/return-distribution returned an untyped 404 — "
        "return_distribution router is missing from app/main.py"
    )
    assert response.status_code in (200, 400, 404), body
