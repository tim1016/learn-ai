"""Which lake sessions a run can read: the file check and the catalog fold."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path, PurePosixPath

import pytest

from app.data_lake.adjustment_versions import adjusted_file_gap, adjustment_companion, current_snapshot_path
from app.data_lake.catalog_client import ArtifactCoverageRow, SymbolRevisionCount
from app.data_lake.run_readiness import (
    SpanGaps,
    coverage_gaps,
    current_versions,
    fold_run_readiness,
    gapped_symbols,
    span_gaps,
)
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc

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
        ArtifactCoverageRow(date(2024, 10, 31), "complete", 1, "GLD", str(RELATIVE), PRICE_SHA),
        ArtifactCoverageRow(date(2024, 11, 1), "failed", 2, "GLD", "equity/usa/minute/gld/20241101_trade.zip", None),
    ]

    assert coverage_gaps(rows, adjusted_root) == {date(2024, 10, 31): "adjustment_record_missing"}


def test_coverage_gaps_asks_nothing_of_a_raw_root(tmp_path: Path) -> None:
    raw_root = tmp_path / "lake" / "raw"
    rows = [ArtifactCoverageRow(date(2024, 10, 31), "complete", 1, "GLD", str(RELATIVE), PRICE_SHA)]

    # Not even the price file is looked for: a raw backfill would not rebuild a session over it.
    assert coverage_gaps(rows, raw_root) == {}


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


def test_gapped_symbols_are_the_ones_holding_fewer_sessions_than_their_span_schedules() -> None:
    recent = len(expected_sessions(date(2024, 9, 4), date(2026, 10, 2)))
    counts = [
        # Backfilled twice: nine sessions in October 2021, then a run from 2024-09-04, and nothing between.
        _count("QQQ", "old", 9, date(2021, 10, 5), date(2021, 10, 15)),
        _count("QQQ", "new", recent, date(2024, 9, 4), date(2026, 10, 2)),
        _count("SPY", "new", recent, date(2024, 9, 4), date(2026, 10, 2)),
    ]

    assert gapped_symbols(counts) == ["QQQ"]
    assert gapped_symbols([]) == []


def test_span_gaps_measure_each_hole_and_what_a_backfill_can_still_fetch() -> None:
    stray = expected_sessions(date(2021, 10, 5), date(2021, 10, 15))
    recent = expected_sessions(date(2024, 9, 4), date(2024, 12, 31))
    held = {*stray, *recent} - {date(2024, 11, 5)}

    gaps = span_gaps(held, floor=date(2021, 11, 1))

    hole = expected_sessions(date(2021, 10, 18), date(2024, 9, 3))
    # The long hole and the one lost session are two gaps; weekends and closures inside the held runs are none.
    assert (gaps.session_count, gaps.gap_count) == (len(hole) + 1, 2)
    # The provider no longer serves October 2021, so a backfill starts at its floor.
    assert gaps.unfillable_count == len(expected_sessions(date(2021, 10, 18), date(2021, 10, 29)))
    assert (gaps.fill_first, gaps.fill_last) == (date(2021, 11, 1), date(2024, 11, 5))


def test_span_gaps_have_no_fill_window_when_every_missing_session_is_older_than_the_provider_history() -> None:
    held = {date(2021, 10, 5), date(2021, 10, 8)}

    gaps = span_gaps(held, floor=date(2022, 1, 3))

    assert (gaps.session_count, gaps.unfillable_count, gaps.fill_first, gaps.fill_last) == (2, 2, None, None)
    # A symbol whose rows went away between the count and the read has no span to measure.
    assert span_gaps(set(), floor=date(2022, 1, 3)).session_count == 0


def test_fold_reports_a_symbols_gaps_and_none_for_a_symbol_it_was_not_given() -> None:
    counts = [
        _count("QQQ", None, 531, date(2021, 10, 5), date(2026, 10, 2)),
        _count("SPY", None, 1252, date(2021, 10, 7), date(2026, 10, 2)),
    ]
    holes = SpanGaps(session_count=724, gap_count=1, unfillable_count=10, fill_first=date(2021, 11, 1), fill_last=date(2024, 9, 3))

    qqq, spy = fold_run_readiness(counts, None, {"QQQ": holes})

    assert (qqq.gap_session_count, qqq.gap_count, qqq.unfillable_gap_session_count) == (724, 1, 10)
    assert qqq.fill_first_trading_date_ms == session_open_ms_utc(date(2021, 11, 1))
    assert qqq.fill_last_trading_date_ms == session_open_ms_utc(date(2024, 9, 3))
    assert (spy.gap_session_count, spy.gap_count, spy.fill_first_trading_date_ms, spy.fill_last_trading_date_ms) == (0, 0, None, None)

