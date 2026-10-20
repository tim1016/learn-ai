"""Frozen data snapshot and manifest-bound reads (PRD #1926, review F05)."""

from __future__ import annotations

import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.data_lake.adjustment_versions import AdjustmentVersionError
from app.engine.data.availability import MissingSessionsError
from app.engine.data.lean_format import write_lean_daily_zip
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.sweep.snapshot import (
    DataSnapshot,
    DataSnapshotMismatchError,
    ManifestBoundDailyReader,
    ManifestBoundMinuteReader,
    capture_data_snapshot,
    verify_data_snapshot,
)
from tests._helpers.lean_store import seed_pre_market_day, seed_store_day

WINDOW = (date(2025, 1, 2), date(2025, 1, 10))
SESSIONS = expected_sessions(*WINDOW)


def _seed_minutes(root: Path, *, count: int = 390) -> None:
    for day in SESSIONS:
        seed_store_day(root, "SPY", day, count=count)


def _daily_bar(day: date, close: str) -> TradeBar:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("America/New_York"))
    price = Decimal(close)
    return TradeBar(symbol="SPY", time=start, end_time=start.replace(hour=23, minute=59), open=price, high=price, low=price, close=price, volume=1)


def test_capture_fingerprints_every_session_artifact(tmp_path: Path) -> None:
    _seed_minutes(tmp_path)

    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])

    assert snapshot.sessions == tuple(SESSIONS)
    assert set(snapshot.artifacts) == {f"equity/usa/minute/spy/{day.strftime('%Y%m%d')}_trade.zip" for day in SESSIONS}
    assert all(len(digest) == 64 for digest in snapshot.artifacts.values())
    assert snapshot.calendar_identity == "NYSE"
    assert DataSnapshot.from_dict(snapshot.as_dict()) == snapshot
    assert verify_data_snapshot(snapshot, [tmp_path]) == []


def test_capture_refuses_a_missing_session_and_names_it(tmp_path: Path) -> None:
    for day in SESSIONS[:-1]:
        seed_store_day(tmp_path, "SPY", day)

    with pytest.raises(MissingSessionsError) as excinfo:
        capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])

    assert excinfo.value.report.missing_days == [SESSIONS[-1]]
    assert f"missing {SESSIONS[-1].isoformat()}" in str(excinfo.value)


def test_verify_reports_an_artifact_whose_bytes_moved(tmp_path: Path) -> None:
    _seed_minutes(tmp_path)
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])

    seed_store_day(tmp_path, "SPY", SESSIONS[2], count=200)

    assert verify_data_snapshot(snapshot, [tmp_path]) == [f"equity/usa/minute/spy/{SESSIONS[2].strftime('%Y%m%d')}_trade.zip"]


def test_a_bound_minute_reader_refuses_bytes_that_changed_after_capture(tmp_path: Path) -> None:
    _seed_minutes(tmp_path)
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])
    reader = ManifestBoundMinuteReader([tmp_path], snapshot.artifacts)

    # A refresh lands between capture and the read of the third session —
    # the A→B→A case a periodic check cannot see, because the read is what
    # is bound, not a check scheduled around it.
    seed_store_day(tmp_path, "SPY", SESSIONS[2], count=200)

    consumed: list[TradeBar] = []
    with pytest.raises(DataSnapshotMismatchError, match="changed since the snapshot"):
        for bar in reader.iter_bars("SPY", *WINDOW):
            consumed.append(bar)
    # Everything before the tampered session was read from receipted bytes.
    assert len(consumed) == 2 * 390


def test_a_bound_minute_reader_refuses_deletion_before_date_enumeration(tmp_path: Path) -> None:
    _seed_minutes(tmp_path)
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])
    reader = ManifestBoundMinuteReader([tmp_path], snapshot.artifacts)
    (tmp_path / f"equity/usa/minute/spy/{SESSIONS[2].strftime('%Y%m%d')}_trade.zip").unlink()

    with pytest.raises(DataSnapshotMismatchError, match="was receipted at launch but is missing now"):
        list(reader.iter_bars("SPY", *WINDOW))


def test_a_bound_reader_yields_identical_bars_to_the_plain_reader(tmp_path: Path) -> None:
    from app.engine.data.lean_format import LeanMinuteDataReader

    _seed_minutes(tmp_path)
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])

    plain = list(LeanMinuteDataReader([tmp_path]).iter_bars("SPY", *WINDOW))
    bound = list(ManifestBoundMinuteReader([tmp_path], snapshot.artifacts).iter_bars("SPY", *WINDOW))

    assert bound == plain
    assert len(bound) == len(SESSIONS) * 390


