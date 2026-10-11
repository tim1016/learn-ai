"""The benchmark corpus and harness are frozen (#3034 slice 1).

The smoke check regenerates the corpus generator's manifest over a seeded
fixture lake and executes it on the serial path, pinning the identity
count, the fold split, determinism (same seeds → same manifest), the
hashes of one fixture run, and the additive-timing contract. Any drift in
an input identity, an expected hash or a count fails here, long before an
operator fires the full baseline.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.research.benchmark import (
    CORPUS_FOLD_COUNT,
    EXECUTION_MODES,
    PARAMETER_GROUPS,
    TIMING_KINDS,
    BaselineReport,
    build_corpus,
    connection_inventory,
    fold_windows,
    protocol_width_histogram,
    run_baseline,
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
    seed_lake_window(root, SYMBOL, _weekdays(DEVELOPMENT), count=390)
    return root


def _weekdays(window: tuple[date, date]) -> list[date]:
    from app.lean_sidecar.trading_calendar import expected_sessions

    return list(expected_sessions(*window))


def test_the_full_corpus_freezes_exactly_1000_distinct_identities_with_the_200_800_split() -> None:
    corpus = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)

    assert len(corpus.identities) == 1000
    assert len({item.evaluation_key for item in corpus.identities}) == 1000
    assert sum(1 for item in corpus.identities if item.window_kind == "full-development") == 200
    assert sum(1 for item in corpus.identities if item.window_kind == "fold") == 800
    assert len(corpus.fold_windows) == CORPUS_FOLD_COUNT


def test_the_same_seeds_regenerate_the_same_manifest(tmp_path: Path, seeded_lake_catalog) -> None:
    """The freeze is a function of its inputs: count the manifest twice, hash it twice."""
    first = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)
    second = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)

    assert first.digest == second.digest
    assert json.loads(json.dumps(first.as_dict())) == json.loads(json.dumps(second.as_dict()))


def test_the_corpus_spans_declared_groups_with_both_trade_expectations() -> None:
    corpus = build_corpus(symbol=SYMBOL, development=DEVELOPMENT, data_snapshot_digest="d" * 64)

    groups = {item.group for item in corpus.identities}
    assert groups == {group.label for group in PARAMETER_GROUPS}
    expectations = {item.expectation for item in corpus.identities}
    assert expectations == {"trade-heavy", "no-trade"}


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
    report = run_baseline(
        roots=[lake],
        symbol=SYMBOL,
        development=DEVELOPMENT,
        repetitions=1,
        full_development_count=6,
        fold_count=6,
        folds=2,
        data_policy=RAW_DATA_POLICY,
    )

    assert report.execution_mode == "serial-original"
    assert report.cache_hits == 0
    assert len(report.runs) == 12
    assert all(run.expectation_held for run in report.runs), "the freeze-time trade expectation must reconcile"
    assert report.medians_ms["total"] > 0
    # The additive spans the serial path can observe carry timings; the
    # nested ones are the later executors' fields and stay None here.
    assert TIMING_KINDS["fetch"] == "additive" and TIMING_KINDS["admission"] == "nested"
    assert all(run.nested_ms == {name: None for name in ("admission", "decoding", "queue", "ipc")} for run in report.runs)


def test_the_smoke_baseline_is_deterministic_and_hash_pinned(lake) -> None:
    """One run's hashes, pinned: a changed engine projection, trade log or metric fails here."""

    def once() -> BaselineReport:
        return run_baseline(
            roots=[lake],
            symbol=SYMBOL,
            development=DEVELOPMENT,
            repetitions=1,
            full_development_count=6,
            fold_count=6,
            folds=2,
            data_policy=RAW_DATA_POLICY,
        )

    first, second = once(), once()
    runs_a = {run.evaluation_key: run for run in first.runs}
    runs_b = {run.evaluation_key: run for run in second.runs}

    assert runs_a.keys() == runs_b.keys()
    for key, run in runs_a.items():
        assert run.result_hash == runs_b[key].result_hash
        assert run.trade_log_hash == runs_b[key].trade_log_hash
        assert run.metrics_hash == runs_b[key].metrics_hash

    # Pinned fixture hashes: the run's identity fields move with the code
    # (the evaluation key folds the code identity in), but its hashes are
    # data-only. A changed engine projection, trade log or metric fails the
    # quick tier with exactly these two lines to point at.
    typical = next(run for run in first.runs if run.group == "sma-typical" and run.window_kind == "full-development")
    assert (typical.trades, typical.bars_scored) == (350, 15210)
    assert typical.trade_log_hash == "72ddf6ba3706e5ee04c94fb343affaff8ea1a9b1b477690a623416b9dc984bf5"
    assert typical.metrics_hash == "7a345affa257c4877078fa815f9af77610500c780f9a20b24a57d9ec362de598"
    no_trade = next(run for run in first.runs if run.group == "sma-no-trade")
    assert no_trade.trades == 0
    # The empty trade log's own hash: the no-trade projection is pinned too,
    # not merely "less than some trades".
    assert no_trade.trade_log_hash == "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


