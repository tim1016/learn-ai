"""Exact chronological chain parity, frozen input integrity and bounded cache."""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.options_backtest.chains import merge_chains, plan_chains, replay_chain
from app.options_backtest.dispatch import replay_frozen_run
from app.options_backtest.frozen_inputs import DecodedOptionCache, FrozenRunData, freeze_run_inputs
from app.options_backtest.run import run_sessions
from app.options_backtest.run_wire import result_from_wire, result_to_wire
from app.options_backtest.timings import RunTimings
from app.research.golden_search.proof import ProofMismatchError
from app.research.golden_search.shared_pool import SharedWorkerPool
from app.research.golden_search.worker import WorkerDispatchContext
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_backtest._run_data import HALF_DAY, THU, WED, Day, fake_data, flat, steady
from tests.options_backtest.setup_fixtures import leg, setup


def scene():
    return fake_data({
        WED: Day(underlying=steady("500.20"), options={('call', 500): {**flat("4", 5, 99), 100: ("4", "3.50")}}),
        THU: Day(underlying=steady("500.20"), options={('call', 500): {**flat("2", 5, 344), 345: ("2.40", "2.40")}}),
        HALF_DAY: Day(underlying=steady("500.20", last=210), options={('call', 500): flat("2", 1, 210)}),
    })


@pytest.mark.parametrize("cash", ["200", "10000"])
def test_shuffled_chain_completion_preserves_exact_carry_marks_refusals_fees_and_half_days(cash: str) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})], starting_cash_usd=cash,
        fees={"kind": "per_contract", "per_contract_usd": "0.65"}, slippage_percent="0.3")
    sessions = (WED, THU, HALF_DAY)
    serial = run_sessions(given, sessions, scene())
    plan = list(plan_chains(given))
    random.Random(3412).shuffle(plan)
    results = {chain.key: result_from_wire(result_to_wire(replay_chain(given, sessions, chain, scene()))) for chain in plan}
    assert merge_chains(given, sessions, results) == serial
    assert len(plan) == len(serial.slippage) + len(serial.summary.cash_ladder) - 1
    results.pop(plan[0].key)
    with pytest.raises(ValueError, match="every planned chain"):
        merge_chains(given, sessions, results)


def test_frozen_data_is_equal_warm_cold_and_after_eviction_and_refuses_changed_bytes(tmp_path: Path) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    sessions = (WED, THU, HALF_DAY)
    frozen = freeze_run_inputs(given, sessions, scene(), tmp_path)
    expected = run_sessions(given, sessions, scene())
    for cap in (1, 64 * 1024**2):
        cache = DecodedOptionCache(cap)
        for _ in range(2):
            assert run_sessions(given, sessions, FrozenRunData(frozen, cache)) == expected
            assert cache.bytes <= cap
        if cap > 1:
            assert cache.hits > 0
    digest = next(iter(frozen.sessions.values()))
    path = tmp_path / digest[:2] / digest
    path.write_bytes(b"changed bytes")
    with pytest.raises(ProofMismatchError):
        run_sessions(given, sessions, FrozenRunData(frozen, cache))


def test_same_inventory_reuses_decode_across_job_directories_but_new_root_identity_misses(tmp_path: Path) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    first = freeze_run_inputs(given, (WED,), scene(), tmp_path / "first")
    second = freeze_run_inputs(given, (WED,), scene(), tmp_path / "second")
    assert first.digest == second.digest
    cache = DecodedOptionCache(64 * 1024**2)
    digest = next(iter(first.sessions.values()))
    assert cache.read(first, digest) == cache.read(second, digest)
    assert (cache.hits, cache.misses) == (1, 1)
    changed = second.model_copy(update={"source_root_id": "another-source-root"})
    cache.read(changed, digest)
    assert cache.misses == 2


