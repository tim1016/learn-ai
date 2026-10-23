"""Real Backtest, Options Study and Signal owners share lanes and the Options cap."""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
import respx

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import read_committed_bytes, stop_admission_batcher
from app.options_backtest.dispatch import replay_frozen_run
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.timings import RunTimings
from app.options_bots.golden_proof import input_manifest
from app.research.golden_search.pool import PooledWorkerDispatch
from app.research.golden_search.proof import BlobStore
from app.research.golden_search.shared_pool import SharedWorkerPool
from app.research.golden_search.worker import WorkerDispatchContext
from app.research.options_golden_search.dispatch import OptionsDispatch, ReadyOptions
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.identity import execution_identity
from app.research.options_golden_search.protocol import Window
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from tests.options_backtest._lake import FRIDAY, FRIDAY_OPEN_MS, raw_root
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_bots.test_golden_proof import _run
from tests.research.golden_search.test_worker import (
    _assumptions,
    _reference_lake,
    _runtime_identity,
    _serial_answer,
    _serial_item,
)


@pytest.mark.slow
@respx.mock
async def test_real_mixed_owners_advance_and_options_backtests_and_studies_share_one_global_cap(
    seeded_lake_catalog, fake_catalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    frozen = await asyncio.to_thread(freeze_run_inputs, source.setup, (FRIDAY,), LakeRunData("SPY"), tmp_path / "frozen")
    blobs = BlobStore(tmp_path / "study")
    manifest = input_manifest(source)
    retained = 0
    for path, digest in manifest.items():
        payload = await asyncio.to_thread(read_committed_bytes, raw_root(tmp_lake) / path)
        assert blobs.put(payload) == digest
        retained += len(payload)
    snapshot = Snapshot(manifest=manifest, absent=(), retained_bytes=retained)
    window = Window(key="development", start_ms=FRIDAY_OPEN_MS, end_ms=FRIDAY_OPEN_MS + 86_400_000,
                    session_ms=(FRIDAY_OPEN_MS,), annualization_sessions=251, activity_floor=1)
    expected_study = evaluate_setup(source.setup, window, SnapshotRunData("SPY", snapshot, blobs))
    study_payload = {"setup": source.setup.model_dump(mode="json"), "window": window.model_dump(mode="json"),
        "snapshot": snapshot.model_dump(mode="json"), "blob_root": str(blobs.root), "identity": execution_identity()}

    # Retained Options workers need no lake/catalog. Signal still exercises
    # its parent admission proxy against the exact fixture receipts.
    writer, _, signal_manifest = _reference_lake(tmp_path / "signal")
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(writer))
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(UUID(int=3044)))
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(writer))
    monkeypatch.setenv("DATA_LAKE_ROOT_ID", str(UUID(int=3044)))

    async def receipt(root, mode, path, digest, size):
        return seeded_lake_catalog.get((root, mode, path)) == (digest, size)

    async def receipts(requests):
        # The parent's admission batcher asks the set-based form; ``fake_catalog``
        # replaces it with answers from its own rows, which hold no Signal lake.
        return frozenset([index for index, request in enumerate(requests) if await receipt(*request)])

    async def init_fixture_pool(**kwargs) -> None:
        # The receipt fixture supplies the catalog boundary for this test.
        return None

    monkeypatch.setattr(catalog_client, "init_pool", init_fixture_pool)
    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", receipt)
    monkeypatch.setattr(catalog_client, "has_committed_file_receipts", receipts)
    expected_signal = await asyncio.to_thread(_serial_answer, signal_manifest)
    assert expected_signal.status == "completed", expected_signal.error
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", 2)
    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", 1)
    pool = SharedWorkerPool(workers=2, submission_bound=4, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    start = threading.Barrier(3)
    began: list[str] = []
    events: list[str] = []
    assigned: set[str] = set()
    samples: list[tuple[int, int, int]] = []
    monitor_stop = threading.Event()

    def sample() -> None:
        while not monitor_stop.wait(0.001):
            with pool._state_changed:
                active = [lane.current for lane in pool._lanes if lane.current is not None]
                samples.append((sum(entry.owner.context.concurrency_group == "options_replay" for entry in active),
                                len(active), pool._submitted_total))
                for entry in active:
                    job = entry.owner.context.job_id
                    if job not in assigned:
                        assigned.add(job)
                        events.append(job + ":began")

    def checkpoint(owner: str):
        def accept(message) -> None:
            if owner not in began:
                began.append(owner)
        return accept

    def backtest():
        context = WorkerDispatchContext("options_backtest", {}, {}, {}, "backtest", 1, on_checkpoint=checkpoint("backtest"))
        start.wait(timeout=10)
        result = replay_frozen_run(source.setup, (FRIDAY,), frozen, context=context, pool=pool, timings=RunTimings())
        events.append("backtest:finished")
        return result

    def study():
        context = WorkerDispatchContext("options", {}, {}, {}, "study", 1, on_checkpoint=checkpoint("study"))
        dispatch = OptionsDispatch(context, pool=pool)
        start.wait(timeout=10)
        try:
            results = list(dispatch([ReadyOptions(f"study-{index}", 1, 1, study_payload) for index in range(14)]))
            events.append("study:finished")
            return results
        finally:
            dispatch.close()

    def signal():
        context = WorkerDispatchContext("sma_crossover", _assumptions(), signal_manifest, _runtime_identity(), "signal", 1,
                                        on_checkpoint=checkpoint("signal"))
        dispatch = PooledWorkerDispatch(context, workers=2, window=2, pool=pool)
        start.wait(timeout=10)
        try:
            results = list(dispatch(None, [replace(_serial_item(), key=f"signal-{index}") for index in range(14)]))
            events.append("signal:finished")
            return results
        finally:
            dispatch.close()

    executor = ThreadPoolExecutor(max_workers=3)
    observer = threading.Thread(target=sample, daemon=True)
    observer.start()
    try:
        futures = [executor.submit(work) for work in (backtest, study, signal)]
        try:
            results = await asyncio.wait_for(asyncio.gather(*(asyncio.to_thread(future.result) for future in futures)), timeout=20)
        except BaseException:
            logging.getLogger(__name__).error("mixed task evidence: %s %s %s", began, pool.facts(),
                [(lane.current.item.key if lane.current else None) for lane in pool._lanes])
            raise
        assert results[0] == source
        assert all(delivery.result == expected_study for delivery in results[1])
        assert all(delivery.result.metrics == expected_signal for delivery in results[2])
        assert set(began) == {"backtest", "study", "signal"}
        assert events.index("backtest:began") < events.index("study:finished"), events
        assert events.index("study:began") < events.index("backtest:finished"), events
        assert min(events.index("study:began"), events.index("backtest:began")) < events.index("signal:finished"), events
        assert max(options for options, _, _ in samples) == 1
        assert max(active for _, active, _ in samples) == 2
        assert max(submitted for _, _, submitted in samples) <= 4
        assert pool.facts()["submitted_unconsumed"] == 0
    finally:
        monitor_stop.set()
        observer.join(timeout=2)
        pool.close()
        executor.shutdown(wait=True, cancel_futures=True)
        stop_admission_batcher()
