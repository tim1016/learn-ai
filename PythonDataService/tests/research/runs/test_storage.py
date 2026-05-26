"""File-backed run storage — A2 acceptance gate.

Uses ``tmp_path`` as the artifacts root so the suite is hermetic and
parallel-safe. The runner is invoked through the same fake-data path
as ``test_runner_inmemory.py``; this file's only concern is what
happens after a ``(ledger, result)`` pair leaves the runner.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.engine.strategy.spec import StrategySpec
from app.engine.strategy.spec.tests._parity_helpers import (
    FakeDataReader,
    build_minute_bars,
    closes_for_spy_ema,
)
from app.research.runs import (
    RunAlreadyExistsError,
    RunCorruptError,
    RunNotFoundError,
    RunRequest,
    list_runs,
    load_run,
    run_strategy_spec,
    save_run,
)
from app.research.runs.ledger import RunLedger


def _build_test_spec(
    *,
    fast_period: int = 5,
    slow_period: int = 10,
) -> StrategySpec:
    return StrategySpec.model_validate(
        {
            "schema_version": "1.0",
            "name": "TEST EMA crossover",
            "symbols": ["TEST"],
            "resolution": {"period_minutes": 15},
            "indicators": [
                {"id": "fast", "kind": "EMA", "period": fast_period, "source": "close"},
                {"id": "slow", "kind": "EMA", "period": slow_period, "source": "close"},
                {"id": "rsi", "kind": "RSI", "period": 14, "source": "close", "ma_type": "wilders"},
            ],
            "entry": {
                "logic": "AND",
                "conditions": [
                    {"kind": "FreshCross", "left": "fast", "right": "slow", "direction": "up"},
                    {
                        "kind": "IndicatorComparison",
                        "left": {
                            "kind": "Subtract",
                            "left": {"kind": "IndicatorRef", "indicator": "fast"},
                            "right": {"kind": "IndicatorRef", "indicator": "slow"},
                        },
                        "op": ">=",
                        "right": {"kind": "Const", "value": 0.20},
                    },
                    {"kind": "IndicatorBetween", "indicator": "rsi", "lo": 50, "hi": 70, "inclusive": True},
                ],
                "size": {"kind": "SetHoldings", "fraction": 1.0},
                "pyramiding": 1,
            },
            "position": {"kind": "EQUITY_LONG"},
            "survival": [],
            "exit": {
                "logic": "OR",
                "conditions": [{"kind": "BarsSinceEntry", "op": ">=", "value": 5}],
            },
            "diagnostics": {"snapshot_at_entry": ["fast", "slow", "rsi"]},
        }
    )


@pytest.fixture
def fake_data_factory():
    bars = build_minute_bars(closes_for_spy_ema(2000))

    def factory(symbol: str, start: date, end: date):
        return FakeDataReader(bars=bars)

    return factory


def _make_run(spec, factory, *, run_id=None, parent_run_id=None, parent_spec_hash=None):
    return run_strategy_spec(
        RunRequest(
            spec=spec,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 12, 31),
            parent_run_id=parent_run_id,
            parent_spec_hash=parent_spec_hash,
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------
def test_save_then_load_restores_ledger_and_result(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)

    run_dir = save_run(ledger, result, root=tmp_path)

    assert run_dir == tmp_path / ledger.run_id
    assert (run_dir / "ledger.json").is_file()
    assert (run_dir / "result.json").is_file()

    loaded_ledger, loaded_result = load_run(ledger.run_id, root=tmp_path)

    assert loaded_ledger.model_dump() == ledger.model_dump()
    assert loaded_result.model_dump() == result.model_dump()
    assert loaded_ledger.result_hash == ledger.result_hash
    assert loaded_ledger.trade_log_hash == ledger.trade_log_hash


def test_save_writes_canonical_json_files(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    save_run(ledger, result, root=tmp_path)

    ledger_payload = json.loads((tmp_path / ledger.run_id / "ledger.json").read_text())
    result_payload = json.loads((tmp_path / ledger.run_id / "result.json").read_text())

    assert ledger_payload["run_id"] == ledger.run_id
    assert ledger_payload["strategy_spec_hash"] == ledger.strategy_spec_hash
    assert result_payload["run_id"] == ledger.run_id
    assert ledger_payload["strategy_spec_id"]  # non-empty
    assert isinstance(result_payload["metrics"]["total_trades"], int)
    # Result has the equity curve we expect.
    assert len(result_payload["equity_curve"]) == len(result.equity_curve)


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------
def test_load_missing_run_raises(tmp_path: Path):
    # Valid UUID-hex format but no such run on disk → 404-equivalent.
    with pytest.raises(RunNotFoundError):
        load_run("deadbeefdeadbeefdeadbeefdeadbeef", root=tmp_path)


def test_load_run_with_malformed_run_id_raises_value_error(tmp_path: Path):
    """Path-traversal defense: regex is ``^[0-9a-f]{32}$``, so anything
    that isn't exactly 32 lowercase hex chars rejects fast.
    """
    bad_ids = [
        "../../../etc/passwd",
        "..",
        "/",
        "abc/../def",
        "abc def",                 # whitespace
        "../" + "a" * 30,
        "abcz",                    # 'z' not in hex alphabet
        "a" * 7,                   # below length
        "a" * 33,                  # above length
        "-" * 32,                  # hyphens — used to pass the loose regex (PR #107 round 2)
        "ABCDEFABCDEFABCDEFABCDEFABCDEFAB",  # 32 chars but uppercase
    ]
    for bad in bad_ids:
        with pytest.raises(ValueError):
            load_run(bad, root=tmp_path)


def test_save_run_with_malformed_run_id_raises_value_error(tmp_path: Path, fake_data_factory):
    """``save_run`` must reject malformed run_ids before any directory creation."""
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    poisoned = ledger.model_copy(update={"run_id": "../escape"})
    poisoned_result = result.model_copy(update={"run_id": "../escape"})

    with pytest.raises(ValueError):
        save_run(poisoned, poisoned_result, root=tmp_path)
    # Nothing escaped above the root.
    assert not (tmp_path.parent / "escape").exists()


def test_save_refuses_to_overwrite_existing_run(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    save_run(ledger, result, root=tmp_path)

    with pytest.raises(RunAlreadyExistsError):
        save_run(ledger, result, root=tmp_path)


def test_save_replace_overwrites(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    save_run(ledger, result, root=tmp_path)
    # Mutating the ledger and re-saving with replace=True should win.
    new_ledger = ledger.model_copy(update={"failure_reason": "manually overridden"})
    save_run(new_ledger, result, root=tmp_path, replace=True)

    loaded_ledger, _ = load_run(ledger.run_id, root=tmp_path)
    assert loaded_ledger.failure_reason == "manually overridden"


def test_save_rejects_run_id_mismatch(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    mismatched_result = result.model_copy(update={"run_id": "definitely-different"})

    with pytest.raises(ValueError, match="run_id"):
        save_run(ledger, mismatched_result, root=tmp_path)


def test_load_corrupt_ledger_raises(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    save_run(ledger, result, root=tmp_path)

    # Truncate the ledger to invalid JSON.
    (tmp_path / ledger.run_id / "ledger.json").write_text("{not valid json")

    with pytest.raises(RunCorruptError):
        load_run(ledger.run_id, root=tmp_path)


# ---------------------------------------------------------------------------
# Listing & filtering.
# ---------------------------------------------------------------------------
def test_list_empty_root_returns_empty(tmp_path: Path):
    assert list_runs(root=tmp_path) == []


def test_list_returns_recent_first(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger_a, result_a = _make_run(spec, fake_data_factory)
    save_run(ledger_a, result_a, root=tmp_path)

    # Stamp a clearly-later ``created_at_ms`` on the second run so the
    # ordering is unambiguous regardless of clock granularity.
    ledger_b, result_b = _make_run(spec, fake_data_factory)
    ledger_b = ledger_b.model_copy(update={"created_at_ms": ledger_a.created_at_ms + 10_000})
    save_run(ledger_b, result_b, root=tmp_path)

    listed = list_runs(root=tmp_path)
    assert [lg.run_id for lg in listed] == [ledger_b.run_id, ledger_a.run_id]


def test_list_filter_by_spec_hash(tmp_path: Path, fake_data_factory):
    spec_a = _build_test_spec(fast_period=5)
    spec_b = _build_test_spec(fast_period=6)

    ledger_a, result_a = _make_run(spec_a, fake_data_factory)
    ledger_b, result_b = _make_run(spec_b, fake_data_factory)
    save_run(ledger_a, result_a, root=tmp_path)
    save_run(ledger_b, result_b, root=tmp_path)

    listed_a = list_runs(root=tmp_path, spec_hash=ledger_a.strategy_spec_hash)
    listed_b = list_runs(root=tmp_path, spec_hash=ledger_b.strategy_spec_hash)

    assert [lg.run_id for lg in listed_a] == [ledger_a.run_id]
    assert [lg.run_id for lg in listed_b] == [ledger_b.run_id]


def test_list_filter_by_symbol(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    save_run(ledger, result, root=tmp_path)

    assert list_runs(root=tmp_path, symbol="TEST")[0].run_id == ledger.run_id
    assert list_runs(root=tmp_path, symbol="DOES_NOT_EXIST") == []


def test_list_filter_by_status(tmp_path: Path, fake_data_factory):
    """A failed run and a completed run cohabit; status filter separates them.

    The runner already produces a failed-status ledger when the data
    source raises, so we exercise that branch directly rather than
    hand-constructing one.
    """
    spec = _build_test_spec()
    completed_ledger, completed_result = _make_run(spec, fake_data_factory)
    save_run(completed_ledger, completed_result, root=tmp_path)

    def broken_factory(symbol, start, end):
        raise RuntimeError("synthetic failure")

    failed_ledger, failed_result = run_strategy_spec(
        RunRequest(spec=spec, start_date=date(2024, 1, 2), end_date=date(2024, 12, 31)),
        data_source_factory=broken_factory,
        data_root_revision="test-revision-1",
    )
    save_run(failed_ledger, failed_result, root=tmp_path)

    completed = list_runs(root=tmp_path, status="completed")
    failed = list_runs(root=tmp_path, status="failed")
    assert [lg.run_id for lg in completed] == [completed_ledger.run_id]
    assert [lg.run_id for lg in failed] == [failed_ledger.run_id]


def test_list_filter_by_parent_run_id(tmp_path: Path, fake_data_factory):
    """Phase C/D/E child runs are discoverable via parent_run_id filter."""
    spec = _build_test_spec()
    parent_ledger, parent_result = _make_run(spec, fake_data_factory)
    child_ledger, child_result = _make_run(
        spec, fake_data_factory, parent_run_id=parent_ledger.run_id
    )
    save_run(parent_ledger, parent_result, root=tmp_path)
    save_run(child_ledger, child_result, root=tmp_path)

    children = list_runs(root=tmp_path, parent_run_id=parent_ledger.run_id)
    assert [lg.run_id for lg in children] == [child_ledger.run_id]


def test_list_filter_by_since_ms(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    older_ledger, older_result = _make_run(spec, fake_data_factory)
    older_ledger = older_ledger.model_copy(update={"created_at_ms": 1_700_000_000_000})
    newer_ledger, newer_result = _make_run(spec, fake_data_factory)
    newer_ledger = newer_ledger.model_copy(update={"created_at_ms": 1_800_000_000_000})

    save_run(older_ledger, older_result, root=tmp_path)
    save_run(newer_ledger, newer_result, root=tmp_path)

    cutoff = 1_750_000_000_000
    listed = list_runs(root=tmp_path, since_ms=cutoff)
    assert [lg.run_id for lg in listed] == [newer_ledger.run_id]


def test_list_limit_truncates(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    for i in range(3):
        ledger, result = _make_run(spec, fake_data_factory)
        ledger = ledger.model_copy(update={"created_at_ms": 1_700_000_000_000 + i})
        save_run(ledger, result, root=tmp_path)

    listed = list_runs(root=tmp_path, limit=2)
    assert len(listed) == 2


def test_list_skips_corrupt_ledger(tmp_path: Path, fake_data_factory, caplog):
    """A single broken ledger should not blind the rest of the listing,
    and the corruption must be loud in the logs — silent skip would
    let a refactor that drops the warning slip past CI.

    After the seam migration the corrupt-skip warning fires from
    ``app.research.artifact.store`` rather than this phase's
    ``storage`` module — but it still carries the ``[RUNS]`` prefix
    the descriptor declares via ``log_tag="RUNS"`` so operator grep
    patterns are preserved. Two debris shapes are exercised: a
    directory whose name doesn't match the id_pattern (the store's
    pre-filter rejects it) and a directory with a valid id-shaped
    name but an unparseable ledger.json (the store's parse step
    rejects it).
    """
    import logging

    spec = _build_test_spec()
    good_ledger, good_result = _make_run(spec, fake_data_factory)
    save_run(good_ledger, good_result, root=tmp_path)

    # Debris #1: name doesn't match the 32-hex id_pattern — the
    # store's pre-filter rejects it before reading the file.
    bad_name_dir = tmp_path / "corrupt-run"
    bad_name_dir.mkdir()
    (bad_name_dir / "ledger.json").write_text("{not valid json")
    (bad_name_dir / "result.json").write_text("{}")

    # Debris #2: name passes the regex but ledger.json is unparseable
    # — the store's parse step rejects it. This is the path that
    # produces the legacy ``[RUNS] skipping corrupt ledger`` log
    # message (now emitted from ``app.research.artifact.store``).
    bad_payload_dir = tmp_path / ("c" * 32)
    bad_payload_dir.mkdir()
    (bad_payload_dir / "ledger.json").write_text("{not valid json")
    (bad_payload_dir / "result.json").write_text("{}")

    with caplog.at_level(logging.WARNING):
        listed = list_runs(root=tmp_path)
    assert [lg.run_id for lg in listed] == [good_ledger.run_id]
    assert any(
        rec.message.startswith("[RUNS]") and "skipping corrupt" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Atomic-write guarantee.
# ---------------------------------------------------------------------------
def test_atomic_write_leaves_no_tmp_files_on_success(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    run_dir = save_run(ledger, result, root=tmp_path)

    leftover_tmp = list(run_dir.glob("*.tmp"))
    assert leftover_tmp == []


def test_save_creates_directory_recursively(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)

    deep_root = tmp_path / "deeply" / "nested" / "runs"
    save_run(ledger, result, root=deep_root)

    loaded_ledger, _ = load_run(ledger.run_id, root=deep_root)
    assert loaded_ledger.run_id == ledger.run_id


# ---------------------------------------------------------------------------
# Suppress unused-import warning in some test runners by referencing.
# ---------------------------------------------------------------------------
_ = RunLedger
