"""PRD #684 PR 1 — data-plane code-liveness health contract."""

from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from app.main import app


async def test_data_plane_health_exposes_revision_process_start_and_reload_mode() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/broker/data-plane/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "polygon-data-service"
    assert re.fullmatch(r"[0-9a-f]{7,40}", body["code_revision"])
    assert isinstance(body["process_start_ms"], int)
    assert body["process_start_ms"] > 0
    assert isinstance(body["fetched_at_ms"], int)
    assert body["fetched_at_ms"] >= body["process_start_ms"]
    assert body["reload"] in {
        "disabled",
        "watchfiles",
        "watchfiles-polling",
        "unknown",
    }
