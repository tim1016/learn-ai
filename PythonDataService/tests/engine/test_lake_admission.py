"""Managed lake readers admit only bytes with a committed physical-file receipt."""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID

import pytest

from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError, read_committed_bytes
from app.data_lake.path_policy import lake_subpath
from app.data_lake.root_identity import init_empty_root, marker_path
from app.engine.data.availability import MissingSessionsError, check_availability
from app.engine.data.lean_format import LeanDailyDataReader, LeanMinuteDataReader
from app.lean_sidecar.lake_mount import LakeMountError, resolve_lake_artifacts
from app.research.sweep.snapshot import (
    ManifestBoundDailyReader,
    ManifestBoundMinuteReader,
    capture_data_snapshot,
)
from app.services.chart_bar_source import compose_chart_bars
from app.services.return_distribution_service import _probe_lake_sessions, _read_study_window
from tests._helpers.lake_fixture import (
    seed_lake_corporate_actions,
    seed_lake_interest_rate,
    seed_lake_window,
)

DAY = date(2024, 5, 20)
AFTER_SESSION_MS = 1716336000000  # 2024-05-22 UTC, after the entire session.


@pytest.fixture
def lake(tmp_path, seeded_lake_catalog):
    root = tmp_path / lake_subpath("raw")
    seed_lake_window(root, "SPY", [DAY])
    return root


def _snapshot(root, resolution="minute"):
    return capture_data_snapshot(roots=[root], symbol="SPY", resolution=resolution, data_start=DAY, data_end=DAY)


@pytest.mark.parametrize("resolution", ["minute", "daily"])
def test_snapshot_and_bound_readers_recheck_commit_after_capture(lake, seeded_lake_catalog, resolution):
    snapshot = _snapshot(lake, resolution)
    reader = (ManifestBoundMinuteReader if resolution == "minute" else ManifestBoundDailyReader)(lake, snapshot.artifacts)
    assert list(reader.iter_bars("SPY", DAY, DAY))
    # A writer can revoke completion before touching the file. A manifest
    # digest and a warm filesystem-coverage cache must not bypass admission.
    seeded_lake_catalog.clear()
    reader = (ManifestBoundMinuteReader if resolution == "minute" else ManifestBoundDailyReader)(lake, snapshot.artifacts)
    with pytest.raises(LakeAdmissionError, match="committed"):
        list(reader.iter_bars("SPY", DAY, DAY))
    with pytest.raises(MissingSessionsError):
        _snapshot(lake, resolution)


def test_coverage_rechecks_catalog_even_when_file_cache_key_is_unchanged(lake, seeded_lake_catalog):
    assert check_availability([lake], "SPY", DAY, DAY).is_complete
    seeded_lake_catalog.clear()
    report = check_availability([lake], "SPY", DAY, DAY)
    assert not report.is_complete
    assert report.unreadable_files


@pytest.mark.parametrize("relative", [
    "equity/usa/minute/spy/20240520_trade.zip",
    "equity/usa/minute/spy/20240520_quote.zip",
    "equity/usa/daily/spy.zip",
    "market-hours/market-hours-database.json",
    "symbol-properties/symbol-properties-database.csv",
    "equity/usa/factor_files/spy.csv",
    "equity/usa/map_files/spy.csv",
    "alternative/interest-rate/usa/interest-rate.csv",
])
def test_lean_preflight_refuses_every_uncommitted_input(lake, seeded_lake_catalog, relative):
    seed_lake_corporate_actions(lake, "SPY")
    seed_lake_interest_rate(lake)
    assert resolve_lake_artifacts(lake_root=lake, symbol="SPY", start=DAY, end=DAY)
    keys = [key for key in seeded_lake_catalog if key[2] == relative]
    assert len(keys) == 1
    del seeded_lake_catalog[keys[0]]
    with pytest.raises(LakeMountError, match="lake_artifact_not_committed"):
        resolve_lake_artifacts(lake_root=lake, symbol="SPY", start=DAY, end=DAY)