def test_cache_retained_byte_bound_holds_over_480_distinct_session_payloads(tmp_path: Path) -> None:
    import json
    import zlib

    from app.options_backtest.frozen_inputs import FrozenRunInputs
    from app.research.golden_search.proof import BlobStore

    blobs = BlobStore(tmp_path)
    frozen = FrozenRunInputs(root=str(tmp_path), underlying="SPY", setup_digest="a" * 64,
                             source_root_id="fixture-root", sessions={}, retained_bytes=0)
    cache = DecodedOptionCache(4096)
    for index in range(480):
        # Session metadata makes each admitted payload distinct. No decoded
        # bars are required to prove the worker's total-retention invariant.
        digest = blobs.put(zlib.compress(json.dumps({"options": {}, "session_index": index}).encode()))
        assert cache.read(frozen, digest) == {"options": {}}
        assert cache.bytes <= cache.max_bytes
    assert cache.misses == 480


def test_worker_pool_preserves_exact_result_and_reuses_admitted_decode(tmp_path: Path) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    frozen = freeze_run_inputs(given, (WED, THU), scene(), tmp_path)
    expected = run_sessions(given, (WED, THU), scene())
    pool = SharedWorkerPool(workers=1, submission_bound=2, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    ledger, metrics = RunTimings(), {}
    try:
        for attempt in range(2):
            context = WorkerDispatchContext(strategy_key="options_backtest", execution={}, manifest={},
                identity={}, job_id=f"test_{attempt}", generation=1)
            assert replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool,
                                     timings=ledger, metadata=metrics) == expected
        assert metrics["cache_hits"] > 0
        assert metrics["cache_bytes_peak"] > 0
        assert metrics["frozen_input_digest"] == frozen.digest
        assert {row.phase for row in ledger.rows} >= {"queue", "replay", "admit_decode", "aggregation"}
    finally:
        pool.close()