def test_a_file_outside_the_manifest_is_refused(tmp_path: Path) -> None:
    _seed_minutes(tmp_path)
    reader = ManifestBoundMinuteReader([tmp_path], {})

    with pytest.raises(DataSnapshotMismatchError, match="not part of the receipted"):
        list(reader.iter_bars("SPY", *WINDOW))


def test_daily_snapshot_and_bound_reader(tmp_path: Path) -> None:
    write_lean_daily_zip(tmp_path, "SPY", [_daily_bar(day, "500") for day in SESSIONS])
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])
    assert set(snapshot.artifacts) == {"equity/usa/daily/spy.zip"}

    bound = ManifestBoundDailyReader([tmp_path], snapshot.artifacts)
    assert len(list(bound.iter_bars("SPY", *WINDOW))) == len(SESSIONS)

    write_lean_daily_zip(tmp_path, "SPY", [_daily_bar(SESSIONS[0], "501")])
    with pytest.raises(DataSnapshotMismatchError):
        list(ManifestBoundDailyReader([tmp_path], snapshot.artifacts).iter_bars("SPY", *WINDOW))


# #3037: the manifest check cannot be skipped by the history cache's hit branch.
# The base reader's old early return handed back decoded bars without
# traversing the subclass's ``_read_zip``, so a warm bound reader served
# history that no longer matched the receipted manifest.
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(
            lambda root: write_lean_daily_zip(root, "SPY", [_daily_bar(SESSIONS[0], "501")]),
            "changed since the snapshot",
            id="bytes_changed_after_its_first_read_3037",
        ),
        pytest.param(
            lambda root: (root / "equity" / "usa" / "daily" / "spy.zip").unlink(),
            "was receipted at launch but is missing now",
            id="receipted_archive_deleted_after_the_first_read",
        ),
    ],
)
def test_a_warm_bound_daily_reader_refuses_an_archive_that_moved_after_its_first_read(tmp_path: Path, mutate, message: str) -> None:
    write_lean_daily_zip(tmp_path, "SPY", [_daily_bar(day, "500") for day in SESSIONS])
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])
    bound = ManifestBoundDailyReader([tmp_path], snapshot.artifacts)
    assert len(list(bound.iter_bars("SPY", *WINDOW))) == len(SESSIONS), "the first read serves every session"

    mutate(tmp_path)

    with pytest.raises(DataSnapshotMismatchError, match=message):
        list(bound.iter_bars("SPY", *WINDOW))


def test_a_warm_bound_daily_reader_rereads_identical_bars_without_redecoding(tmp_path: Path, monkeypatch) -> None:
    write_lean_daily_zip(tmp_path, "SPY", [_daily_bar(day, "500") for day in SESSIONS])
    snapshot = capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])
    bound = ManifestBoundDailyReader([tmp_path], snapshot.artifacts)
    decodes = 0
    real_parse = bound.parse_history_zip

    def counting_parse(payload, symbol):
        nonlocal decodes
        decodes += 1
        return real_parse(payload, symbol)

    monkeypatch.setattr(bound, "parse_history_zip", counting_parse)
    first = list(bound.iter_bars("SPY", *WINDOW))
    second = list(bound.iter_bars("SPY", *WINDOW))
    assert first == second
    assert decodes == 1, "a warm bound read re-verifies the bytes but must not decode again"


