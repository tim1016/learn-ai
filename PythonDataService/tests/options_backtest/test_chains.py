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
    from app.options_backtest.lake_data import LakeRunData
    from tests.options_backtest import test_golden_spy_2025_06_10 as bought
    from tests.options_backtest import test_golden_spy_2026_10_09_sold_spread as sold

    golden = bought if recording == "bought" else sold
    given = bought.a_setup() if recording == "bought" else sold.a_call_credit_spread(stop_percent="25")
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH):
        await golden.capture_the_recording()
    # All provider routes are now gone. No worker has a catalog or provider.
    sessions = (golden.TRADING_DATE,)
    expected = await asyncio.to_thread(run_sessions, given, sessions, LakeRunData("SPY"))
    frozen = await asyncio.to_thread(freeze_run_inputs, given, sessions, LakeRunData("SPY"), tmp_path / "frozen")
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", width)
    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", width)
    pool = SharedWorkerPool(workers=width, submission_bound=2 * width, payload_bound_bytes=32 * 1024**2,
                            result_bound_bytes=32 * 1024**2)
    try:
        for run in range(2):
            context = WorkerDispatchContext(strategy_key="options_backtest", execution={}, manifest={},
                identity={}, job_id=f"golden_{run}", generation=1)
            actual = await asyncio.to_thread(replay_frozen_run, given, sessions, frozen, context=context,
                                             workers=width, pool=pool)
            assert actual == expected
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


def test_profiler_uses_frozen_temporary_inputs_and_reports_cumulative_functions(tmp_path: Path) -> None:
    from scripts.profile_options_backtest import profile_run

    given = setup(legs=[leg(expiry={"kind": "same_day"})])
    output = tmp_path / "profile.txt"
    actual = profile_run(given, (WED, THU), scene(), tmp_path / "inputs", output)
    assert actual == run_sessions(given, (WED, THU), scene())
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