@pytest.mark.slow
@pytest.mark.parametrize("width", [1, 2, 4])
@pytest.mark.parametrize("recording", ["bought", "sold"])
async def test_both_real_goldens_are_exact_on_shared_pool_cold_and_warm(
    fake_catalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    width: int, recording: str,
) -> None:
    from app.data_lake.option_session_repack import repack_option_session
    from app.data_lake.path_policy import OptionMinuteBarPath
    from app.options_backtest.lake_data import LakeRunData
    from app.options_bots.golden_proof import result_sha256
    from tests.options_backtest import test_golden_spy_2025_06_10 as bought
    from tests.options_backtest import test_golden_spy_2026_10_09_sold_spread as sold
    from tests.options_backtest._lake import raw_root

    golden = bought if recording == "bought" else sold
    given = bought.a_setup() if recording == "bought" else sold.a_call_credit_spread(stop_percent="25")
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH):
        await golden.capture_the_recording()
    # All provider routes are now gone. No worker has a catalog or provider.
    sessions = (golden.TRADING_DATE,)
    legacy = await asyncio.to_thread(run_sessions, given, sessions, LakeRunData("SPY"))
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", width)
    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", width)
    pool = SharedWorkerPool(workers=width, submission_bound=2 * width, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    try:
        # Reuse each real worker pool for both layouts instead of doubling its
        # process startup cost across the recording/width matrix.
        for layout in ("legacy", "container"):
            if layout == "container":
                assert (await repack_option_session("SPY", golden.TRADING_DATE)).failure is None
                for pin in legacy.sessions[0].pins.option_days:
                    path = raw_root(tmp_lake) / OptionMinuteBarPath(pin.contract.root, pin.contract.occ_symbol,
                                                                  golden.TRADING_DATE).relative_path()
                    path.unlink()
            expected = await asyncio.to_thread(run_sessions, given, sessions, LakeRunData("SPY"))
            assert result_sha256(expected) == result_sha256(legacy)
            assert all((pin.reference is None) == (layout == "legacy") for pin in expected.sessions[0].pins.option_days)
            frozen = await asyncio.to_thread(freeze_run_inputs, given, sessions, LakeRunData("SPY"), tmp_path / layout)
            for run in range(2):
                context = WorkerDispatchContext(strategy_key="options_backtest", execution={}, manifest={},
                    identity={}, job_id=f"golden_{layout}_{run}", generation=1)
                metadata = {}
                actual = await asyncio.to_thread(replay_frozen_run, given, sessions, frozen, context=context,
                    workers=width, pool=pool, cold_cache=run == 0, metadata=metadata)
                assert actual == expected
                assert metadata["cache_misses"] > 0 if run == 0 else metadata["cache_hits"] > 0
    finally:
        pool.close()


def test_utc_phase_anchors_and_monotonic_elapsed_have_a_bounded_rounding_error() -> None:
    clock = iter([1000, 1010, 1010, 1020])
    monotonic = iter([1.0, 1.01, 1.01, 1.02])
    ledger = RunTimings(clock=lambda: next(clock), monotonic=lambda: next(monotonic))
    with ledger.measure("check"):
        pass
    with ledger.measure("download"):
        pass
    assert abs(sum(row.duration_ms for row in ledger.rows) - 20) <= len(ledger.rows)
    assert [(row.started_at_ms, row.finished_at_ms) for row in ledger.rows] == [(1000, 1010), (1010, 1020)]


def test_exclusive_phase_sum_includes_coordination_and_excludes_parallel_chain_spans() -> None:
    from app.options_backtest.timings import PhaseTiming

    clock = iter([1000, 1050])
    monotonic = iter([1.0, 1.05])
    ledger = RunTimings(clock=lambda: next(clock), monotonic=lambda: next(monotonic))
    ledger.start()
    ledger.rows.extend([
        PhaseTiming(phase="replay", started_at_ms=1000, finished_at_ms=1040, duration_ms=40),
        PhaseTiming(phase="replay", started_at_ms=1000, finished_at_ms=1040, duration_ms=40, scope="chain", chain_key="a"),
        PhaseTiming(phase="replay", started_at_ms=1000, finished_at_ms=1040, duration_ms=40, scope="chain", chain_key="b"),
    ])
    assert ledger.seal() == 50
    assert sum(row.duration_ms for row in ledger.rows if row.scope == "run") == 50


def test_backtest_identity_refuses_decoder_changes_without_changing_approval_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.options_backtest import execution_identity as identity

    before = identity.backtest_execution_identity()
    assert "backtest_execution" in before
    (tmp_path / "frozen_inputs.py").write_text("changed decoder")
    monkeypatch.setattr(identity, "_ROOT", tmp_path)
    with pytest.raises(ValueError, match="code changed"):
        identity.backtest_execution_identity()


@pytest.mark.slow
@pytest.mark.parametrize("width", [1, 2, 4])
async def test_generated_busy_scenario_stops_momentum_reentry_marks_half_day_are_exact_in_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int,
) -> None:
    from tests.options_backtest._a_run import the_busy_data, the_busy_setup, what_the_busy_run_reaches

    given = the_busy_setup()
    sessions, data = the_busy_data()
    expected = run_sessions(given, sessions, data)
    assert all(what_the_busy_run_reaches(expected).values())
    frozen = freeze_run_inputs(given, sessions, the_busy_data()[1], tmp_path / "inputs")
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", width)
    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", width)
    pool = SharedWorkerPool(workers=width, submission_bound=2 * width,
                           payload_bound_bytes=32 * 1024**2, result_bound_bytes=32 * 1024**2)
    try:
        context = WorkerDispatchContext("options_backtest", {}, {}, {}, "busy", 1)
        actual = await asyncio.to_thread(replay_frozen_run, given, sessions, frozen, context=context, workers=width, pool=pool)
        assert actual == expected
    finally:
        pool.close()


