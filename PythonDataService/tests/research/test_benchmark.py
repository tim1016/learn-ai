"""The benchmark corpus and harness are frozen (#3034 slice 1).

The smoke check regenerates the corpus generator's manifest over a seeded
fixture lake and executes a scaled-down corpus on the serial path, pinning
the identity count, the fold split, determinism (same seeds → same
manifest), the hashes of one fixture run, and the additive-timing contract.
Any drift in an input identity, an expected hash or a count fails here,
long before an operator fires the full baseline. The smoke stays small on
purpose: CI's quick tier runs it on every PR inside a hard time budget, and
the full 1,000-identity baseline is the operator's manual run (#3036).
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.research.benchmark import (
    CORPUS_FOLD_COUNT,
    EXECUTION_MODES,
    PARAMETER_GROUPS,
    TIMING_KINDS,
    BaselineExecutionError,
    BaselineReport,
    IdentityRun,
    _summarize,
    build_corpus,
    connection_inventory,
    fold_windows,
    protocol_width_histogram,
    run_baseline,
    summarize_report,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.lake_fixture import seed_lake_window

DEVELOPMENT = (date(2026, 1, 2), date(2026, 2, 27))
SYMBOL = "SPY"


# The fixture lake seeds the raw root only, so the smoke runs ask for raw
# bars — the adjustment seam has its own suites.
RAW_DATA_POLICY = {
    "source": "polygon",
    "symbol": SYMBOL,
    "adjusted": False,
    "session": "regular",
    "input_bars": {"timespan": "minute", "multiplier": 1},
    "strategy_bars": {"timespan": "minute", "multiplier": 1},
}


@pytest.fixture
def lake(tmp_path: Path, seeded_lake_catalog, monkeypatch) -> Path:
    """A seeded, admitted local lake: the corpus never contacts a provider or broker."""
    from app.config import settings
    from app.data_lake.path_policy import lake_subpath

    write_root = tmp_path / "writer-root"
    root = write_root / lake_subpath("raw")
    root.mkdir(parents=True)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(write_root))
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(write_root))  # the spawned workers read env, not the object
    seed_lake_window(root, SYMBOL, _weekdays(DEVELOPMENT), count=390)
    return root


def _weekdays(window: tuple[date, date]) -> list[date]:
    from app.lean_sidecar.trading_calendar import expected_sessions

    return list(expected_sessions(*window))


def _smoke_baseline(lake: Path, **overrides: Any) -> BaselineReport:
    """The scaled-down corpus the quick tier can afford: 4 identities, raw bars."""
    return run_baseline(
        roots=[lake],
        symbol=SYMBOL,
        development=DEVELOPMENT,
        repetitions=1,
        full_development_count=2,
        fold_count=2,
        folds=2,
        data_policy=RAW_DATA_POLICY,
        **overrides,
    )


def test_the_corpus_freezes_its_identities_groups_and_data_policy() -> None:
    corpus = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)

    assert len(corpus.identities) == 1000, "the full corpus freezes exactly 1000 identities"
    assert len({item.evaluation_key for item in corpus.identities}) == 1000, "the 1000 identities are distinct"
    assert sum(1 for item in corpus.identities if item.window_kind == "full-development") == 200, "200 full-development windows"
    assert sum(1 for item in corpus.identities if item.window_kind == "fold") == 800, "800 fold windows"
    assert len(corpus.fold_windows) == CORPUS_FOLD_COUNT, "the corpus carries every fold window"
    groups = {item.group for item in corpus.identities}
    assert groups == {group.label for group in PARAMETER_GROUPS}, "the corpus spans the declared groups"
    expectations = {item.expectation for item in corpus.identities}
    assert expectations == {"trade-heavy", "no-trade"}, "the corpus carries both trade expectations"
    # A raw run's keys must claim raw bytes: the policy is part of the context digest.
    raw = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64, data_policy=RAW_DATA_POLICY)
    assert corpus.context_digest != raw.context_digest, "the data policy is part of the context digest"
    assert {item.evaluation_key for item in corpus.identities}.isdisjoint(
        {item.evaluation_key for item in raw.identities}
    ), "a raw run's evaluation keys never collide with the adjusted corpus's"


def test_the_same_seeds_regenerate_the_same_manifest(tmp_path: Path, seeded_lake_catalog) -> None:
    """The freeze is a function of its inputs: count the manifest twice, hash it twice."""
    first = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)
    second = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)

    assert first.digest == second.digest
    assert json.loads(json.dumps(first.as_dict())) == json.loads(json.dumps(second.as_dict()))


def test_the_default_decoded_cache_bound_leaves_room_inside_the_container() -> None:
    """The default bound is half the deployed python-service cgroup limit, never all of it (#3060 review)."""
    from app.research.benchmark import DECODED_CACHE_DEFAULT_BYTES

    assert DECODED_CACHE_DEFAULT_BYTES <= 1024**3


def test_the_default_corpus_policy_is_a_complete_engine_wire_policy() -> None:
    """The frozen default must run without an operator-named policy — the CLI's own path.

    The default once carried a source the engine's policy block rejects, so
    ``run_baseline`` with no explicit policy crashed on the first identity's
    request; this pins the completed default as engine-executable and the
    cold-window claim as exactly what the request executes (#3036 review).
    """
    from app.research.benchmark import CORPUS_DATA_POLICY, _assumptions_dict, _engine_request

    corpus = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)
    request = _engine_request(
        corpus.identities[0], execution=_assumptions_dict(), data_policy={**CORPUS_DATA_POLICY, "symbol": SYMBOL}
    )
    assert request.data_policy is not None and request.data_policy.source == "polygon"
    # The corpus claims COLD windows and executes cold: an unprimed request
    # is the claim, not an oversight — the never-ready no-trade group only
    # scores over an unprimed window (#3036 review).
    assert request.warmup_from_date is None


