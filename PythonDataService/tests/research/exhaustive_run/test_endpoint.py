"""Async HTTP contract tests for persisted Exhaustive Run evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.research.exhaustive_run.result import ExhaustiveRunConfig, ExhaustiveRunResult
from app.research.exhaustive_run.storage import save_exhaustive_run
from app.routers.research_runs import get_artifacts_root


def _artifact(identifier: str, source: str) -> tuple[ExhaustiveRunConfig, ExhaustiveRunResult]:
    config = ExhaustiveRunConfig(
        exhaustive_run_id=identifier,
        source_walk_forward_id=source,
        source_protocol_id="spy-ema-normalized-gap",
        source_protocol_version="1.0",
        start_ms=1,
        recent_window_start_ms=2,
        end_ms=3,
        initial_cash=100_000.0,
        fill_mode="next_bar_open",
        commission_per_order=0.0,
        slippage_per_share=0.0,
        random_seed=0,
        data_root_revision="revision",
        created_at_ms=10,
    )
    result = ExhaustiveRunResult(
        exhaustive_run_id=identifier,
        source_walk_forward_id=source,
        selections=[],
        candidates=[],
        status="failed",
        failure_reason="fixture",
        created_at_ms=10,
        completed_at_ms=11,
    )
    return config, result


@pytest.fixture
async def client(tmp_path: Path):
    app.dependency_overrides[get_artifacts_root] = lambda: tmp_path
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http:
            yield http
    finally:
        app.dependency_overrides.clear()


async def test_gets_artifact_by_id_and_source_walk_forward(client: AsyncClient, tmp_path: Path) -> None:
    identifier = "a" * 32
    source = "b" * 32
    save_exhaustive_run(*_artifact(identifier, source), root=tmp_path)

    by_id = await client.get(f"/api/research/exhaustive-runs/{identifier}")
    by_source = await client.get(
        f"/api/research/exhaustive-runs/by-walk-forward/{source}"
    )

    assert by_id.status_code == 200
    assert by_source.status_code == 200
    assert by_id.json()["config"]["exhaustive_run_id"] == identifier
    assert by_source.json()["result"]["source_walk_forward_id"] == source


async def test_endpoint_validates_ids_and_returns_not_found(client: AsyncClient) -> None:
    invalid = await client.get("/api/research/exhaustive-runs/not-an-id")
    missing = await client.get("/api/research/exhaustive-runs/" + "c" * 32)

    assert invalid.status_code == 422
    assert missing.status_code == 404