def test_chart_falls_back_and_study_requests_capture_for_orphan(lake, seeded_lake_catalog):
    seeded_lake_catalog.clear()
    calls = []

    def provider(start, end):
        calls.append((start, end))
        return []

    result = compose_chart_bars(ticker="SPY", from_date=DAY.isoformat(), to_date=DAY.isoformat(),
                               adjusted=False, fetch_provider=provider, lake_root=lake,
                               now_ms=AFTER_SESSION_MS)
    assert calls == [(DAY.isoformat(), DAY.isoformat())]
    assert all(span.source == "provider" for span in result.spans)
    assert _probe_lake_sessions(symbol="SPY", from_date=DAY, to_date=DAY, lake_root=lake,
                               now_ms=AFTER_SESSION_MS).sessions_missing
    assert _read_study_window(symbol="SPY", from_date=DAY, to_date=DAY, lake_root=lake).lake_dates == []


def test_catalog_receipt_cannot_cross_roots_or_modes(lake, tmp_path, seeded_lake_catalog):
    original = lake / "equity/usa/minute/spy/20240520_trade.zip"
    other = init_empty_root(tmp_path / "other", UUID(int=2456)).lake_root("raw")
    copied = other / original.relative_to(lake)
    copied.parent.mkdir(parents=True)
    copied.write_bytes(original.read_bytes())
    with pytest.raises(LakeAdmissionError, match="committed"):
        read_committed_bytes(copied)
    moved_key = next(key for key in seeded_lake_catalog if key[2] == original.relative_to(lake).as_posix())
    value = seeded_lake_catalog.pop(moved_key)
    seeded_lake_catalog[(moved_key[0], "polygon_split_adjusted", moved_key[2])] = value
    with pytest.raises(LakeAdmissionError, match="committed"):
        read_committed_bytes(original)


def test_old_receipt_does_not_admit_replacement_bytes(lake):
    path = lake / "equity/usa/minute/spy/20240520_trade.zip"
    path.write_bytes(path.read_bytes() + b"new uncommitted generation")
    with pytest.raises(LakeAdmissionError, match="committed"):
        LeanMinuteDataReader(lake).read_day("SPY", DAY)


def test_missing_root_identity_never_downgrades_to_reference_reader(lake):
    marker_path(lake.parent.parent).unlink()
    with pytest.raises(LakeAdmissionError, match="root identity"):
        LeanDailyDataReader(lake).available_dates("SPY")


def test_catalog_outage_fails_closed(lake, monkeypatch):
    async def unavailable(*args):
        raise catalog_client.CatalogUnavailableError("offline")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", unavailable)
    with pytest.raises(catalog_client.CatalogUnavailableError, match="cannot verify committed"):
        LeanMinuteDataReader(lake).read_day("SPY", DAY)


def test_exact_payload_read_is_the_one_verified(lake, monkeypatch):
    path = lake / "equity/usa/minute/spy/20240520_trade.zip"
    payload = path.read_bytes()

    async def replace_after_read(root_id, mode, relative, digest, size):
        path.write_bytes(b"replacement")
        return (digest, size) == (hashlib.sha256(payload).hexdigest(), len(payload))

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", replace_after_read)
    assert read_committed_bytes(path) == payload