def test_fold_windows_are_calendar_derived_and_tile_the_development_window() -> None:
    windows = fold_windows(DEVELOPMENT, CORPUS_FOLD_COUNT)

    assert [index for _, index in windows] == list(range(CORPUS_FOLD_COUNT))
    ordered = [window for window, _ in windows]
    assert ordered == sorted(ordered), "folds run in calendar order"
    assert all(ordered[index][1] <= ordered[index + 1][0] for index in range(len(ordered) - 1)), "folds are disjoint"
    # Every calendar session falls in exactly one fold: the tiles cover the
    # window without skipping or doubling a session.
    from app.lean_sidecar.trading_calendar import expected_sessions

    sessions = expected_sessions(*DEVELOPMENT)
    covered = sum(
        sum(1 for session in sessions if window[0] <= et_midnight_ms(session) < window[1]) for window in ordered
    )
    assert covered == len(sessions)


def test_the_smoke_baseline_runs_and_reconciles_its_expectations(lake) -> None:
    report = _smoke_baseline(lake)

    assert report.execution_mode == "serial-original"
    assert report.cache_hits == 0
    assert len(report.runs) == 4
    assert all(run.expectation_held for run in report.runs), "the freeze-time trade expectation must reconcile"
    assert report.medians_ms["total"] > 0
    # The additive spans the serial path can observe carry timings; the
    # nested ones are the later executors' fields and stay None here.
    assert TIMING_KINDS["fetch"] == "additive" and TIMING_KINDS["admission"] == "nested"
    assert all(run.nested_ms == {name: None for name in ("admission", "decoding", "queue", "ipc")} for run in report.runs)
    # The machine-readable report carries the projections themselves —
    # ordered trades and the curve — beside their hashes, and the
    # valid-new-evaluation throughput the issue asks for.
    as_dict = report.as_dict()
    assert as_dict["modes_distinguished"] == {"execution": list(EXECUTION_MODES), "timing": ["warm", "preparation-inclusive"]}
    trade_heavy = next(run for run in report.runs if run.trades > 0)
    assert len(trade_heavy.ordered_trades) == trade_heavy.trades and trade_heavy.equity_curve
    assert as_dict["runs"][0]["ordered_trades"] is not None
    assert report.throughput["valid_new_evaluations"] == 4
    assert report.throughput["evaluations_per_second"] > 0


