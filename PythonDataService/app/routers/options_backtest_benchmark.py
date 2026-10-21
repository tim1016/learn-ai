"""Authenticated benchmark observations from the runtime parent (#3433)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.options_backtest import benchmark_runtime
from app.options_backtest.benchmark import benchmark_cases
from app.schemas.options_benchmark import (
    BenchmarkPreflightRequest,
    BenchmarkPreflightResponse,
    BenchmarkResourcesResponse,
    BenchmarkResultResponse,
)
from app.security.data_plane_control import require_data_plane_control_secret_always
from app.services.research_runtime_client import is_research_runtime

router = APIRouter(dependencies=[Depends(require_data_plane_control_secret_always)])


def _require_runtime() -> None:
    if not is_research_runtime():
        raise HTTPException(503, "Benchmark observations belong to the research runtime.")


@router.get("/options-backtest/benchmark-resources", response_model=BenchmarkResourcesResponse)
async def benchmark_resources() -> BenchmarkResourcesResponse:
    _require_runtime()
    return await benchmark_runtime.resources()


@router.get("/options-backtest/benchmark-result/{run_id}", response_model=BenchmarkResultResponse)
async def benchmark_result(run_id: Annotated[int, Path(ge=1)]) -> BenchmarkResultResponse:
    _require_runtime()
    result = await benchmark_runtime.measured_result(run_id)
    if result is None:
        raise HTTPException(409, "The run has no complete saved result and measurement receipt yet.")
    return result


@router.post("/options-backtest/benchmark-preflight", response_model=BenchmarkPreflightResponse)
async def benchmark_preflight(body: BenchmarkPreflightRequest) -> BenchmarkPreflightResponse:
    if is_research_runtime():
        raise HTTPException(503, "Archive admission belongs to the data service.")
    case = next(case for case in benchmark_cases() if case.name == body.case)
    return await asyncio.to_thread(benchmark_runtime.archived_inventory, case)
