"""Real spawned-worker parity over archived ZIP bytes and exact Decimal evidence."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import respx

from app.data_lake.admission import read_committed_bytes
from app.options_bots.golden_proof import input_manifest
from app.research.golden_search.proof import BlobStore
from app.research.golden_search.worker import ResearchWorker, WorkerDispatchContext
from app.research.options_golden_search.dispatch import ReadyOptions
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.identity import execution_identity
from app.research.options_golden_search.protocol import Window
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import FRIDAY_OPEN_MS, raw_root
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_bots.test_golden_proof import _run


@respx.mock
@pytest.mark.parametrize("missing_option_day", [False, True])
async def test_spawned_worker_reproduces_all_exact_evidence_from_retained_inputs(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, missing_option_day: bool,
) -> None:
    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "blobs")
    manifest = input_manifest(source)
    retained = 0
    for relative, expected in manifest.items():
        payload = await asyncio.to_thread(read_committed_bytes, raw_root(tmp_lake) / relative)
        assert blobs.put(payload) == expected
        retained += len(payload)
    absent = tuple(path for path in manifest if path.startswith("option/") and "/listing/" not in path) if missing_option_day else ()
    assert not missing_option_day or absent
    snapshot = Snapshot(manifest={path: digest for path, digest in manifest.items() if path not in absent},
                        absent=absent, retained_bytes=retained)
    window = Window(key="development", start_ms=FRIDAY_OPEN_MS, end_ms=FRIDAY_OPEN_MS + 86_400_000,
                    session_ms=(FRIDAY_OPEN_MS,), annualization_sessions=251, activity_floor=1)
    expected = evaluate_setup(source.setup, window, SnapshotRunData("SPY", snapshot, blobs))
    item = ReadyOptions(key="a" * 64, attempt=1, task_gen=1, payload={
        "setup": source.setup.model_dump(mode="json"), "window": window.model_dump(mode="json"),
        "snapshot": snapshot.model_dump(mode="json"), "blob_root": str(blobs.root),
        "identity": execution_identity(),
    })
    context = WorkerDispatchContext(strategy_key="options", execution={}, manifest={}, identity={}, job_id="", generation=1)
    def execute():
        worker = ResearchWorker(context)
        try:
            return worker.run_task(item.as_wire())
        finally:
            worker.close()
    message = await asyncio.to_thread(execute)
    delivered = item.outcome_of(message)
    assert delivered.result == expected
    assert bool(delivered.result.input_failures) == missing_option_day


@respx.mock
async def test_production_snapshot_retains_admitted_selection_inventory_with_byte_limits(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    from app.research.options_golden_search.snapshots import capture_snapshot
    from tests.research.options_golden_search.test_protocol_search import protocol

    source = await _run(fake_catalog, tmp_lake)
    window = Window(key="development", start_ms=FRIDAY_OPEN_MS, end_ms=FRIDAY_OPEN_MS + 86_400_000,
                    session_ms=(FRIDAY_OPEN_MS,), annualization_sessions=251, activity_floor=1)
    plan = protocol().model_copy(update={"setup": source.setup, "development": window, "final": window})
    blobs = BlobStore(tmp_path / "snapshot")
    retained = await asyncio.to_thread(capture_snapshot, plan, blobs)
    assert retained.manifest == input_manifest(source)
    assert retained.absent == ()
    assert retained.retained_bytes == sum(len(blobs.get(digest)) for digest in retained.manifest.values())
    bounded = plan.model_copy(update={"budget": plan.budget.model_copy(update={"max_snapshot_bytes": 1})})
    with pytest.raises(ValueError, match="byte bound"):
        await asyncio.to_thread(capture_snapshot, bounded, blobs)


@pytest.mark.slow
@pytest.mark.parametrize("width", [1, 2, 4, 8])
@respx.mock
async def test_real_shared_pool_width_matrix_preserves_exact_evidence_and_global_bound(
    fake_catalog: FakeCatalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.config import settings
    from app.research.golden_search.shared_pool import SharedWorkerPool
    from app.research.options_golden_search.dispatch import OptionsDispatch

    source = await _run(fake_catalog, tmp_lake)
    blobs = BlobStore(tmp_path / "pool-blobs")
    manifest = input_manifest(source)
    retained = 0
    for relative, expected_digest in manifest.items():
        payload = await asyncio.to_thread(read_committed_bytes, raw_root(tmp_lake) / relative)
        assert blobs.put(payload) == expected_digest
        retained += len(payload)
    snapshot = Snapshot(manifest=manifest, absent=(), retained_bytes=retained)
    window = Window(key="development", start_ms=FRIDAY_OPEN_MS, end_ms=FRIDAY_OPEN_MS + 86_400_000,
                    session_ms=(FRIDAY_OPEN_MS,), annualization_sessions=251, activity_floor=1)
    expected = evaluate_setup(source.setup, window, SnapshotRunData("SPY", snapshot, blobs))
    payload = {"setup": source.setup.model_dump(mode="json"), "window": window.model_dump(mode="json"),
        "snapshot": snapshot.model_dump(mode="json"), "blob_root": str(blobs.root), "identity": execution_identity()}
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", width)
    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", width)
    pool = SharedWorkerPool(workers=width, submission_bound=2 * width,
                            payload_bound_bytes=32 * 1024**2, result_bound_bytes=32 * 1024**2)
    def run_owner(owner):
        context = WorkerDispatchContext(strategy_key="options", execution={}, manifest={}, identity={},
                                         job_id=f"owner_{owner}", generation=1)
        dispatch = OptionsDispatch(context, pool=pool)
        items = [ReadyOptions(key=f"{owner}_{index}", attempt=1, task_gen=1, payload=payload) for index in range(8)]
        try:
            return list(dispatch(items))
        finally:
            dispatch.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_owner, owner) for owner in range(2)]
            results = [await asyncio.to_thread(future.result) for future in futures]
        assert all(delivery.result == expected for owner in results for delivery in owner)
        assert [len(owner) for owner in results] == [8, 8]
        assert pool.facts()["busy_lanes"] == 0
    finally:
        pool.close()