def test_the_smoke_baseline_is_deterministic_and_hash_pinned(lake) -> None:
    """Two runs' hashes, pinned: a changed engine projection, trade log or metric fails here."""
    first, second = _smoke_baseline(lake), _smoke_baseline(lake)
    runs_a = {run.evaluation_key: run for run in first.runs}
    runs_b = {run.evaluation_key: run for run in second.runs}

    assert runs_a.keys() == runs_b.keys()
    for key, run in runs_a.items():
        assert run.result_hash == runs_b[key].result_hash
        assert run.trade_log_hash == runs_b[key].trade_log_hash
        assert run.metrics_hash == runs_b[key].metrics_hash
        assert run.ordered_trades == runs_b[key].ordered_trades
        assert run.equity_curve == runs_b[key].equity_curve

    # Pinned fixture hashes: the run's identity fields move with the code
    # (the evaluation key folds the code identity in), but its hashes are
    # data-only. A changed engine projection, trade log or metric fails the
    # quick tier with exactly these two lines to point at.
    typical = next(run for run in first.runs if run.group == "sma-typical" and run.window_kind == "full-development")
    assert (typical.trades, typical.bars_scored) == (350, 15210)
    assert typical.trade_log_hash == "72ddf6ba3706e5ee04c94fb343affaff8ea1a9b1b477690a623416b9dc984bf5"
    # PR #3300 removes the null mae_pct/mfe_pct placeholders; every retained
    # metric and the trade log match the previous pinned projection exactly.
    assert typical.metrics_hash == "ac20af8dd03617e93b0baab55e414637503e8fd8804cd9f39fec359df4a55b77"
    no_trade = next(run for run in first.runs if run.group == "sma-no-trade")
    assert no_trade.trades == 0
    # The empty trade log's own hash: the no-trade projection is pinned too,
    # not merely "less than some trades".
    assert no_trade.trade_log_hash == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def test_the_engine_reads_are_bound_to_the_captured_snapshot(lake) -> None:
    """The manifest the engine reads under is the snapshot's own receipts, run by run."""
    manifests: list[dict[str, str] | None] = []

    def capture_execute(request: Any, on_phase: Any, data_manifest: Any) -> Any:
        manifests.append(dict(data_manifest) if data_manifest is not None else None)

        class _Response:
            success = True
            trades = []
            equity_curve = [{"t": 1, "equity": 1.0}]
            statistics = {"net_profit": 0.0}

        return _Response()

    report = _smoke_baseline(lake, execute=capture_execute)

    assert len(manifests) == len(report.runs)
    expected = report.environment["data_artifact_checksums"]
    assert expected, "the fixture lake's snapshot carries receipts"
    assert all(manifest == expected for manifest in manifests), "every run executes under the snapshot's manifest"


def test_a_failed_evaluation_is_rejected_never_recorded_as_a_no_trade_result(lake) -> None:
    """``success=False`` raises: a failed run has no projection to hash, and a no-trade expectation must not absorb it."""
    from app.research.benchmark import run_identity

    class _FailedResponse:
        success = False
        error = "engine exploded"

    corpus = build_corpus(
        symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64,
        data_policy=RAW_DATA_POLICY, full_development_count=1, fold_count=0, folds=1,
    )

    with pytest.raises(BaselineExecutionError, match="engine exploded"):
        run_identity(
            corpus.identities[0], data_policy=RAW_DATA_POLICY,
            execute=lambda request, on_phase, manifest: _FailedResponse(),
        )


