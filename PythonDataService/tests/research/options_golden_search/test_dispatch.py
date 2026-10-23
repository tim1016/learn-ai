"""Real spawned-worker parity over archived ZIP bytes and exact Decimal evidence."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import respx

from app.data_lake.admission import read_committed_bytes
from app.options_backtest.run import RunResult
from app.options_backtest.setup import OptionsBacktestSetup
from app.options_bots.golden_proof import input_manifest
from app.research.golden_search.proof import BlobStore
from app.research.golden_search.worker import ResearchWorker, WorkerDispatchContext
from app.research.options_golden_search.dispatch import ReadyOptions, pins_to_wire
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.identity import execution_identity
from app.research.options_golden_search.marks import MARK_RULE_V1, MARK_RULE_V2
from app.research.options_golden_search.metrics import evidence_to_json
from app.research.options_golden_search.protocol import Window
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    minute_bar,
    raw_root,
    stock_bar,
    store_listing,
    store_option_day,
    store_underlying_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake
from tests.options_bots.test_golden_proof import _run


@pytest.mark.parametrize(("frozen", "carried", "missing"), [(MARK_RULE_V2, 1, 0), (MARK_RULE_V1, 0, 1), (None, 0, 1)])
def test_the_parent_values_a_workers_raw_evidence_under_the_frozen_mark_rule(
    frozen: str | None, carried: int, missing: int,
) -> None:
    from app.lean_sidecar.trading_calendar import session_window_for_date
    from tests.research.options_golden_search._evidence import scene

    day = date(2026, 10, 7)
    written, data = scene((day,), exits=("2",), missing_minute=7)
    clock = session_window_for_date(day)
    window = Window(key="development", start_ms=clock.open_ms_utc, end_ms=clock.close_ms_utc + 1,
                    session_ms=(clock.open_ms_utc,), annualization_sessions=251, activity_floor=1)
    raw = evaluate_setup(written, window, data, mark_rule=MARK_RULE_V2)
    payload = {"setup": written.model_dump(mode="json"), "window": window.model_dump(mode="json")}
    if frozen is not None:
        payload["mark_rule"] = frozen
    item = ReadyOptions(key="b" * 64, attempt=1, task_gen=1, payload=payload)
    message = {"key": item.key, "options_result": {"sessions": [evidence_to_json(s) for s in raw.sessions],
        "pins": pins_to_wire(raw.pins), "input_failures": [], "actual_reads": []}}
    delivered = item.outcome_of(message).result
    # A payload that names no rule is a study frozen before the rule existed: it means v1.
    assert delivered.mark_rule == (frozen or MARK_RULE_V1)
    assert (delivered.metrics.carried_held_marks or 0, delivered.metrics.missing_held_marks) == (carried, missing)


@pytest.mark.parametrize(("frozen", "expected"), [(MARK_RULE_V2, MARK_RULE_V2), (None, MARK_RULE_V1)])
def test_a_worker_replays_under_the_mark_rule_the_study_froze(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, frozen: str | None, expected: str,
) -> None:
    import threading
    from types import SimpleNamespace

    from app.research.options_golden_search import evaluator
    from app.research.options_golden_search.dispatch import run_options_task
    from tests.research.options_golden_search._evidence import scene

    written, _data = scene((date(2026, 10, 7),), exits=("2",))
    window = Window(key="development", start_ms=FRIDAY_OPEN_MS, end_ms=FRIDAY_OPEN_MS + 86_400_000,
                    session_ms=(FRIDAY_OPEN_MS,), annualization_sessions=251, activity_floor=1)
    seen: dict[str, object] = {}

    def spy(*_args: object, **kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(sessions=(), pins=(), input_failures=())

    messages: list[dict[str, object]] = []
    monkeypatch.setattr(evaluator, "evaluate_setup", spy)
    payload = {"setup": written.model_dump(mode="json"), "window": window.model_dump(mode="json"),
        "snapshot": Snapshot(manifest={}, absent=(), retained_bytes=0).model_dump(mode="json"),
        "blob_root": str(tmp_path), "identity": execution_identity()}
    if frozen is not None:
        payload["mark_rule"] = frozen
    run_options_task({"key": "k", "options": payload}, out=SimpleNamespace(write=messages.append),
        ask_admission=lambda _path: b"", cancel_flag=threading.Event(),
        state={"decoded_cache": SimpleNamespace(take_residency_delta=lambda: ([], []))})
    assert [message["type"] for message in messages if message["type"] != "checkpoint"] == ["result"]
    assert seen["mark_rule"] == expected


@pytest.mark.parametrize(("width", "reason"), [(3, "must be 1, 2, 4 or 8"), (4, "exceeds the current operator profile")])
def test_dispatch_distinguishes_unsupported_width_from_unqualified_capacity(
    monkeypatch: pytest.MonkeyPatch, width: int, reason: str,
) -> None:
    from app.config import settings
    from app.research.options_golden_search.dispatch import OptionsDispatch

    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", 2)
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", 8)
    context = WorkerDispatchContext(strategy_key="options", execution={}, manifest={}, identity={}, job_id="", generation=1)
    with pytest.raises(ValueError, match=reason):
        OptionsDispatch(context, workers=width)


async def _eight_leg_run(fake_catalog: FakeCatalog, tmp_lake: Path) -> RunResult:
    from app.options.contract import OptionContract
    from app.options_backtest.lake_data import LakeRunData
    from app.options_backtest.run import run_sessions
    from tests.options_backtest._market import leg, setup

    contracts = [OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=600 + index) for index in range(8)]
    await store_listing([vendor_listing_row(contract) for contract in contracts])
    await store_underlying_day(fake_catalog, tmp_lake,
        [stock_bar(FRIDAY_OPEN_MS + minute * 60000, "600", "600", "600", "600") for minute in range(65)])
    for contract in contracts:
        rows = [minute_bar(minute, 1, 1, 1, 1) for minute in range(65)]
        rows[6] = minute_bar(6, 1, 1, 0.85, 0.85)
        rows[7] = minute_bar(7, 0.9, 1, 0.9, 1)
        rows[60] = minute_bar(60, 2, 2, 2, 2)
        await store_option_day(contract, rows)
    legs = [{**leg(f"leg_{index}", stop=("percent", "10"), reentry=("at_once", 1)),
             "strike": {"kind": "atm_offset", "steps": index} if index else {"kind": "atm"}} for index in range(8)]
    written = OptionsBacktestSetup.model_validate({**setup(*legs, slippage="0.3").model_dump(mode="json"),
        "fees": {"kind": "per_contract", "per_contract_usd": "0.65"}})
    return await asyncio.to_thread(run_sessions, written, [FRIDAY], LakeRunData("SPY"))


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
    record_property: Callable[[str, object], None],
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from app.config import settings
    from app.options_bots.golden_proof import build_proof, result_sha256
    from app.research.benchmark import CorpusIdentity, _BenchmarkReady, _engine_request
    from app.research.golden_search.shared_pool import SharedWorkerPool, cgroup_bytes
    from app.research.golden_search.worker import _child_identity
    from app.research.options_golden_search.dispatch import OptionsDispatch
    from app.research.options_golden_search.protocol import CostScenario, apply_scenario
    from app.research.options_golden_search.stages import evidence_payload

    source = await _eight_leg_run(fake_catalog, tmp_lake)
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
    scenarios = (CostScenario(key="base"), CostScenario(key="slippage", slippage_multiplier=Decimal(2)),
                 CostScenario(key="fees", fee_multiplier=Decimal(2)))
    expected = {scenario.key: evaluate_setup(apply_scenario(source.setup, scenario), window,
        SnapshotRunData("SPY", snapshot, blobs), fee_multiplier=scenario.effective_fee_multiplier) for scenario in scenarios}
    assert all(result.complete and not result.metrics.technical_failures for result in expected.values())
    assert all(len(result.sessions[0].outcome.trades) == 16 for result in expected.values())
    payloads = [{"setup": apply_scenario(source.setup, scenario).model_dump(mode="json"), "window": window.model_dump(mode="json"),
        "snapshot": snapshot.model_dump(mode="json"), "blob_root": str(blobs.root), "identity": execution_identity(),
        "fee_multiplier": str(scenario.effective_fee_multiplier)} for scenario in scenarios]
    monkeypatch.setattr(settings, "RESEARCH_WORKER_COUNT", width)
    monkeypatch.setattr(settings, "OPTIONS_SEARCH_WORKER_COUNT", width)
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(tmp_lake))
    pool = SharedWorkerPool(workers=width, submission_bound=2 * width,
                            payload_bound_bytes=32 * 1024**2, result_bound_bytes=32 * 1024**2)
    def run_owner(owner: int):
        context = WorkerDispatchContext(strategy_key="options", execution={}, manifest={}, identity={},
                                         job_id=f"owner_{owner}", generation=1)
        dispatch = OptionsDispatch(context, pool=pool)
        items = [ReadyOptions(key=f"{owner}_{index}", attempt=1, task_gen=1, payload=payloads[index % len(scenarios)]) for index in range(8)]
        try:
            return list(dispatch(items))
        finally:
            dispatch.close()

    seal = _child_identity()
    stock_manifest = {path: digest for path, digest in manifest.items() if path.startswith("equity/")}
    signal_identity = CorpusIdentity(evaluation_key="signal", point_hash="signal", strategy="sma_crossover",
        group="capacity", expectation="complete", params={"symbol": "SPY", "short_window": 2, "long_window": 3, "resolution_minutes": 1},
        window_start_ms=window.start_ms, window_end_ms=window.end_ms - 1, window_kind="full-development", fold_index=None)
    execution = {"fill_mode": "next_bar_open", "commission_per_order": 0.0, "slippage_per_share": 0.0, "initial_cash": 10000.0}
    signal_request = _engine_request(signal_identity, execution=execution, data_policy={"source": "polygon", "symbol": "SPY", "adjusted": False,
        "session": "regular", "input_bars": {"timespan": "minute", "multiplier": 1}, "strategy_bars": {"timespan": "minute", "multiplier": 1}})

    def run_signal() -> list[dict]:
        context = WorkerDispatchContext(strategy_key="benchmark", execution=execution, manifest=stock_manifest, identity=seal,
                                         job_id="concurrent-signal", generation=1)
        handle = pool.register(context, window=width, epoch_of=lambda: 1)
        answers = []
        try:
            for index in range(8):
                item = _BenchmarkReady(key=f"signal-{index}", attempt=1, task_gen=1, identity=signal_identity,
                    request=signal_request.model_dump(mode="json"), manifest=stock_manifest, identity_seal=seal)
                deadline = time.monotonic() + 60
                while not handle.offer(1, item):
                    if time.monotonic() > deadline:
                        raise TimeoutError("concurrent Signal admission timed out")
                    time.sleep(0.01)
                key, generation, answer = handle.arrived.get(timeout=60)
                handle.consume(key, generation)
                if isinstance(answer, BaseException):
                    raise answer
                assert answer.error is None, answer.error
                answers.append({key: value for key, value in answer.run.items() if key not in {"child_total_ms", "additive_ms"}})
            return answers
        finally:
            handle.close()

    samples = []
    async def sample() -> None:
        while True:
            samples.append({**pool.facts(), "memory_bytes": cgroup_bytes("memory.current"), "pids": cgroup_bytes("pids.current")})
            await asyncio.sleep(0.1)

    sampling = asyncio.create_task(sample())
    try:
        timings = {}
        signal_runs = []
        for mode in ("cold_pool", "warm_pool"):
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(run_owner, owner) for owner in range(2)]
                signal_future = executor.submit(run_signal)
                results = [await asyncio.to_thread(future.result) for future in futures]
                signal_runs.extend(await asyncio.to_thread(signal_future.result))
            timings[mode] = int((time.monotonic() - started) * 1000)
            assert all(delivery.result == expected[scenarios[int(delivery.key.rsplit("_", 1)[1]) % len(scenarios)].key]
                       for owner in results for delivery in owner)
            assert [len(owner) for owner in results] == [8, 8]
        assert all(answer == signal_runs[0] for answer in signal_runs)
        assert pool.facts()["busy_lanes"] == 0
        assert all(sample["submitted_unconsumed"] <= 2 * width for sample in samples)
        proof = await asyncio.to_thread(build_proof, source, blobs=blobs, source_data=SnapshotRunData("SPY", snapshot, blobs))
        assert proof["result_sha256"] == result_sha256(source)
        record_property("capacity_observation", json.dumps({"width": width, "fixture": "synthetic_eight_leg_reentry",
            "capacity_qualified": False, "timings_ms": timings, "retained_snapshot_bytes": retained,
            "evidence_bytes_per_scenario": {key: len(evidence_payload(result)) for key, result in expected.items()},
            "complete_source_and_proof_digest": proof["result_sha256"],
            "peak_samples": {field: max((sample[field] for sample in samples if sample[field] is not None), default=None)
                             for field in ("memory_bytes", "pids", "payload_bytes", "result_bytes", "owners")}}))
    finally:
        sampling.cancel()
        await asyncio.gather(sampling, return_exceptions=True)
        pool.close()