@pytest.mark.asyncio
async def test_reader_refuses_promoted_bytes_after_catalog_commit_failure(tmp_path, monkeypatch):
    """#2456: exercise the real publication transaction's post-rename failure."""
    import asyncio
    from contextlib import asynccontextmanager
    from datetime import date
    from pathlib import PurePosixPath

    from app.data_lake.atomic import publish_artifact
    from app.data_lake.root_identity import init_empty_root
    from app.engine.data.lean_format import LeanMinuteDataReader
    from tests._helpers.lean_store import seed_store_day

    root = init_empty_root(tmp_path / "store", UUID(int=2456))
    lake_root = root.lake_root("raw")
    lake_root.mkdir(parents=True)
    staging_root = root.staging_root("raw")
    staging_root.mkdir(parents=True)
    day = date(2024, 5, 20)
    source = seed_store_day(tmp_path / "source", "SPY", day, count=1)
    relative = PurePosixPath("equity/usa/minute/spy/20240520_trade.zip")

    class CommitFailure(RuntimeError):
        pass

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            yield
            raise CommitFailure("injected commit failure")

        async def fetchrow(self, query, *args):
            if 'FOR UPDATE' in query:
                return dict(Status="fetching", LeaseOwner="writer", LeaseGeneration=1,
                            LeaseExpiresAtMs=9_000_000_000_000)
            return None  # No committed receipt survived the rollback.

        async def execute(self, query, *args):
            return "UPDATE 1"

    @asynccontextmanager
    async def connection():
        yield Connection()

    async def init_pool():
        return None

    monkeypatch.setattr(catalog_client, "connection", connection)
    monkeypatch.setattr(catalog_client, "init_pool", init_pool)
    with pytest.raises(CommitFailure, match="injected"):
        await publish_artifact(
            content=source.read_bytes(), lake_root=lake_root, staging_root=staging_root,
            rel_lake_path=relative, request_id=UUID(int=1), worker_id="writer", attempt=1,
            artifact_id=1, lease_generation=1, row_count=1,
            first_bar_start_ms=1_716_211_800_000, last_bar_start_ms=1_716_211_800_000,
        )
    assert (lake_root / relative).is_file(), "the fault must occur after promotion"
    with pytest.raises(RuntimeError, match="committed"):
        await asyncio.to_thread(LeanMinuteDataReader(lake_root).read_day, "SPY", day)


def test_derivation_refuses_uncommitted_source_bars(lake, seeded_lake_catalog):
    from app.data_lake.derived_daily import read_minute_trade_bars

    relative = "equity/usa/minute/spy/20240520_trade.zip"
    assert read_minute_trade_bars(relative, lake)
    seeded_lake_catalog.clear()
    with pytest.raises(LakeAdmissionError, match="committed"):
        read_minute_trade_bars(relative, lake)


def test_chart_falls_back_if_admission_is_revoked_after_planning(lake, seeded_lake_catalog, monkeypatch):
    from app.services import chart_bar_source

    read = chart_bar_source._read_lake_bars
    calls = []

    def revoke_then_read(*args):
        seeded_lake_catalog.clear()
        return read(*args)

    def provider(start, end):
        calls.append((start, end))
        return []

    monkeypatch.setattr(chart_bar_source, "_read_lake_bars", revoke_then_read)
    result = compose_chart_bars(ticker="SPY", from_date=DAY.isoformat(), to_date=DAY.isoformat(),
                               adjusted=False, fetch_provider=provider, lake_root=lake,
                               now_ms=AFTER_SESSION_MS)
    assert calls == [(DAY.isoformat(), DAY.isoformat())]
    assert all(span.source == "provider" for span in result.spans)


def test_catalog_outage_stops_range_probe_after_one_failure(lake, monkeypatch):
    from app.services.return_distribution_service import DayNotCapturedError, _day_candles_sync

    calls = []

    async def unavailable(*args):
        calls.append(args)
        raise catalog_client.CatalogUnavailableError("offline")

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", unavailable)
    providers = []
    compose_chart_bars(ticker="SPY", from_date=DAY.isoformat(), to_date="2024-05-31",
                       adjusted=False, fetch_provider=lambda *args: providers.append(args) or [],
                       lake_root=lake, now_ms=1717372800000)
    assert len(calls) == 1
    assert len(providers) == 1
    with pytest.raises(catalog_client.CatalogUnavailableError):
        check_availability([lake], "SPY", DAY, DAY)
    assert len(calls) == 2

    async def uncommitted(*args):
        return False

    monkeypatch.setattr(catalog_client, "has_committed_file_receipt", uncommitted)
    from app.lean_sidecar.trading_calendar import session_open_ms_utc
    with pytest.raises(DayNotCapturedError):
        _day_candles_sync(symbol="SPY", session_open_ms_utc=session_open_ms_utc(DAY), lake_root=lake)