def test_future_modes_are_refused_until_the_executor_that_measures_them_exists(lake) -> None:
    """No selectable branch without behavior: the runtime modes exist since #3053; the rest still refuse."""
    with pytest.raises(ValueError, match="execution mode"):
        _smoke_baseline(lake, execution_mode="pooled-3")  # widths outside the qualified 1/2/4/8 ladder
    with pytest.raises(ValueError, match="execution mode"):
        _smoke_baseline(lake, execution_mode="serial-turbo")
    with pytest.raises(ValueError, match="positive"):
        _smoke_baseline(lake, execution_mode="serial-decoded-cache", decoded_cache_byte_bound=0)


def test_serial_decoded_cache_reuses_overlap_and_matches_the_serial_originals_hashes(lake) -> None:
    """#3038: different identities reuse overlapping artifacts; the answers do not move.

    The cache mode runs the same corpus through one shared decoded-artifact
    cache. Even at one repetition, distinct identities read overlapping
    windows — a full-development run's days are re-read by the next — so real
    reuse occurs, every artifact still admits per read, and each evaluation's
    scientific projection is byte-identical to the serial original's.
    """
    plain = _smoke_baseline(lake)
    cached = _smoke_baseline(lake, execution_mode="serial-decoded-cache")

    assert plain.decoded_cache_metrics is None
    metrics = cached.decoded_cache_metrics
    assert metrics is not None and metrics["misses"] > 0
    assert metrics["hits"] > 0, "overlapping-but-different identities reused shared artifacts"
    assert metrics["uncached_decodes"] == 0
    assert metrics["resident_bytes"] <= metrics["max_bytes"]
    assert "decoded_cache" in cached.as_dict() and "decoded_cache" not in plain.as_dict()
    assert "decoded cache:" in summarize_report(cached)

    def hashes(report: BaselineReport) -> dict[str, tuple[str, str, str]]:
        return {
            run.evaluation_key: (run.result_hash, run.trade_log_hash, run.metrics_hash)
            for run in report.runs
        }

    assert hashes(cached) == hashes(plain), "the cached mode is the serial original, hash for hash"


def test_the_median_is_true_and_variability_pools_only_comparable_repetitions() -> None:
    """[1, 3] medians to 2 (never the upper middle), and only same-key repetitions mix."""
    one = IdentityRun(evaluation_key="a", group="g", window_kind="fold", repetition=0, total_ms=1.0)
    one_again = IdentityRun(evaluation_key="a", group="g", window_kind="fold", repetition=1, total_ms=3.0)
    other = IdentityRun(evaluation_key="b", group="g", window_kind="fold", repetition=0, total_ms=100.0)
    other_again = IdentityRun(evaluation_key="b", group="g", window_kind="fold", repetition=1, total_ms=100.0)

    medians, variability = _summarize([one, one_again, other, other_again])

    # Per key: a's repetitions [1, 3] median to 2.0 (the true middle pair
    # mean — the old code picked the upper value 3), b's are constant at
    # 100.0; the report is the median of the per-key medians, never a pool
    # of all four samples.
    assert medians["total"] == 51.0  # median of [2.0, 100.0]
    assert variability["total"] == 1.0  # median of the per-key spreads [2.0, 0.0]