def test_the_report_distinguishes_the_modes_and_labels_the_future_ones(lake) -> None:
    report = run_baseline(
        roots=[lake],
        symbol=SYMBOL,
        development=DEVELOPMENT,
        repetitions=1,
        full_development_count=2,
        fold_count=2,
        folds=1,
        execution_mode="serial-decoded-cache",
        data_policy=RAW_DATA_POLICY,
    )

    assert report.as_dict()["modes_distinguished"] == list(EXECUTION_MODES)
    assert any("serial-decoded-cache" in note and "future executor" in note for note in report.notes)


def test_the_connection_inventory_reports_and_never_repairs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_SERVER_MAX_CONNECTIONS", raising=False)
    inventory = connection_inventory()

    assert inventory["research_cap"] == 12
    assert inventory["required_free"] == 20
    assert inventory["postgres_server_max_connections"] == 100
    assert inventory["server_max_is_assumed"] is True
    assert inventory["reserved_by_configured_pools"] == 20
    assert inventory["free_at_server_without_research"] == 80
    assert inventory["free_after_research_cap"] == 68
    assert inventory["meets_required_free"] is True and inventory["mismatch"] is None

    squeezed = connection_inventory(research_cap=95)
    assert squeezed["free_after_research_cap"] < 0
    assert squeezed["meets_required_free"] is False
    assert squeezed["mismatch"] is not None and "research cap of 95" in squeezed["mismatch"]


def test_an_under_provisioned_server_is_reported_as_a_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_SERVER_MAX_CONNECTIONS", "25")
    inventory = connection_inventory()

    assert inventory["meets_required_free"] is False
    assert inventory["mismatch"] is not None and "below the required 20" in inventory["mismatch"]


def test_the_width_histogram_reconciles_and_names_drift() -> None:
    """The frozen widths agree with the stop accounting on a consistent stage and read False on drift."""
    trials = [
        {"stage": "search", "kind": "round", "fold_index": None, "payload": {
            "pass_index": 0, "knob": "gap", "round_index": 0,
            "values": [0.1, 0.2, 0.3], "invalid": [[0.2, "outside domain"]],
            "results": [[0.1, None], [0.3, "NO_TRADES"]],
        }},
        {"stage": "validation", "kind": "round", "fold_index": 0, "payload": {
            "pass_index": 0, "knob": "gap", "round_index": 0,
            "values": [0.4], "invalid": [], "results": [[0.4, None]],
        }},
        {"stage": "search", "kind": "stop", "fold_index": None, "payload": {
            "stop_reason": "quantization", "winner_hash": "h", "window": [0, 1],
            "evaluated": 1, "cached": 1, "invalid": 1,
        }},
    ]

    histogram = protocol_width_histogram(trials, stage="search")

    assert [row["round_index"] for row in histogram["rounds"]] == [0], "other stages' rounds do not enter the histogram"
    assert histogram["totals"]["sampled"] == 3
    assert histogram["totals"]["invalid"] == 1
    assert histogram["totals"]["testable"] == 2
    assert histogram["totals"]["eligible"] == 1
    assert histogram["accounting"] == {"newly_reserved": 1, "cached_or_duplicate": 1, "invalid": 1, "ready": 2}
    assert histogram["totals"]["reconciled"] is True, "the rounds' invalid total agrees with the stop accounting"

    from copy import deepcopy

    drifted = deepcopy(trials)
    drifted[-1]["payload"]["invalid"] = 2
    assert protocol_width_histogram(drifted, stage="search")["totals"]["reconciled"] is False, (
        "a drifted invalid count must read False, not pass silently"
    )
    drifted_round = deepcopy(trials)
    drifted_round[0]["payload"]["invalid"] = []
    assert protocol_width_histogram(drifted_round, stage="search")["totals"]["reconciled"] is False, (
        "rounds that lost an invalid record must read False against the stop accounting"
    )
