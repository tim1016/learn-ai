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
from scripts.benchmark_options_archive import _measure, _preflight, main
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


@pytest.mark.parametrize("cache", ["reused", "zero_hits", "missing_counters"])
async def test_harness_requires_observed_warm_reuse_and_allows_long_archive_preflight(cache: str) -> None:
    case = benchmark_cases()[0]
    preflight_timeouts = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/benchmark-preflight"):
            preflight_timeouts.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"case_digest": case.digest, "inventory_digest": "d" * 64,
                                           "sessions": 480, "incomplete_session_ms": []})
        if path.endswith("/benchmark-resources"):
            return httpx.Response(200, json={"observed_at_ms": 1_000_000, "runtime_instance": "runtime-1",
                "worker_width": 1, "cgroup_memory_bytes": 1024, "cgroup_memory_limit_bytes": 16 * 1024**3,
                "cpu_quota_us": 800_000, "cpu_period_us": 100_000, "frozen_input_bytes": 512,
                "database_connections": 4, "database_connection_limit": 10})
        if path.endswith("/benchmark-result/1"):
            metadata = {"worker_width": 1, "frozen_input_digest": "a" * 64,
                        "execution_identity_digest": "c" * 64, "cache_epoch": "",
                        "cache_bytes_peak": 512}
            if cache != "missing_counters":
                metadata.update(cache_hits=480 if cache == "reused" else 0, cache_misses=480)
            return httpx.Response(200, json={"run_id": 1, "accepted_at_ms": 1_000_000,
                "saved_at_ms": 1_062_000, "result_digest": "b" * 64,
                "phase_timings": [], "execution_metadata": metadata})
        if request.method == "POST" and path.endswith("/options-backtest"):
            return httpx.Response(200, json={"run_id": 1})
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        raise AssertionError(f"unexpected benchmark request: {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=120) as transport:
        args = dict(api="http://api", runtime="http://runtime", operational="http://api/healthz",
                    case=case, mode="warm", qualified_widths=[1], timeout_seconds=901)
        if cache == "reused":
            result = await _measure(transport, **args)
            assert result.cache_hits == result.cache_misses == 480
        else:
            with pytest.raises(ValueError, match=r"warm.*reuse|required.*measurement"):
                await _measure(transport, **args)
    assert preflight_timeouts == [901, 901]