def test_the_connection_inventory_reports_and_never_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_SERVER_MAX_CONNECTIONS", raising=False)
    inventory = connection_inventory()

    assert inventory["research_cap"] == 12
    assert inventory["required_free"] == 20
    # Every deployed pool is counted from its own source: the coordinator's
    # two loops, the Backend's Npgsql default, and the fleet's clerk lanes.
    names = {pool["name"] for pool in inventory["pools"]}
    assert any("python-service" in name for name in names)
    assert any("backend" in name for name in names)
    assert any("clerk" in name for name in names)
    assert inventory["reserved_by_configured_pools"] == 20 + 100 + 10 + 10
    assert inventory["vm_limits"] and "db" in inventory["vm_limits"], "VM maxima come from the fleet topology"
    # An unknown deployed server maximum leaves the verdict unverified —
    # never a pass guessed against an assumed limit.
    assert inventory["postgres_server_max_connections"] is None
    assert inventory["verified"] is False
    assert inventory["meets_required_free"] is None and inventory["mismatch"] is None
    assert any("POSTGRES_SERVER_MAX_CONNECTIONS" in reason for reason in inventory["unverified_reasons"])

    monkeypatch.setenv("POSTGRES_SERVER_MAX_CONNECTIONS", "300")
    verified = connection_inventory()
    assert verified["verified"] is True
    assert verified["free_at_server_without_research"] == 300 - 140
    assert verified["free_after_research_cap"] == 300 - 140 - 12
    assert verified["meets_required_free"] is True and verified["mismatch"] is None


def test_an_under_provisioned_server_is_reported_as_a_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_SERVER_MAX_CONNECTIONS", "150")
    inventory = connection_inventory()

    assert inventory["verified"] is True
    assert inventory["free_after_research_cap"] == 150 - 140 - 12
    assert inventory["meets_required_free"] is False
    assert inventory["mismatch"] is not None and "research cap of 12" in inventory["mismatch"]


def _stage_trials() -> list[dict[str, Any]]:
    """A validation stage with two fold procedures, each with its own rounds and stop."""
    def round_payload(values: list[float], invalid: list[list[Any]], results: list[list[Any]], **widths: int) -> dict[str, Any]:
        return {
            "pass_index": 0, "knob": "gap", "round_index": 0,
            "values": values, "invalid": invalid, "results": results,
            "objectives": [], "chosen": values[0], "moved": True, "current_before": values[0],
            "quantization_limit": False, **widths,
        }

    return [
        {"stage": "search", "kind": "round", "fold_index": None, "payload": round_payload(
            [0.1, 0.2, 0.3], [[0.2, "outside domain"]], [[0.1, None], [0.3, "NO_TRADES"]], submitted=2, first_evaluated=1)},
        {"stage": "validation", "kind": "round", "fold_index": 0, "payload": round_payload(
            [0.4], [], [[0.4, None]], submitted=1, first_evaluated=1)},
        {"stage": "validation", "kind": "round", "fold_index": 1, "payload": round_payload(
            [0.5, 0.6], [[0.5, "outside domain"]], [[0.6, None]], submitted=1, first_evaluated=0)},
        {"stage": "validation", "kind": "stop", "fold_index": 0, "payload": {
            "stop_reason": "quantization", "winner_hash": "h0", "window": [0, 1], "evaluated": 1, "cached": 0, "invalid": 0}},
        {"stage": "validation", "kind": "stop", "fold_index": 1, "payload": {
            "stop_reason": "quantization", "winner_hash": "h1", "window": [2, 3], "evaluated": 1, "cached": 1, "invalid": 1}},
    ]


def test_the_width_histogram_keys_its_accounting_by_stage_and_fold() -> None:
    """Every fold procedure keeps its own stop accounting; no fold's counts overwrite another's."""
    histogram = protocol_width_histogram(_stage_trials(), stage="validation")

    assert [fold["fold_index"] for fold in histogram["folds"]] == [0, 1], "the full-development fold is absent here"
    fold_zero, fold_one = histogram["folds"]
    assert fold_zero["accounting"] == {"newly_reserved": 1, "cached_or_duplicate": 0, "invalid": 0, "ready": 1}
    assert fold_one["accounting"] == {"newly_reserved": 1, "cached_or_duplicate": 1, "invalid": 1, "ready": 2}
    assert fold_zero["totals"]["reconciled"] is True
    assert fold_one["totals"]["reconciled"] is True
    # Per-round widths carry the cached/newly-reserved/ready accounting
    # captured at the procedure source, including the cached repeat.
    assert fold_zero["rounds"][0]["newly_reserved"] == 1
    assert fold_zero["rounds"][0]["cached_or_duplicate"] == 0
    assert fold_zero["rounds"][0]["ready"] == 1
    assert fold_one["rounds"][0]["newly_reserved"] == 0
    assert fold_one["rounds"][0]["cached_or_duplicate"] == 1
    assert fold_one["rounds"][0]["ready"] == 1
    assert histogram["totals"]["sampled"] == 3 and histogram["totals"]["invalid"] == 1
    # Other stages' rounds never enter the histogram.
    search = protocol_width_histogram(_stage_trials(), stage="search")
    assert [fold["fold_index"] for fold in search["folds"]] == [None]
    assert search["totals"]["sampled"] == 3


