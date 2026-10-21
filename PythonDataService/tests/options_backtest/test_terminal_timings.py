"""Terminal lifecycle keeps partial operational history without changing the outcome (#3417)."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import respx

from app.jobs.progress import JobCancelled, ProgressEmitter
from app.options_backtest import runtime, service
from app.options_backtest.dispatch import replay_frozen_run
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.research.golden_search.shared_pool import SharedWorkerPool
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.persistence import ownership
from app.utils.background_loop import CallerStoppedWaitingError
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_lake import Vendor
from tests.options_backtest.test_chains import THU, WED, scene
from tests.options_backtest.test_preparation_protocol import owner as owner
from tests.options_backtest.test_preparation_protocol import send, socket_peer
from tests.options_backtest.test_service import FakeStore, Job, _execute, the_scene


@dataclass
class ObservationStore(FakeStore):
    observations: list[tuple[int, list[dict[str, Any]], dict[str, Any]]] = field(default_factory=list)
    unavailable: bool = False

    def record_timings(self, run_id: int, timings: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
        if self.unavailable:
            raise OSError("observations unavailable")
        self.observations.append((run_id, timings, metadata))


@respx.mock
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_local_failure_or_cancel_keeps_partial_timings_and_original_outcome(
    fake_catalog: FakeCatalog, tmp_lake: Path, caplog: pytest.LogCaptureFixture,
    cancel: bool, unavailable: bool,
) -> None:
    vendor = Vendor(the_scene())
    store, job = ObservationStore(unavailable=unavailable), Job()
    if cancel:
        vendor.on_option_request = lambda symbol: job.stop.set()
    else:
        vendor.listing_errors[date(2026, 10, 6)] = 403
    vendor.mount()

    with pytest.raises(service.RunCancelled if cancel else service.RunFailedError) as caught:
        await _execute(store, job)

    assert store.completed == [] and store.endings == 1
    if not cancel:
        assert caught.value.failure == store.failed[0][1]
        assert caught.value.failure.code == service.DOWNLOAD_FAILED
    if unavailable:
        assert store.observations == []
        assert "execution observations" in caplog.text
    else:
        [(run_id, rows, metadata)] = store.observations
        assert run_id == 7
        assert {row["phase"] for row in rows} == {"check", "download", "coordination"}
        assert sum(row["duration_ms"] for row in rows if row["scope"] == "run") == metadata["wall_duration_ms"]


def test_observation_retry_reuses_sealed_history_after_storage_failure() -> None:
    ledger = RunTimings()
    ledger.start()
    store = ObservationStore(unavailable=True)
    service.record_observations(store, 7, ledger, {})
    original = ledger.wire()
    store.unavailable = False
    service.record_observations(store, 7, ledger, {})
    assert ledger.wire() == original == store.observations[0][1]
    assert [row["phase"] for row in original] == ["coordination"]


@pytest.mark.parametrize("fenced", [False, True])
def test_fenced_runtime_or_unconfirmed_save_writes_no_terminal_observations(
    owner: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, fenced: bool,
) -> None:
    req, _, authority, _, _ = owner
    monkeypatch.setattr(ownership, "process_authority", lambda: authority)

    async def prepare(*args: Any, **kwargs: Any) -> None:
        if fenced:
            raise ownership.GenerationFencedError("successor owns the run")

    def uncertain_save(*args: Any, **kwargs: Any) -> None:
        raise CallerStoppedWaitingError("uncertain write")

    monkeypatch.setattr(runtime, "prepare_over_api", prepare)
    monkeypatch.setattr(runtime, "replay_frozen_run", lambda *args, **kwargs: a_busy_run())
    store = ObservationStore()
    monkeypatch.setattr(store, "complete", uncertain_save)
    with pytest.raises(ownership.GenerationFencedError if fenced else service.RunFailedError):
        runtime.execute_runtime_run(run_id=7, req=req, sessions=(), store=store,
            emit=ProgressEmitter(req.job_id, renews_lease=False))
    assert store.endings == 0 and store.observations == []


@pytest.mark.slow
@pytest.mark.parametrize("stop_at", ["queue", "replay", "failed_replay"])
def test_interrupted_shared_pool_retains_elapsed_queue_and_replay(
    tmp_path: Path, stop_at: str,
) -> None:
    from tests.options_backtest.setup_fixtures import leg, setup

    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    frozen = freeze_run_inputs(given, (WED, THU), scene(), tmp_path)
    if stop_at == "failed_replay":
        digest = next(iter(frozen.sessions.values()))
        (tmp_path / digest[:2] / digest).write_bytes(b"corrupt admitted input")
    cancelled = threading.Event()
    if stop_at == "queue":
        cancelled.set()
    context = WorkerDispatchContext("options_backtest", {}, {}, {}, "interrupted", 1,
        on_checkpoint=lambda message: cancelled.set() if stop_at == "replay" else None,
        cancel_check=cancelled.is_set)
    ledger = RunTimings()
    ledger.start()
    pool = SharedWorkerPool(workers=1, submission_bound=2, payload_bound_bytes=32 * 1024**2,
        result_bound_bytes=32 * 1024**2)
    try:
        with pytest.raises(RuntimeError if stop_at == "failed_replay" else JobCancelled):
            replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool, timings=ledger)
        phases = [row.phase for row in ledger.rows if row.scope == "run"]
        assert phases == (["queue"] if stop_at == "queue" else ["queue", "replay"])
        wall_ms = ledger.seal()
        assert sum(row.duration_ms for row in ledger.rows if row.scope == "run") == wall_ms
    finally:
        pool.close()


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_runtime_terminal_preparation_keeps_partial_timings_without_masking_outcome(
    owner: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    cancel: bool, unavailable: bool,
) -> None:
    req, _, authority, _, state = owner
    monkeypatch.setattr(ownership, "process_authority", lambda: authority)
    store = ObservationStore(unavailable=unavailable)
    checked = PhaseTiming(phase="check", started_at_ms=1, finished_at_ms=1, duration_ms=0).model_dump(mode="json")
    downloaded = {**checked, "phase": "download"}

    async def peer(writer: Any) -> None:
        await send(writer, {**state, "timings": [checked]})
        await send(writer, {**state, "sequence": 2, "status": "cancelled" if cancel else "failed",
            "timings": [checked, downloaded], "code": service.DOWNLOAD_FAILED, "reason": "The download failed."})

    async with socket_peer(peer, monkeypatch):
        with pytest.raises(JobCancelled if cancel else service.RunFailedError) as caught:
            await asyncio.to_thread(runtime.execute_runtime_run, run_id=7, req=req, sessions=(),
                store=store, emit=ProgressEmitter(req.job_id, renews_lease=False))

    assert store.completed == [] and store.endings == 1
    if not cancel:
        assert caught.value.failure == store.failed[0][1]
        assert caught.value.failure.code == service.DOWNLOAD_FAILED
    if unavailable:
        assert store.observations == []
        assert "execution observations" in caplog.text
    else:
        [(run_id, rows, metadata)] = store.observations
        assert run_id == 7
        assert [row["phase"] for row in rows] == ["check", "download", "coordination"]
        assert sum(row["duration_ms"] for row in rows if row["scope"] == "run") == metadata["wall_duration_ms"]