def test_a_warm_bound_daily_reader_refuses_a_changed_adjustment_basis(tmp_path, seeded_lake_catalog) -> None:
    from uuid import UUID

    from app.data_lake.adjustment_versions import CorporateActionSnapshot, current_snapshot_path
    from app.data_lake.polygon_corp_actions import DividendEvent
    from app.data_lake.root_identity import init_empty_root
    from tests._helpers.lake_fixture import seed_lake_daily

    root = init_empty_root(tmp_path / "store", UUID(int=2456)).lake_root("polygon_split_adjusted")
    seed_lake_daily(root, "SPY", list(SESSIONS))
    snapshot = capture_data_snapshot(roots=[root], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])
    bound = ManifestBoundDailyReader([root], snapshot.artifacts)
    assert len(list(bound.iter_bars("SPY", *WINDOW))) == len(SESSIONS)

    moved = CorporateActionSnapshot((), (DividendEvent("2000-01-02", 1.0),), date(2000, 1, 1))
    (root / current_snapshot_path("SPY")).write_bytes(moved.payload())

    with pytest.raises(AdjustmentVersionError, match="stale or mixed corporate-action version"):
        list(bound.iter_bars("SPY", *WINDOW))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        pytest.param(
            lambda companion: companion.unlink(),
            "corporate-action version receipt is missing",
            id="companion_deleted",
        ),
        pytest.param(
            lambda companion: companion.write_bytes(companion.read_bytes() + b"\n"),
            "changed since the snapshot",
            id="companion_changed",
        ),
    ],
)
def test_a_warm_bound_daily_reader_refuses_a_companion_that_moved_after_the_first_read(tmp_path, seeded_lake_catalog, mutate, message) -> None:
    from uuid import UUID

    from app.data_lake.adjustment_versions import companion_path
    from app.data_lake.root_identity import init_empty_root
    from tests._helpers.lake_fixture import seed_lake_daily

    root = init_empty_root(tmp_path / "store", UUID(int=2456)).lake_root("polygon_split_adjusted")
    seed_lake_daily(root, "SPY", list(SESSIONS))
    snapshot = capture_data_snapshot(roots=[root], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])
    bound = ManifestBoundDailyReader([root], snapshot.artifacts)
    assert list(bound.iter_bars("SPY", *WINDOW)), "the first read serves bars"

    mutate(companion_path(root / "equity" / "usa" / "daily" / "spy.zip"))

    with pytest.raises(DataSnapshotMismatchError, match=message):
        list(bound.iter_bars("SPY", *WINDOW))


def test_daily_capture_refuses_a_missing_session(tmp_path: Path) -> None:
    write_lean_daily_zip(tmp_path, "SPY", [_daily_bar(day, "500") for day in SESSIONS[1:]])

    with pytest.raises(MissingSessionsError) as excinfo:
        capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])

    assert excinfo.value.report.missing_days == [SESSIONS[0]]


def test_daily_capture_refuses_a_session_whose_row_the_reader_cannot_parse(tmp_path: Path) -> None:
    """A truncated row still begins with its date, but the reader parses no bar from it (#2445 review).

    Admitting it would bind the sweep to a snapshot whose bound reader then
    reads one session fewer than the window holds, without a word.
    """
    write_lean_daily_zip(tmp_path, "SPY", [_daily_bar(day, "500") for day in SESSIONS])
    zip_path = tmp_path / "equity" / "usa" / "daily" / "spy.zip"
    with zipfile.ZipFile(zip_path) as zf:
        rows = zf.read("spy.csv").decode("ascii").splitlines()
    stamp = SESSIONS[3].strftime("%Y%m%d")
    rows = [",".join(row.split(",")[:4]) if row.startswith(stamp) else row for row in rows]
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("spy.csv", "\n".join(rows) + "\n")

    with pytest.raises(MissingSessionsError) as excinfo:
        capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])

    assert excinfo.value.report.missing_days == [SESSIONS[3]]


def test_capture_refuses_a_session_whose_zip_holds_no_regular_hours_bar(tmp_path: Path) -> None:
    """A zip on disk that the regular-session reader reads no bar from is a missing session (#2475 review)."""
    _seed_minutes(tmp_path)
    seed_pre_market_day(tmp_path, "SPY", SESSIONS[2])

    with pytest.raises(MissingSessionsError) as excinfo:
        capture_data_snapshot(roots=[tmp_path], symbol="SPY", resolution="minute", data_start=WINDOW[0], data_end=WINDOW[1])

    assert excinfo.value.report.missing_days == [SESSIONS[2]]


def test_daily_capture_refuses_more_than_one_root(tmp_path: Path) -> None:
    """Two roots' histories share one root-relative path, so one digest could bind only one of them (#2475 review).

    Before, the capture hashed the first root's archive alone and the second
    root's sessions entered the window unreceipted.
    """
    first, second = tmp_path / "first", tmp_path / "second"
    write_lean_daily_zip(first, "SPY", [_daily_bar(day, "500") for day in SESSIONS[:3]])
    write_lean_daily_zip(second, "SPY", [_daily_bar(day, "501") for day in SESSIONS[3:]])

    with pytest.raises(ValueError, match="a daily data snapshot binds one root, got 2"):
        capture_data_snapshot(roots=[first, second], symbol="SPY", resolution="daily", data_start=WINDOW[0], data_end=WINDOW[1])
