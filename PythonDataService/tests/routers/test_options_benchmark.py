"""The benchmark refuses missing archive inputs before dispatching work."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.options_backtest.benchmark import benchmark_cases
from app.routers.options_backtest_benchmark import router
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.error_handlers import install_fleet_control_error_handler
from scripts.benchmark_options_archive import _preflight, main
from tests._helpers.fake_lake_catalog import point_lake_writer_at_tmp


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    monkeypatch.setenv("RESEARCH_RUNTIME", "0")
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "private-benchmark-test-secret")
    point_lake_writer_at_tmp(tmp_path, monkeypatch)
    app = FastAPI()
    install_fleet_control_error_handler(app)
    app.include_router(router, prefix="/api/jobs-internal")
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                            headers={CONTROL_SECRET_HEADER: "private-benchmark-test-secret"})


async def test_preflight_lists_missing_archive_and_harness_refuses(client: httpx.AsyncClient) -> None:
    case = benchmark_cases()[0]
    response = await client.post("/api/jobs-internal/options-backtest/benchmark-preflight", json={"case": case.name})
    assert response.status_code == 200
    assert response.json()["sessions"] == 0
    assert response.json()["incomplete_session_ms"] == list(case.session_open_ms)
    with pytest.raises(ValueError, match="no benchmark job was started"):
        await _preflight(client, "http://test", case)


async def test_resource_endpoint_requires_secret_and_runtime_role(client: httpx.AsyncClient) -> None:
    path = "/api/jobs-internal/options-backtest/benchmark-resources"
    assert (await client.get(path)).status_code == 503
    assert (await client.get(path, headers={CONTROL_SECRET_HEADER: "incorrect"})).status_code == 403


def test_cases_command_exports_exact_hash_bound_choices(tmp_path: Path) -> None:
    output = tmp_path / "cases.json"
    assert main(["cases", "--output", str(output)]) == 0
    cases = json.loads(output.read_bytes())
    assert [row["case_digest"] for row in cases] == [case.digest for case in benchmark_cases()]
    assert all(len(row["case"]["session_open_ms"]) == 480 for row in cases)