@pytest.mark.slow
@pytest.mark.parametrize("persistent", [False, True])
def test_killed_chain_is_retried_with_same_identity_and_persistent_loss_returns_no_result(
    tmp_path: Path, persistent: bool,
) -> None:
    from app.research.golden_search.evaluator import WorkerLost
    from tests.options_backtest._a_run import the_busy_data, the_busy_setup

    given = the_busy_setup()
    sessions, data = the_busy_data()
    expected = run_sessions(given, sessions, data)
    frozen = freeze_run_inputs(given, sessions, the_busy_data()[1], tmp_path)
    pool = SharedWorkerPool(workers=1, submission_bound=2, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    killed: list[tuple[str, int]] = []

    def checkpoint(message) -> None:
        lane = pool._lanes[0]
        if message["phase"] == "preparation" and (
            not killed or (persistent and lane.current.item.key == killed[0][0])
        ):
            killed.append((lane.current.item.key, lane.current.item.task_gen))
            lane.worker._child.kill()

    context = WorkerDispatchContext("options_backtest", {}, {}, {}, "kill-test", 1, on_checkpoint=checkpoint)
    try:
        if persistent:
            with pytest.raises(WorkerLost, match="retry allowance"):
                replay_frozen_run(given, sessions, frozen, context=context, pool=pool)
            assert len(killed) >= 2
            assert len({key for key, generation in killed}) == 1
        else:
            assert replay_frozen_run(given, sessions, frozen, context=context, pool=pool) == expected
            assert killed == [("kill-test:slippage:0", 1)]
        assert pool.facts()["submitted_unconsumed"] == 0
    finally:
        pool.close()


@pytest.mark.slow
def test_cancelling_one_backtest_releases_shared_capacity_for_the_next_owner(tmp_path: Path) -> None:
    import threading

    from app.jobs.progress import JobCancelled

    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    frozen = freeze_run_inputs(given, (WED, THU), scene(), tmp_path)
    stopped = threading.Event()
    context = WorkerDispatchContext("options_backtest", {}, {}, {}, "cancel-one", 1,
        on_checkpoint=lambda message: stopped.set(), cancel_check=stopped.is_set)
    pool = SharedWorkerPool(workers=1, submission_bound=2, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    try:
        with pytest.raises(JobCancelled):
            replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool)
        context = WorkerDispatchContext("options_backtest", {}, {}, {}, "healthy-next", 1)
        assert replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool) == run_sessions(given, (WED, THU), scene())
    finally:
        pool.close()


async def test_profiler_uses_captured_temporary_lake_and_reports_cumulative_functions(
    fake_catalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    from app.options_backtest.lake_data import LakeRunData
    from scripts.profile_options_backtest import profile_run
    from tests.options_backtest import test_golden_spy_2025_06_10 as golden

    given = golden.a_setup()
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH):
        await golden.capture_the_recording()
    data = LakeRunData("SPY")
    output = tmp_path / "profile.txt"
    actual = await asyncio.to_thread(profile_run, given, (golden.TRADING_DATE,), data, tmp_path / "inputs", output)
    assert actual == await asyncio.to_thread(run_sessions, given, (golden.TRADING_DATE,), data)
    assert "cumulative" in output.read_text()


async def test_unavailable_runtime_refuses_without_creating_or_replaying_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routers.options_backtest_runs import jobs_router
    from app.utils.session_anchors import et_midnight_ms

    monkeypatch.setattr(settings, "RESEARCH_EXECUTION_MODE", "research")
    monkeypatch.setattr(settings, "RESEARCH_RUNTIME_URL", "")
    monkeypatch.delenv("RESEARCH_RUNTIME", raising=False)
    app = FastAPI()
    app.include_router(jobs_router, prefix="/api/jobs-internal")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/jobs-internal/options-backtest", json={
            "job_id": "runtime_missing", "setup": setup().model_dump(mode="json"),
            "start_ms": et_midnight_ms(WED), "end_ms": et_midnight_ms(THU)})
    assert response.status_code == 503
    assert "RESEARCH_RUNTIME_URL" in response.json()["detail"]
