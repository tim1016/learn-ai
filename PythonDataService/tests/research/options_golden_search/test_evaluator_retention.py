"""A complete candidate batch retains bounded replay graphs and restores details from blobs."""
from __future__ import annotations

import gc
import weakref
from decimal import Decimal

import pytest

from app.options_backtest.setup import OptionsBacktestSetup
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search import repository as repo
from app.research.options_golden_search import stages
from app.research.options_golden_search.evaluator import evaluate_setup
from app.research.options_golden_search.protocol import Knob
from app.research.options_golden_search.snapshots import Snapshot
from tests.options_backtest._run_data import WED
from tests.research.options_golden_search._evidence import scene
from tests.research.options_golden_search.test_evaluator import _window
from tests.research.options_golden_search.test_protocol_search import protocol


@pytest.mark.parametrize("missing_option_day", [False, True])
def test_large_batch_releases_replay_graphs_and_loads_selected_exact_evidence(
    tmp_path, monkeypatch: pytest.MonkeyPatch, missing_option_day: bool,
) -> None:
    from app.services import research_runtime_client

    setup, data = scene((WED,), exits=("2",))
    if missing_option_day:
        data._options.clear()
    window = _window((WED,))
    snapshot = Snapshot(manifest={}, absent=(), retained_bytes=0)
    plan = protocol().model_copy(update={"setup": setup, "snapshot_id": snapshot.snapshot_id, "knobs": (Knob(path=f"legs.{setup.legs[0].leg_id}.stop_loss", values=(None, {"kind": "percent", "value": "10"})),)})
    row = {"id": "memory-study", "attempt": 1, "phase": "research", "protocol": plan.model_dump(mode="json"),
           "protocol_hash": plan.protocol_hash, "snapshot": snapshot.model_dump(mode="json")}
    records = {}
    identities = {}
    reservations = 0
    live = 0
    peak = 0
    computations = 0

    def released():
        nonlocal live
        live -= 1

    def replay(*args, **kwargs):
        nonlocal live, peak, computations
        computations += 1
        result = evaluate_setup(args[0], args[1], data)
        live += 1
        peak = max(peak, live)
        weakref.finalize(result, released)
        return result

    def operation(func, *args, **kwargs):
        return func, args, kwargs

    def run(operation):
        nonlocal reservations
        func, args, _kwargs = operation
        if func is repo.reserve:
            key = args[2]
            reservations += 1
            identities[key] = args[3]
            return records.setdefault(key, None)
        if func is repo.complete:
            records[args[2]] = args[3]
            return None
        if func is repo.begin_execution:
            return None
        if func is repo.counters:
            return {"reserved": len(records), "completed": sum(bool(record) for record in records.values()),
                    "execution_attempts": computations,
                    "evidence_bytes": sum(record["evidence_bytes"] for record in records.values() if record)}
        if func is repo.get_evaluation:
            if args[1] not in records:
                raise LookupError("missing")
            return {"key": args[1], "identity": identities[args[1]], "result": records[args[1]]}
        if func.__name__ == "report":
            return None
        raise AssertionError(f"unexpected all-history or persistence call: {func}")

    monkeypatch.setattr(stages, "evaluate_setup", replay)
    monkeypatch.setattr(stages, "with_connection", operation)
    monkeypatch.setattr(stages, "run_sync", run)
    monkeypatch.setattr(research_runtime_client, "is_research_runtime", lambda: False)
    evaluator = stages.StudyEvaluator(row, lambda: None, lambda done, total: None, blobs=BlobStore(tmp_path / "blobs"))
    requests = []
    for index in range(64):
        body = setup.model_dump(mode="json")
        body["legs"][0]["stop_loss"] = {"kind": "percent", "value": str(Decimal(10) + Decimal(index) / 10)}
        requests.append((OptionsBacktestSetup.model_validate(body), window, "base"))
    answers = evaluator.evaluate_batch(requests)
    gc.collect()
    assert computations == 64
    assert peak <= 2
    assert live == 0
    assert len(answers) == len(records) == 64
    assert all(set(record["metrics"]) == set(stages._SCALAR_METRICS) for record in records.values())
    assert all("daily" not in record["metrics"] for record in records.values())
    def refuse_trace(*args, **kwargs):
        raise AssertionError("Compact diagnostic projection must not load retained minute traces")
    with monkeypatch.context() as compact:
        compact.setattr(evaluator, "load", refuse_trace)
        for answer in answers:
            metrics, complete = evaluator.metrics_for(answer)
            assert metrics.net_profit_usd == answer.net_profit_usd
            assert complete is not missing_option_day
    restored = evaluator.result_for(answers[-1], window)
    expected = evaluate_setup(requests[-1][0], window, data)
    assert restored == expected
    assert bool(restored.input_failures) == missing_option_day
    assert evaluator.evaluate_batch(requests) == answers
    assert computations == 64
    evaluator.verify_only = True
    reserved_before = reservations
    assert evaluator.evaluate_batch(requests) == answers
    evaluator.on_trial(None, window)
    assert reservations == reserved_before
    body = setup.model_dump(mode="json")
    body["legs"][0]["stop_loss"] = {"kind": "percent", "value": "99"}
    with pytest.raises(ValueError, match="unrecorded"):
        evaluator.evaluate_batch([(OptionsBacktestSetup.model_validate(body), window, "base")])
    assert reservations == reserved_before
    assert computations == 64
    corrupted = records[answers[-1].evidence_id]
    corrupted["metrics_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="no longer reproduces"):
        evaluator.result_for(answers[-1], window)
