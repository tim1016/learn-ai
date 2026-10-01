"""File-backed run storage — A2 acceptance gate.

Uses ``tmp_path`` as the artifacts root so the suite is hermetic and
parallel-safe. The runner is invoked through the same fake-data path
as ``test_runner_inmemory.py``; this file's only concern is what
happens after a ``(ledger, result)`` pair leaves the runner.
"""

from __future__ import annotations

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
    RunCorruptError,
    RunRequest,
    list_runs,
    load_run,
    run_date_to_ms,
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
            start_ms=run_date_to_ms(date(2024, 1, 2)),
            end_ms=run_date_to_ms(date(2024, 12, 31)),
            parent_run_id=parent_run_id,
            parent_spec_hash=parent_spec_hash,
        ),
        data_source_factory=factory,
        data_root_revision="test-revision-1",
        run_id=run_id,
    )


# ---------------------------------------------------------------------------
# Failure modes.
# ---------------------------------------------------------------------------
def test_load_run_with_malformed_run_id_raises_value_error(tmp_path: Path):
    """Path-traversal defense: regex is ``^[0-9a-f]{32}$``, so anything
    that isn't exactly 32 lowercase hex chars rejects fast.
    """
    bad_ids = [
        "../../../etc/passwd",
        "..",
        "/",
        "abc/../def",
        "abc def",  # whitespace
        "../" + "a" * 30,
        "abcz",  # 'z' not in hex alphabet
        "a" * 7,  # below length
        "a" * 33,  # above length
        "-" * 32,  # hyphens — used to pass the loose regex (PR #107 round 2)
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
def test_list_filter_by_symbol(tmp_path: Path, fake_data_factory):
    spec = _build_test_spec()
    ledger, result = _make_run(spec, fake_data_factory)
    save_run(ledger, result, root=tmp_path)

    assert list_runs(root=tmp_path, symbol="TEST")[0].run_id == ledger.run_id
    assert list_runs(root=tmp_path, symbol="DOES_NOT_EXIST") == []


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
    assert any(rec.message.startswith("[RUNS]") and "skipping corrupt" in rec.message for rec in caplog.records)


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
