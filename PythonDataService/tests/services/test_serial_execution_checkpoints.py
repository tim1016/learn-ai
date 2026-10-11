"""A serial engine run's checkpoints: real boundaries, unchanged results (#3034).

The pooled research mode's lease contract (:mod:`app.jobs.checkpoints`) is
proven here against the serial execution path Golden Search drives. The
workflow's ``on_phase`` calls fire at the run's real boundaries —
preparation (``consolidating_bars``), simulation (``running_indicators``),
aggregation and persistence — so they feed a :class:`CheckpointGate` as
transition checkpoints and renew the worker's lease once each. A
cancellation poll taken between boundaries is observation and renews
nothing. And a run that reports checkpoints produces the same response as
one that does not: reporting sits beside the work, never in it.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from app.config import settings
from app.data_lake.path_policy import lake_subpath
from app.jobs import progress
from app.jobs.checkpoints import CheckpointGate, ExecutionCheckpoint
from app.research.backtest_runs.service import SAVE_FAILED
from app.schemas.engine_backtest import EngineBacktestRequest
from app.services import engine_backtest_service as engine_service
from app.services.engine_backtest_service import execute_engine_backtest
from tests._helpers.lean_store import seed_store_day
from tests.jobs.conftest import _FakeRedis

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

DAY_ONE = date(2026, 1, 5)  # Monday
DAY_THREE = date(2026, 1, 7)
SEEDED_DAYS = (DAY_ONE, date(2026, 1, 6), DAY_THREE)
JOB = "serial-proof"
GENERATION = 1
LEASE = f"job:{JOB}:lease"


def _request() -> EngineBacktestRequest:
    return EngineBacktestRequest(
        strategy_name="sma_crossover",
        params={"symbol": "SPY"},
        from_date=DAY_ONE.isoformat(),
        to_date=DAY_THREE.isoformat(),
        # The seeded lake already holds the window; reading it is the path
        # whose boundaries this test instruments.
        auto_fetch=False,
        data_policy={
            "source": "polygon",
            "symbol": "SPY",
            "adjusted": False,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 1},
        },
    )


@pytest.fixture(autouse=True)
def offline_persistence(monkeypatch):
    """The .NET study save is best-effort and not what these tests are about."""
    monkeypatch.setattr(engine_service, "persist_engine_response_sync", lambda **kwargs: SAVE_FAILED)


@pytest.fixture
def lake(tmp_path: Path, monkeypatch) -> Path:
    write_root = tmp_path / "writer-root"
    lake_dir = write_root / lake_subpath("raw")
    lake_dir.mkdir(parents=True)
    (write_root / "staging").mkdir()
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    for day in SEEDED_DAYS:
        seed_store_day(lake_dir, "SPY", day)
    return lake_dir


def _without_wall_clock(response: object) -> dict:
    """The response with its run-time stamp normalized — the one field two identical runs may differ in."""
    payload = response.model_dump()  # type: ignore[attr-defined]
    payload["run_verdict"]["generated_at_ms"] = 0
    return payload


def test_reporting_checkpoints_changes_nothing_and_renews_only_on_boundaries(lake, monkeypatch):
    plain = execute_engine_backtest(request=_request(), on_phase=lambda phase: None, on_log=lambda message: None)

    fake = _FakeRedis()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    fake.strings[LEASE] = "1"  # the worker holds the lease
    seen: list[ExecutionCheckpoint] = []
    gate = CheckpointGate(JOB, GENERATION, on_accepted=seen.append)
    poll = progress.CancellationCheck(JOB, renews_lease=False)

    def on_phase(phase: str) -> None:
        poll.should_cancel()  # observation between boundaries: renews nothing
        assert gate.accept(
            ExecutionCheckpoint(
                generation=GENERATION,
                task_id="serial",
                sequence=len(seen) + 1,
                cursor=0,
                cursor_kind="processed_blocks",
                phase=phase,
                occurred_at_ms=time.time_ns() // 1_000_000,
                transition=True,
            )
        )

    reported = execute_engine_backtest(request=_request(), on_phase=on_phase, on_log=lambda message: None)

    assert _without_wall_clock(reported) == _without_wall_clock(plain), (
        "checkpoint reporting sits beside the work, never in it"
    )
    assert [checkpoint.phase for checkpoint in seen] == [
        "consolidating_bars",  # preparation
        "running_indicators",  # simulation
        "aggregating_results",  # aggregation
        "persisting",  # persistence
    ]
    assert fake.expires == [(LEASE, gate._lease_ttl_s)] * len(seen)