def test_a_round_stored_before_the_width_capture_reads_absent_not_zero() -> None:
    trials = deepcopy(_stage_trials())
    for trial in trials:
        if trial["kind"] == "round" and trial["fold_index"] == 0:
            trial["payload"].pop("submitted")
            trial["payload"].pop("first_evaluated")

    histogram = protocol_width_histogram(trials, stage="validation")

    assert histogram["folds"][0]["rounds"][0]["newly_reserved"] is None
    assert histogram["folds"][0]["rounds"][0]["cached_or_duplicate"] is None
    assert histogram["folds"][0]["rounds"][0]["ready"] is None
    assert histogram["folds"][1]["rounds"][0]["ready"] == 1


def test_the_width_histogram_names_drift_per_fold() -> None:
    """A fold's drifted stop count reads False against that fold's rounds, not another's."""
    drifted = deepcopy(_stage_trials())
    drifted[3]["payload"]["invalid"] = 5  # fold 0's stop now disagrees with fold 0's rounds

    histogram = protocol_width_histogram(drifted, stage="validation")

    assert histogram["folds"][0]["totals"]["reconciled"] is False
    assert histogram["folds"][1]["totals"]["reconciled"] is True

    drifted_round = deepcopy(_stage_trials())
    drifted_round[2]["payload"]["invalid"] = []  # fold 1's round lost its invalid record
    histogram = protocol_width_histogram(drifted_round, stage="validation")
    assert histogram["folds"][1]["totals"]["reconciled"] is False, (
        "rounds that lost an invalid record must read False against the stop accounting"
    )


# ── The pooled executor (#3053) ───────────────────────────────────────────


@pytest.mark.slow
def test_the_pooled_executor_matches_the_serial_originals_hashes_and_reports_ipc(lake) -> None:
    """The runtime-measured corpus is the serial corpus, hash for hash, with nested spans named."""
    serial = _smoke_baseline(lake)
    pooled = _smoke_baseline(lake, execution_mode="pooled-2", timing_mode="preparation-inclusive")

    by_key = {run.evaluation_key: run for run in serial.runs}
    assert len(pooled.runs) == len(serial.runs)
    for run in pooled.runs:
        twin = by_key[run.evaluation_key]
        assert run.result_hash == twin.result_hash, f"evaluation {run.evaluation_key} moved over the wire"
        assert run.trades == twin.trades and run.expectation_held
        assert run.nested_ms["ipc"] is not None, "queue/IPC attributed parent-side"
    assert pooled.throughput["valid_new_evaluations"] == len(pooled.runs)
    assert any("pooled executor" in note for note in (pooled.notes or []))


@pytest.mark.slow
def test_the_same_resource_serial_baseline_is_the_one_worker_runtime(lake) -> None:
    """serial-same-resource runs the #3044 shape: one spawned worker, warm timing, serial answers."""
    serial = _smoke_baseline(lake)
    same_resource = _smoke_baseline(lake, execution_mode="serial-same-resource")

    by_key = {run.evaluation_key: run for run in serial.runs}
    for run in same_resource.runs:
        assert run.result_hash == by_key[run.evaluation_key].result_hash
