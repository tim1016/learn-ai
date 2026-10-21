"""Read-only runtime-parent measurements; workers never acquire a DB handle (#3433)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg

from app.config import settings
from app.options_backtest import saved_runs
from app.options_backtest.benchmark import BenchmarkCase
from app.options_backtest.lake_data import LakeRunData
from app.options_bots.golden_proof import result_sha256
from app.research.golden_search.shared_pool import SharedWorkerPool
from app.research.persistence import ownership
from app.research.persistence.db import with_connection, writer_loop_call
from app.research.runs.hashing import hash_payload
from app.schemas.options_benchmark import (
    BenchmarkPreflightResponse,
    BenchmarkResourcesResponse,
    BenchmarkResultResponse,
)
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc


def _input_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            if path.is_file():
                total += path.stat().st_size
        except FileNotFoundError:
            # A completed job can release its own staging during sampling.
            continue
    return total


async def _database_capacity(connection: asyncpg.Connection) -> tuple[int, int]:
    row = await connection.fetchrow("SELECT (SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend') AS used, "
                                    "current_setting('max_connections')::int AS maximum")
    return int(row["used"]), int(row["maximum"])


async def resources() -> BenchmarkResourcesResponse:
    authority = ownership.process_authority()
    if authority is None or not authority.connected:
        raise RuntimeError("benchmark sampling needs the active research runtime")
    used, maximum = await writer_loop_call(with_connection(_database_capacity))
    cpu = SharedWorkerPool._cgroup_file("cpu.max")
    quota = period = None
    if cpu is not None:
        pieces = cpu.split()
        if len(pieces) == 2 and pieces[0].isdigit() and pieces[1].isdigit():
            quota, period = int(pieces[0]), int(pieces[1])
    return BenchmarkResourcesResponse(
        observed_at_ms=now_ms_utc(), runtime_instance=f"{authority.instance_id}:{authority.boot_generation}",
        worker_width=settings.OPTIONS_SEARCH_WORKER_COUNT,
        cgroup_memory_bytes=SharedWorkerPool._cgroup_bytes("memory.current"),
        cgroup_memory_limit_bytes=SharedWorkerPool._cgroup_bytes("memory.max"),
        cpu_quota_us=quota, cpu_period_us=period,
        frozen_input_bytes=await asyncio.to_thread(_input_bytes, settings.OPTIONS_BACKTEST_INPUT_ROOT),
        database_connections=used, database_connection_limit=maximum,
    )


async def _result(connection: asyncpg.Connection, run_id: int) -> BenchmarkResultResponse | None:
    stored = await saved_runs.get_run(connection, run_id, is_live=saved_runs.not_asked)
    if stored is None or stored.record.status != "complete" or stored.record.finished_at_ms is None:
        return None
    if stored.execution_metadata is None or not stored.phase_timings:
        return None
    saves = [span for span in stored.phase_timings if span.scope == "run" and span.phase == "save"]
    if len(saves) != 1:
        return None
    saved = saves[0]
    if not (stored.record.created_at_ms <= saved.started_at_ms <= stored.record.finished_at_ms <= saved.finished_at_ms):
        return None
    result = await saved_runs.read_result(connection, run_id)
    if result is None:
        return None
    return BenchmarkResultResponse(
        run_id=run_id, accepted_at_ms=stored.record.created_at_ms, saved_at_ms=saved.finished_at_ms,
        result_digest=result_sha256(result), phase_timings=list(stored.phase_timings),
        execution_metadata=stored.execution_metadata,
    )


async def measured_result(run_id: int) -> BenchmarkResultResponse | None:
    return await writer_loop_call(with_connection(_result, run_id))


def archived_inventory(case: BenchmarkCase) -> BenchmarkPreflightResponse:
    """Admit complete archived days before the benchmark can start any job."""
    from app.data_lake.option_session import read_option_session

    pins = []
    incomplete = []
    data = LakeRunData(case.setup.underlying)
    for session_ms in case.session_open_ms:
        day = et_date_at_ms(session_ms)
        session = read_option_session(case.setup.underlying, day)
        if session is None or not session.complete:
            incomplete.append(session_ms)
            continue
        underlying = data.underlying_day(day)
        if underlying is None:
            incomplete.append(session_ms)
            continue
        pins.append({"session_ms": session_ms, "container": session.sha256,
                     "listing": session.listing_sha256, "underlying": underlying.sha256})
    return BenchmarkPreflightResponse(case_digest=case.digest, inventory_digest=hash_payload(pins),
                                     sessions=len(pins), incomplete_session_ms=incomplete)
