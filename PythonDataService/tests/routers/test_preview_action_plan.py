"""Slice 1D — ``POST /api/live-instances/preview-action-plan``.

Stateless, side-effect-free endpoint that returns parity warnings on a
candidate plan. Pydantic rejects malformed plans (422); semantically
valid plans return 200 with whatever ``parity_diagnostics`` produced.
Same response code regardless of warning count — the UI may surface
warnings but submit stays enabled (operator-override path).

Prior art: tests/routers/test_live_runs.py.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.routers import live_instances


@pytest.fixture
def app_with_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "live_runs"
    root.mkdir()
    stub = SimpleNamespace(
        live_runs_root=str(root),
        live_runner_daemon_url="http://daemon",
        live_runner_host_start_command="",
        fleet_dirty_blocks_starts=False,
        mode="paper",
        readonly=False,
    )
    monkeypatch.setattr(live_instances, "get_settings", lambda: stub)
    from app.main import app

    return app


_STOCK_LEG: dict = {
    "leg_id": "spy_long",
    "instrument": {"kind": "stock", "underlying": "SPY"},
    "position": "long",
    "qty_ratio": 1,
}


async def test_preview_empty_plan_returns_200_with_no_warnings(app_with_root) -> None:
    async with AsyncClient(transport=ASGITransport(app=app_with_root), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/preview-action-plan",
            json={"on_enter": [], "on_exit": []},
        )

    assert response.status_code == 200
    assert response.json() == {"warnings": []}


async def test_preview_orphan_entry_returns_200_with_warning(app_with_root) -> None:
    async with AsyncClient(transport=ASGITransport(app=app_with_root), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/preview-action-plan",
            json={"on_enter": [_STOCK_LEG], "on_exit": []},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["warnings"]) == 1
    assert body["warnings"][0]["code"] == "orphan_entry"
    assert body["warnings"][0]["leg_id"] == "spy_long"


async def test_preview_malformed_plan_returns_422(app_with_root) -> None:
    """Schema errors (missing ``instrument.underlying``) are NOT warnings;
    Pydantic rejects them at this same boundary with 422."""

    bad_leg = {
        "leg_id": "spy_long",
        "instrument": {"kind": "stock"},  # underlying missing
        "position": "long",
        "qty_ratio": 1,
    }
    async with AsyncClient(transport=ASGITransport(app=app_with_root), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/preview-action-plan",
            json={"on_enter": [bad_leg], "on_exit": []},
        )

    assert response.status_code == 422


async def test_preview_orphan_close_leg_returns_422_not_warning(app_with_root) -> None:
    """A ``close_leg`` referencing a non-existent ``entry_leg_id`` is a
    Pydantic-rejected schema error per ADR 0012 §"Architectural
    decisions" — NOT a parity warning. Pinned so a refactor doesn't
    relax the schema boundary."""

    async with AsyncClient(transport=ASGITransport(app=app_with_root), base_url="http://test") as client:
        response = await client.post(
            "/api/live-instances/preview-action-plan",
            json={
                "on_enter": [],
                "on_exit": [{"kind": "close_leg", "entry_leg_id": "ghost"}],
            },
        )

    assert response.status_code == 422


async def test_preview_is_stateless_across_calls(app_with_root) -> None:
    """Two preview calls with the same plan from different transports
    must return identical bodies — the endpoint must NOT consult
    ``live_config.symbol``, instance roster, or any other session
    context (PRD #593 §"API contracts" + ADR 0012 §"Architectural
    decisions")."""

    payload: dict = {"on_enter": [_STOCK_LEG], "on_exit": []}
    async with AsyncClient(transport=ASGITransport(app=app_with_root), base_url="http://test") as client_a:
        first = await client_a.post("/api/live-instances/preview-action-plan", json=payload)
    async with AsyncClient(transport=ASGITransport(app=app_with_root), base_url="http://test") as client_b:
        second = await client_b.post("/api/live-instances/preview-action-plan", json=payload)

    assert first.json() == second.json()
