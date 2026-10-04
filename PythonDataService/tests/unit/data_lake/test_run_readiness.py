"""Which lake sessions a run can read: the file check and the catalog fold."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path, PurePosixPath

import pytest

from app.data_lake.adjustment_versions import adjusted_file_gap, adjustment_companion, current_snapshot_path
from app.data_lake.catalog_client import ArtifactCoverageRow, SymbolRevisionCount
from app.data_lake.run_readiness import coverage_gaps, current_versions, fold_run_readiness
from app.lean_sidecar.trading_calendar import session_open_ms_utc

PRICE_BYTES = b"one session of minute bars"
PRICE_SHA = hashlib.sha256(PRICE_BYTES).hexdigest()
RELATIVE = PurePosixPath("equity/usa/minute/gld/20241031_trade.zip")


@pytest.fixture
def adjusted_root(tmp_path: Path) -> Path:
    root = tmp_path / "lake" / "polygon_split_adjusted"
    root.mkdir(parents=True)
    return root


def _write(root: Path, relative: PurePosixPath, payload: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _publish_snapshot(root: Path, symbol: str, payload: bytes) -> str:
    """The symbol's current corporate-action snapshot; its version is the payload's digest."""
    _write(root, current_snapshot_path(symbol), payload)
    return hashlib.sha256(payload).hexdigest()


def _write_record(root: Path, version: str, *, payload: bytes = PRICE_BYTES) -> None:
    relative, record = adjustment_companion(RELATIVE, payload, version)
    _write(root, relative, record)


def test_adjusted_file_gap_is_none_when_the_record_binds_the_current_version(adjusted_root: Path) -> None:
    version = _publish_snapshot(adjusted_root, "GLD", b"no actions")
    price = _write(adjusted_root, RELATIVE, PRICE_BYTES)
    _write_record(adjusted_root, version)

    assert adjusted_file_gap(price, PRICE_SHA, "GLD") is None


def test_adjusted_file_gap_names_a_missing_record(adjusted_root: Path) -> None:
    """A file written before the lake kept adjustment records: on disk, and unreadable."""
    price = _write(adjusted_root, RELATIVE, PRICE_BYTES)

    assert adjusted_file_gap(price, PRICE_SHA, "GLD") == "adjustment_record_missing"


def test_adjusted_file_gap_rejects_a_record_of_a_superseded_version(adjusted_root: Path) -> None:
    old_version = _publish_snapshot(adjusted_root, "GLD", b"before the dividend")
    price = _write(adjusted_root, RELATIVE, PRICE_BYTES)
    _write_record(adjusted_root, old_version)
    _publish_snapshot(adjusted_root, "GLD", b"after the dividend")

    assert adjusted_file_gap(price, PRICE_SHA, "GLD") == "adjustment_record_rejected"


def test_adjusted_file_gap_rejects_a_record_bound_to_other_bytes(adjusted_root: Path) -> None:
    version = _publish_snapshot(adjusted_root, "GLD", b"no actions")
    price = _write(adjusted_root, RELATIVE, PRICE_BYTES)
    _write_record(adjusted_root, version, payload=b"a different price file")

    assert adjusted_file_gap(price, PRICE_SHA, "GLD") == "adjustment_record_rejected"


def test_adjusted_file_gap_names_a_missing_price_file(adjusted_root: Path) -> None:
    version = _publish_snapshot(adjusted_root, "GLD", b"no actions")
    _write_record(adjusted_root, version)

    assert adjusted_file_gap(adjusted_root / RELATIVE, PRICE_SHA, "GLD") == "price_file_missing"


def test_adjusted_file_gap_asks_nothing_of_a_raw_file(tmp_path: Path) -> None:
    """Raw prices carry no adjustment record, so there is none to miss."""
    price = _write(tmp_path / "lake" / "raw", RELATIVE, PRICE_BYTES)

    assert adjusted_file_gap(price, PRICE_SHA, "GLD") is None


def test_coverage_gaps_checks_only_complete_sessions(adjusted_root: Path) -> None:
    _write(adjusted_root, RELATIVE, PRICE_BYTES)
    rows = [
        ArtifactCoverageRow(date(2024, 10, 31), "complete", 1, str(RELATIVE), PRICE_SHA),
        ArtifactCoverageRow(date(2024, 11, 1), "failed", 2, "equity/usa/minute/gld/20241101_trade.zip", None),
    ]

    assert coverage_gaps(rows, adjusted_root, "GLD") == {date(2024, 10, 31): "adjustment_record_missing"}


def test_current_versions_is_none_for_a_symbol_with_no_snapshot(adjusted_root: Path) -> None:
    version = _publish_snapshot(adjusted_root, "SPY", b"spy actions")

    assert current_versions(adjusted_root, ["GLD", "SPY"]) == {"GLD": None, "SPY": version}


def _count(
    symbol: str, revision: str | None, sessions: int, first: date, last: date, *, before_floor: bool = False
) -> SymbolRevisionCount:
    return SymbolRevisionCount(symbol, revision, before_floor, sessions, first, last)


def test_fold_counts_only_sessions_at_the_current_version_as_readable() -> None:
    counts = [
        _count("SPY", "new", 100, date(2026, 1, 2), date(2026, 5, 29)),
        _count("SPY", "old", 40, date(2025, 11, 3), date(2025, 12, 31)),
        _count("GLD", None, 532, date(2024, 8, 6), date(2026, 9, 18)),
    ]

    gld, spy = fold_run_readiness(counts, {"SPY": "new", "GLD": None})

    assert (spy.session_count, spy.run_ready_count, spy.unrebuildable_count) == (140, 100, 0)
    assert spy.first_trading_date_ms == session_open_ms_utc(date(2025, 11, 3))
    assert spy.last_trading_date_ms == session_open_ms_utc(date(2026, 5, 29))
    # The rebuild window covers only the sessions that need one.
    assert spy.rebuild_first_trading_date_ms == session_open_ms_utc(date(2025, 11, 3))
    assert spy.rebuild_last_trading_date_ms == session_open_ms_utc(date(2025, 12, 31))
    assert (gld.session_count, gld.run_ready_count) == (532, 0)
    assert gld.rebuild_first_trading_date_ms == session_open_ms_utc(date(2024, 8, 6))


def test_fold_keeps_sessions_older_than_the_provider_history_out_of_the_rebuild_window() -> None:
    counts = [
        _count("AMZN", None, 9, date(2021, 9, 22), date(2021, 10, 4), before_floor=True),
        _count("AMZN", None, 1245, date(2021, 10, 5), date(2026, 9, 18)),
    ]

    (amzn,) = fold_run_readiness(counts, {"AMZN": None})

    assert (amzn.session_count, amzn.run_ready_count, amzn.unrebuildable_count) == (1254, 0, 9)
    assert amzn.rebuild_first_trading_date_ms == session_open_ms_utc(date(2021, 10, 5))
    assert amzn.rebuild_last_trading_date_ms == session_open_ms_utc(date(2026, 9, 18))


def test_fold_has_no_rebuild_window_when_only_unfetchable_sessions_are_unreadable() -> None:
    counts = [
        _count("AMZN", None, 9, date(2021, 9, 22), date(2021, 10, 4), before_floor=True),
        _count("AMZN", "new", 1245, date(2021, 10, 5), date(2026, 9, 18)),
    ]

    (amzn,) = fold_run_readiness(counts, {"AMZN": "new"})

    assert (amzn.run_ready_count, amzn.unrebuildable_count) == (1245, 9)
    assert amzn.rebuild_first_trading_date_ms is None
    assert amzn.rebuild_last_trading_date_ms is None


def test_fold_reads_every_complete_session_in_a_mode_without_adjustment_records() -> None:
    counts = [_count("GLD", None, 532, date(2024, 8, 6), date(2026, 9, 18))]

    (gld,) = fold_run_readiness(counts, None)

    assert (gld.run_ready_count, gld.unrebuildable_count) == (532, 0)
    assert gld.rebuild_first_trading_date_ms is None
