"""Exact chronological chain parity, frozen input integrity and bounded cache."""
from __future__ import annotations

import asyncio
import random
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.config import settings
from app.engine.data.decoded_cache import DecodedArtifactCache
from app.options_backtest.chains import merge_chains, plan_chains, replay_chain
from app.options_backtest.dispatch import replay_frozen_run
from app.options_backtest.frozen_inputs import FrozenRunData, freeze_run_inputs
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
from tests.options_backtest._run_data import HALF_DAY, THU, WED, chain_scene
from tests.options_backtest.setup_fixtures import leg, setup


@pytest.mark.parametrize("cash", ["200", "10000"])
def test_shuffled_chain_completion_preserves_exact_carry_marks_refusals_fees_and_half_days(cash: str) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})], starting_cash_usd=cash,
        fees={"kind": "per_contract", "per_contract_usd": "0.65"}, slippage_percent="0.3")
    sessions = (WED, THU, HALF_DAY)
    serial = run_sessions(given, sessions, chain_scene())
    plan = list(plan_chains(given))
    random.Random(3412).shuffle(plan)
    results = {chain.key: result_from_wire(result_to_wire(replay_chain(given, sessions, chain, chain_scene()))) for chain in plan}
    assert merge_chains(given, sessions, results) == serial
    assert len(plan) == len(serial.slippage) + len(serial.summary.cash_ladder) - 1
    results.pop(plan[0].key)
    with pytest.raises(ValueError, match="every planned chain"):
        merge_chains(given, sessions, results)


def test_frozen_data_is_equal_warm_cold_and_after_eviction_and_refuses_changed_bytes(tmp_path: Path) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    sessions = (WED, THU, HALF_DAY)
    frozen = freeze_run_inputs(given, sessions, chain_scene(), tmp_path)
    expected = run_sessions(given, sessions, chain_scene())
    for cap in (1, 64 * 1024**2):
        cache = DecodedArtifactCache(max_bytes=cap)
        for _ in range(2):
            assert run_sessions(given, sessions, FrozenRunData(frozen, cache)) == expected
            assert cache.metrics()["resident_bytes"] <= cap
        if cap > 1:
            assert cache.metrics()["hits"] > 0
    digest = next(iter(frozen.sessions.values()))
    path = tmp_path / digest[:2] / digest
    path.write_bytes(b"changed bytes")
    with pytest.raises(ProofMismatchError):
        run_sessions(given, sessions, FrozenRunData(frozen, cache))


def test_same_inventory_reuses_decode_across_job_directories_but_new_root_identity_misses(tmp_path: Path) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    first = freeze_run_inputs(given, (WED,), chain_scene(), tmp_path / "first")
    second = freeze_run_inputs(given, (WED,), chain_scene(), tmp_path / "second")
    assert first.digest == second.digest
    cache = DecodedArtifactCache(max_bytes=64 * 1024**2)
    assert FrozenRunData(first, cache).listing(WED) == FrozenRunData(second, cache).listing(WED)
    assert (cache.metrics()["hits"], cache.metrics()["misses"]) == (1, 1)
    changed = second.model_copy(update={"source_root_id": "another-source-root"})
    FrozenRunData(changed, cache).listing(WED)
    assert cache.metrics()["misses"] == 2


def test_worker_pool_preserves_exact_result_and_reuses_admitted_decode(tmp_path: Path) -> None:
    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    frozen = freeze_run_inputs(given, (WED, THU), chain_scene(), tmp_path)
    expected = run_sessions(given, (WED, THU), chain_scene())
    pool = SharedWorkerPool(workers=1, submission_bound=2, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    try:
        for attempt in range(2):
            context = WorkerDispatchContext(strategy_key="options_backtest", execution={}, manifest={},
                identity={}, job_id=f"test_{attempt}", generation=1)
            ledger = RunTimings()
            ledger.start()
            assert replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool, timings=ledger) == expected
        observed = ledger.seal()
        assert observed.replay is not None and observed.replay.cache_hits > 0
        assert observed.replay.cache_bytes_peak > 0
        assert observed.replay.frozen_input_digest == frozen.digest
        assert {row.phase for row in observed.phase_timings} >= {"queue", "replay", "admit_decode", "aggregation"}
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
                ledger = RunTimings()
                ledger.start()
                actual = await asyncio.to_thread(replay_frozen_run, given, sessions, frozen, context=context,
                    workers=width, pool=pool, cold_cache=run == 0, timings=ledger)
                assert actual == expected
                replay = ledger.seal().replay
                assert replay is not None
                assert replay.cache_misses > 0 if run == 0 else replay.cache_hits > 0
    finally:
        pool.close()


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
        actual = await asyncio.to_thread(replay_frozen_run, given, sessions, frozen, context=context, workers=width,
                                         pool=pool, timings=RunTimings())
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
                replay_frozen_run(given, sessions, frozen, context=context, pool=pool, timings=RunTimings())
            assert len(killed) >= 2
            assert len({key for key, generation in killed}) == 1
        else:
            assert replay_frozen_run(given, sessions, frozen, context=context, pool=pool, timings=RunTimings()) == expected
            assert killed == [("kill-test:slippage:0", 1)]
        assert pool.facts()["submitted_unconsumed"] == 0
    finally:
        pool.close()


@pytest.mark.slow
def test_cancelling_one_backtest_releases_shared_capacity_for_the_next_owner(tmp_path: Path) -> None:
    import threading

    from app.jobs.progress import JobCancelled

    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    frozen = freeze_run_inputs(given, (WED, THU), chain_scene(), tmp_path)
    stopped = threading.Event()
    context = WorkerDispatchContext("options_backtest", {}, {}, {}, "cancel-one", 1,
        on_checkpoint=lambda message: stopped.set(), cancel_check=stopped.is_set)
    pool = SharedWorkerPool(workers=1, submission_bound=2, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    try:
        with pytest.raises(JobCancelled):
            replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool, timings=RunTimings())
        context = WorkerDispatchContext("options_backtest", {}, {}, {}, "healthy-next", 1)
        assert replay_frozen_run(given, (WED, THU), frozen, context=context, pool=pool,
                                 timings=RunTimings()) == run_sessions(given, (WED, THU), chain_scene())
    finally:
        pool.close()


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
