"""Unit tests for the lean-cache -> lake catalog import (#1832).

Pure-function tests (discovery, provenance loading, zip verification, DCH
tagging, provenance-preservation, and the claim-outcome decision that encodes
idempotency + no-overwrite) need no database and always run.

Orchestration tests (``import_cache_root``) exercise the real catalog via
live Postgres, following the same skip-if-unconfigured pattern as
``test_ensure_data.py`` / ``test_catalog_write_ops.py``. They are
parametrized by adjustment mode: the 'raw' cases pass against the schema as
it stands today; the 'polygon_split_adjusted' cases additionally require
``Backend/Migrations/20260827120000_AllowImportedNonRawAdjustmentModes.cs``
to be applied (see that migration and ``app/data_lake/cache_import.py``'s
module docstring for why).
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.cache_import import (
    ClaimDecision,
    CorruptCacheZipError,
    LakeRootModeConflictError,
    MissingProvenanceError,
    build_provider_params,
    check_lake_root_mode,
    decide_claim_outcome,
    discover_cache_zips,
    import_cache_root,
    load_symbol_provenance,
    price_adjustment_mode_for,
    verify_and_read_zip,
)
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath
from app.data_lake.types import ArtifactRecord

_ET = ZoneInfo("America/New_York")


def _bar(hour: int, minute: int, trading_date: date, price: str) -> MinuteTradeBar:
    from datetime import datetime

    return MinuteTradeBar(
        bar_start_et=datetime(trading_date.year, trading_date.month, trading_date.day, hour, minute, tzinfo=_ET),
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=100,
    )


def _write_valid_zip(cache_root: Path, symbol: str, trading_date: date, bars: list[MinuteTradeBar] | None = None) -> Path:
    if bars is None:
        bars = [_bar(9, 30, trading_date, "500.00"), _bar(9, 31, trading_date, "500.05")]
    payload = build_minute_trade_zip_bytes(
        symbol=symbol, trading_date_yyyymmdd=trading_date.strftime("%Y%m%d"), bars=bars
    )
    day_dir = cache_root / "equity" / "usa" / "minute" / symbol.lower()
    day_dir.mkdir(parents=True, exist_ok=True)
    zip_path = day_dir / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    zip_path.write_bytes(payload)
    return zip_path


def _write_provenance(cache_root: Path, symbol: str, *, adjusted: bool, fetches: list[dict] | None = None) -> Path:
    prov_dir = cache_root / "provenance"
    prov_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": 1,
        "symbol": symbol.upper(),
        "policy": {"source": "polygon", "adjusted": adjusted},
        "fetches": fetches if fetches is not None else [{"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-05-20", "fetched_at_ms": 1_700_000_000_000}],
    }
    path = prov_dir / f"{symbol.lower()}.json"
    path.write_text(json.dumps(doc))
    return path


# ---------------------------------------------------------------------------
# discover_cache_zips
# ---------------------------------------------------------------------------


def test_discover_cache_zips_finds_all_symbols_and_dates_sorted(tmp_path: Path):
    _write_valid_zip(tmp_path, "SPY", date(2024, 5, 21))
    _write_valid_zip(tmp_path, "SPY", date(2024, 5, 20))
    _write_valid_zip(tmp_path, "QQQ", date(2024, 5, 20))

    refs, unrecognized = discover_cache_zips(tmp_path)

    assert unrecognized == []
    assert [(r.symbol, r.trading_date) for r in refs] == [
        ("QQQ", date(2024, 5, 20)),
        ("SPY", date(2024, 5, 20)),
        ("SPY", date(2024, 5, 21)),
    ]


def test_discover_cache_zips_ignores_non_trade_files(tmp_path: Path):
    _write_valid_zip(tmp_path, "SPY", date(2024, 5, 20))
    stray_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    (stray_dir / "20240520_quote.zip").write_bytes(b"not a trade zip")
    (stray_dir / "notes.txt").write_text("hello")

    refs, unrecognized = discover_cache_zips(tmp_path)

    assert unrecognized == []
    assert len(refs) == 1
    assert refs[0].zip_path.name == "20240520_trade.zip"


def test_discover_cache_zips_missing_minute_tree_returns_empty(tmp_path: Path):
    assert discover_cache_zips(tmp_path) == ([], [])


def test_discover_cache_zips_surfaces_mis_named_file_as_unrecognized(tmp_path: Path):
    _write_valid_zip(tmp_path, "SPY", date(2024, 5, 20))
    stray_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    # Matches the *_trade.zip glob but not the <yyyymmdd>_trade.zip shape.
    (stray_dir / "spy_2024-05-21_trade.zip").write_bytes(b"whatever")

    refs, unrecognized = discover_cache_zips(tmp_path)

    assert len(refs) == 1  # the well-formed zip is still found
    assert len(unrecognized) == 1
    assert unrecognized[0].symbol == "SPY"
    assert unrecognized[0].path.name == "spy_2024-05-21_trade.zip"


def test_discover_cache_zips_surfaces_invalid_calendar_date_as_unrecognized(tmp_path: Path):
    _write_valid_zip(tmp_path, "SPY", date(2024, 5, 20))
    stray_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    # 8 digits, matches the regex shape, but month 13 doesn't exist -- must
    # not raise an uncaught ValueError out of discover_cache_zips.
    (stray_dir / "20241332_trade.zip").write_bytes(b"whatever")

    refs, unrecognized = discover_cache_zips(tmp_path)

    assert len(refs) == 1
    assert len(unrecognized) == 1
    assert unrecognized[0].symbol == "SPY"
    assert "20241332" in unrecognized[0].detail


# ---------------------------------------------------------------------------
# load_symbol_provenance / price_adjustment_mode_for (adjustment-mode tagging)
# ---------------------------------------------------------------------------


def test_price_adjustment_mode_for_true_is_polygon_split_adjusted(tmp_path: Path):
    _write_provenance(tmp_path, "SPY", adjusted=True)
    provenance = load_symbol_provenance(tmp_path, "SPY")
    assert price_adjustment_mode_for(provenance) == "polygon_split_adjusted"


def test_price_adjustment_mode_for_false_is_raw(tmp_path: Path):
    _write_provenance(tmp_path, "SPY", adjusted=False)
    provenance = load_symbol_provenance(tmp_path, "SPY")
    assert price_adjustment_mode_for(provenance) == "raw"


def test_load_symbol_provenance_missing_file_raises(tmp_path: Path):
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


def test_load_symbol_provenance_missing_policy_adjusted_raises(tmp_path: Path):
    prov_dir = tmp_path / "provenance"
    prov_dir.mkdir(parents=True)
    (prov_dir / "spy.json").write_text(json.dumps({"schema_version": 1, "symbol": "SPY"}))
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


def test_load_symbol_provenance_invalid_json_raises(tmp_path: Path):
    prov_dir = tmp_path / "provenance"
    prov_dir.mkdir(parents=True)
    (prov_dir / "spy.json").write_text("{not json")
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


# ---------------------------------------------------------------------------
# _import_minute_trade_dch (imported-vs-fetched provenance distinction)
# ---------------------------------------------------------------------------


def test_import_minute_trade_dch_differs_by_adjustment_mode():
    from app.data_lake.cache_import import _import_minute_trade_dch

    assert _import_minute_trade_dch(adjusted=False) != _import_minute_trade_dch(adjusted=True)


def test_import_minute_trade_dch_is_deterministic():
    from app.data_lake.cache_import import _import_minute_trade_dch

    assert _import_minute_trade_dch(adjusted=True) == _import_minute_trade_dch(adjusted=True)


def test_import_minute_trade_dch_raw_differs_from_ensure_data_fetch_dch():
    """An imported 'raw' artifact and a live Polygon fetch produce the same
    kind of bytes, but must carry a *different* data_contract_hash -- the
    DCH is part of the provenance trail, so "this was imported, not fetched"
    must be visible there even when nothing else about the row would
    otherwise distinguish the two. Mirrors test_ensure_data.py's own style
    of importing a private DCH helper directly for a parity assertion."""
    from app.data_lake.cache_import import _import_minute_trade_dch
    from app.data_lake.ensure_data import _minute_trade_dch

    assert _import_minute_trade_dch(adjusted=False) != _minute_trade_dch()


# ---------------------------------------------------------------------------
# build_provider_params (provenance preservation)
# ---------------------------------------------------------------------------


def test_build_provider_params_preserves_original_fetch_history(tmp_path: Path):
    fetches = [
        {"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-05-10", "fetched_at_ms": 1},
        {"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-06-01", "fetched_at_ms": 2},
    ]
    _write_provenance(tmp_path, "SPY", adjusted=True, fetches=fetches)
    provenance = load_symbol_provenance(tmp_path, "SPY")

    params = build_provider_params(tmp_path, provenance)

    assert params["imported_from_cache"] is True
    assert params["import_source"] == "lean_cache"
    assert params["cache_root"] == str(tmp_path)
    assert isinstance(params["imported_at_ms"], int)
    # The full original document -- including every historical fetch, the
    # evidence of the refetch leak (#1830) -- survives byte-for-byte.
    assert params["original_provenance"] == provenance
    assert params["original_provenance"]["fetches"] == fetches


# ---------------------------------------------------------------------------
# verify_and_read_zip (corrupt-zip refusal)
# ---------------------------------------------------------------------------


# 2024-05-20 09:30:00 America/New_York (EDT, UTC-4) = 1716211800000 ms UTC --
# the same ET anchor test_ensure_data.py's _polygon_ok_payload documents and
# relies on, cross-checked independently here via ZoneInfo rather than
# trusted from the module under test.
_KNOWN_BAR_START_MS = 1716211800000


def test_verify_and_read_zip_valid_zip_returns_metadata(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    bars = [_bar(9, 30, trading_date, "500.00"), _bar(9, 31, trading_date, "500.05")]
    zip_path = _write_valid_zip(tmp_path, "SPY", trading_date, bars=bars)

    verified = verify_and_read_zip(zip_path, "SPY", trading_date)

    assert verified.row_count == 2
    # Pinned to the actual ET-anchored epoch value, not just first < last --
    # a wrong UTC offset (or a naive/UTC anchor instead of ET) would still
    # satisfy first < last while being numerically wrong.
    assert verified.first_bar_start_ms == _KNOWN_BAR_START_MS
    assert verified.last_bar_start_ms == _KNOWN_BAR_START_MS + 60_000
    assert verified.raw_bytes == zip_path.read_bytes()


def test_verify_and_read_zip_encrypted_member_raises(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("20240520_spy_minute_trade.csv", "34200000,5000000,5000000,5000000,5000000,100\n")
        for info in zf.infolist():
            # Flip the "encrypted" bit (bit 0 of the general-purpose flag) in
            # the central-directory entry so a read without a password
            # raises RuntimeError -- no third-party AES-zip library needed
            # just to build the fixture.
            info.flag_bits |= 0x1

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


def test_verify_and_read_zip_not_a_zip_raises(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    bad = day_dir / "20240520_trade.zip"
    bad.write_bytes(b"this is not a zip file at all")

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(bad, "SPY", trading_date)


def test_verify_and_read_zip_missing_file_raises(tmp_path: Path):
    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(tmp_path / "does_not_exist.zip", "SPY", date(2024, 5, 20))


def test_verify_and_read_zip_wrong_member_name_raises(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("wrong_name.csv", "34200000,5000000,5000000,5000000,5000000,100\n")

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


def test_verify_and_read_zip_malformed_row_raises(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        # Missing a field (only 5 columns instead of 6).
        zf.writestr("20240520_spy_minute_trade.csv", "34200000,5000000,5000000,5000000,5000000\n")

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


def test_verify_and_read_zip_non_integer_field_raises(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("20240520_spy_minute_trade.csv", "34200000,NOT_A_PRICE,5000000,5000000,5000000,100\n")

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


def test_verify_and_read_zip_zero_rows_raises(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("20240520_spy_minute_trade.csv", "")

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


# ---------------------------------------------------------------------------
# decide_claim_outcome (idempotency + no-overwrite, pure)
# ---------------------------------------------------------------------------


def _record(file_sha256: str) -> ArtifactRecord:
    return ArtifactRecord(
        id=1,
        artifact_kind="time_series_bars",
        market="usa",
        symbol="SPY",
        trading_date=date(2024, 5, 20),
        resolution="minute",
        data_type="trade",
        provider="polygon",
        price_adjustment_mode="raw",
        data_contract_hash="a" * 64,
        file_path="equity/usa/minute/spy/20240520_trade.zip",
        file_sha256=file_sha256,
        row_count=2,
        first_bar_start_ms=1,
        last_bar_start_ms=2,
    )


def test_decide_claim_outcome_proceeds_on_fresh_claim():
    decision = decide_claim_outcome(claim_result=42, existing=None, content_hash="deadbeef")
    assert decision == ClaimDecision(action="proceed")


def test_decide_claim_outcome_skips_duplicate_when_hash_matches():
    decision = decide_claim_outcome(claim_result=None, existing=_record("deadbeef"), content_hash="deadbeef")
    assert decision.action == "skip_duplicate"


def test_decide_claim_outcome_refuses_conflict_on_hash_mismatch():
    decision = decide_claim_outcome(claim_result=None, existing=_record("aaaa"), content_hash="bbbb")
    assert decision.action == "conflict"
    assert "aaaa" in decision.detail
    assert "bbbb" in decision.detail


def test_decide_claim_outcome_flags_in_flight_when_no_existing_complete_row():
    decision = decide_claim_outcome(claim_result=None, existing=None, content_hash="deadbeef")
    assert decision.action == "in_flight_or_incomplete"


# ---------------------------------------------------------------------------
# check_lake_root_mode (one lake root per adjustment mode, pure/filesystem-only)
# ---------------------------------------------------------------------------


def test_check_lake_root_mode_allows_first_use_on_empty_root(tmp_path: Path):
    # No marker yet, and no lake tree at all -- any mode is fine.
    lake_dir = tmp_path / "lake"
    check_lake_root_mode(tmp_path, lake_dir, "raw")
    check_lake_root_mode(tmp_path, lake_dir, "polygon_split_adjusted")


def test_check_lake_root_mode_allows_matching_committed_mode(tmp_path: Path):
    from app.data_lake.cache_import import _commit_lake_root_mode

    lake_dir = tmp_path / "lake"
    _commit_lake_root_mode(tmp_path, "raw")
    check_lake_root_mode(tmp_path, lake_dir, "raw")  # must not raise


def test_check_lake_root_mode_refuses_conflicting_mode(tmp_path: Path):
    from app.data_lake.cache_import import LakeRootModeConflictError, _commit_lake_root_mode

    lake_dir = tmp_path / "lake"
    _commit_lake_root_mode(tmp_path, "raw")

    with pytest.raises(LakeRootModeConflictError):
        check_lake_root_mode(tmp_path, lake_dir, "polygon_split_adjusted")


def test_check_lake_root_mode_refuses_unmarked_nonempty_root(tmp_path: Path):
    """The reviewer's scenario, at the pure-function level: a lake tree
    already has a real file (e.g. from ensure_data's live pipeline) but was
    never stamped with this importer's marker. Must not be treated as a
    fresh, safe-to-claim root."""
    lake_dir = tmp_path / "lake"
    (lake_dir / "equity" / "usa" / "minute" / "spy").mkdir(parents=True)
    (lake_dir / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip").write_bytes(b"real raw bytes")

    with pytest.raises(LakeRootModeConflictError):
        check_lake_root_mode(tmp_path, lake_dir, "polygon_split_adjusted")


def test_check_lake_root_mode_claim_unmarked_root_as_allows_matching_mode(tmp_path: Path):
    lake_dir = tmp_path / "lake"
    (lake_dir / "equity" / "usa" / "minute" / "spy").mkdir(parents=True)
    (lake_dir / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip").write_bytes(b"real raw bytes")

    # Must not raise: the operator has explicitly asserted this root's mode.
    check_lake_root_mode(tmp_path, lake_dir, "raw", claim_unmarked_root_as="raw")


def test_check_lake_root_mode_claim_unmarked_root_as_does_not_override_a_mismatch(tmp_path: Path):
    """The flag asserts a specific mode -- it must not act as a blanket
    bypass for a *different* mode than the one it names."""
    lake_dir = tmp_path / "lake"
    (lake_dir / "equity" / "usa" / "minute" / "spy").mkdir(parents=True)
    (lake_dir / "equity" / "usa" / "minute" / "spy" / "20240520_trade.zip").write_bytes(b"real raw bytes")

    with pytest.raises(LakeRootModeConflictError):
        check_lake_root_mode(tmp_path, lake_dir, "polygon_split_adjusted", claim_unmarked_root_as="raw")


# ---------------------------------------------------------------------------
# import_cache_root orchestration (live Postgres, skip if unconfigured)
# ---------------------------------------------------------------------------


def _postgres_url() -> str:
    url = settings.POSTGRES_URL or os.getenv("POSTGRES_URL", "")
    if not url:
        pytest.skip("POSTGRES_URL not configured")
    return url


@pytest.fixture
async def clean_artifacts():
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()
    yield
    conn = await asyncpg.connect(_postgres_url())
    try:
        await conn.execute('TRUNCATE TABLE "DataLakeArtifacts" RESTART IDENTITY CASCADE')
    finally:
        await conn.close()


@pytest.fixture
async def pool():
    await catalog_client.close_pool()
    await catalog_client.init_pool()
    yield
    await catalog_client.close_pool()


def _build_cache(tmp_path: Path, symbol: str, dates: list[date], *, adjusted: bool) -> Path:
    cache_root = tmp_path / "cache"
    for d in dates:
        _write_valid_zip(cache_root, symbol, d)
    _write_provenance(cache_root, symbol, adjusted=adjusted)
    return cache_root


@pytest.mark.parametrize("adjusted", [False, True])
@pytest.mark.asyncio
async def test_import_cache_root_creates_complete_rows_under_true_adjustment_mode(
    clean_artifacts, pool, tmp_path: Path, adjusted: bool
):
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20), date(2024, 5, 21)], adjusted=adjusted)
    lake_root = tmp_path / "lake-root"

    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert report.failed == []
    assert len(report.imported) == 2
    expected_mode = "polygon_split_adjusted" if adjusted else "raw"
    assert all(a.price_adjustment_mode == expected_mode for a in report.imported)

    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch('SELECT * FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert len(rows) == 2
    for row in rows:
        assert row["Status"] == "complete"
        assert row["PriceAdjustmentMode"] == expected_mode
        params = json.loads(row["ProviderParams"])
        assert params["imported_from_cache"] is True
        assert "fetches" in params["original_provenance"]

    # The physical zip bytes were placed under the lake's canonical layout,
    # untouched.
    for a in report.imported:
        lake_zip = lake_root / "lake" / "equity" / "usa" / "minute" / "spy" / f"{a.trading_date.strftime('%Y%m%d')}_trade.zip"
        assert lake_zip.is_file()
        cache_zip = cache_root / "equity" / "usa" / "minute" / "spy" / f"{a.trading_date.strftime('%Y%m%d')}_trade.zip"
        assert lake_zip.read_bytes() == cache_zip.read_bytes()


@pytest.mark.parametrize("adjusted", [False, True])
@pytest.mark.asyncio
async def test_import_cache_root_is_idempotent_on_rerun(clean_artifacts, pool, tmp_path: Path, adjusted: bool):
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=adjusted)
    lake_root = tmp_path / "lake-root"

    first = await import_cache_root(cache_root=cache_root, lake_root=lake_root)
    assert len(first.imported) == 1

    second = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert second.imported == []
    assert len(second.skipped) == 1
    assert second.skipped[0].reason == "already_imported_same_hash"
    assert second.failed == []

    conn = await asyncpg.connect(_postgres_url())
    try:
        count = await conn.fetchval('SELECT count(*) FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert count == 1


@pytest.mark.parametrize("adjusted", [False, True])
@pytest.mark.asyncio
async def test_import_cache_root_refuses_overwrite_on_hash_mismatch(
    clean_artifacts, pool, tmp_path: Path, adjusted: bool
):
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=adjusted)
    lake_root = tmp_path / "lake-root"

    first = await import_cache_root(cache_root=cache_root, lake_root=lake_root)
    assert len(first.imported) == 1
    original_hash = first.imported[0].file_sha256

    # Simulate cache drift: the cache zip's content changes after the first
    # import (e.g. a fixture regenerated by a different tool run).
    mutated_bars = [_bar(9, 30, trading_date, "999.99")]
    zip_path = cache_root / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    zip_path.write_bytes(
        build_minute_trade_zip_bytes(symbol="SPY", trading_date_yyyymmdd=trading_date.strftime("%Y%m%d"), bars=mutated_bars)
    )

    second = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert second.imported == []
    assert second.skipped == []
    assert len(second.failed) == 1
    assert second.failed[0].reason == "hash_conflict"

    # Neither the catalog row nor the on-disk lake file were overwritten.
    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "FileSha256" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert row["FileSha256"] == original_hash
    lake_zip = lake_root / "lake" / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    assert hashlib.sha256(lake_zip.read_bytes()).hexdigest() == original_hash


@pytest.mark.asyncio
async def test_import_cache_root_refuses_corrupt_zip_with_no_catalog_row_but_imports_the_rest(
    clean_artifacts, pool, tmp_path: Path
):
    cache_root = tmp_path / "cache"
    good_date = date(2024, 5, 20)
    bad_date = date(2024, 5, 21)
    _write_valid_zip(cache_root, "SPY", good_date)
    _write_provenance(cache_root, "SPY", adjusted=False)

    bad_dir = cache_root / "equity" / "usa" / "minute" / "spy"
    (bad_dir / f"{bad_date.strftime('%Y%m%d')}_trade.zip").write_bytes(b"garbage, not a zip")

    lake_root = tmp_path / "lake-root"
    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert len(report.imported) == 1
    assert report.imported[0].trading_date == good_date
    assert len(report.failed) == 1
    assert report.failed[0].trading_date == bad_date
    assert report.failed[0].reason == "corrupt_zip"

    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch('SELECT "TradingDate" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert [r["TradingDate"] for r in rows] == [good_date]


@pytest.mark.asyncio
async def test_import_cache_root_missing_provenance_fails_without_guessing_mode(
    clean_artifacts, pool, tmp_path: Path
):
    cache_root = tmp_path / "cache"
    _write_valid_zip(cache_root, "SPY", date(2024, 5, 20))
    # No provenance file written for SPY.

    lake_root = tmp_path / "lake-root"
    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert report.imported == []
    assert len(report.failed) == 1
    assert report.failed[0].reason == "missing_provenance"

    conn = await asyncpg.connect(_postgres_url())
    try:
        count = await conn.fetchval('SELECT count(*) FROM "DataLakeArtifacts"')
    finally:
        await conn.close()
    assert count == 0


@pytest.mark.asyncio
async def test_import_cache_root_makes_zero_provider_calls(clean_artifacts, pool, tmp_path: Path):
    """Wrapping the run in an httpx mock with zero registered routes means any
    accidental network call raises instead of silently reaching a real host."""
    import respx

    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=True)
    lake_root = tmp_path / "lake-root"

    with respx.mock:
        report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert len(report.imported) == 1


@pytest.mark.asyncio
async def test_import_cache_root_refuses_second_mode_into_same_lake_root(
    clean_artifacts, pool, tmp_path: Path
):
    """Raw + adjusted zips for the same (symbol, date) resolve to the same
    on-disk path (LeanMinuteBarPath carries no adjustment-mode component).
    Importing a 'raw' cache into a --lake-root and then an adjusted cache
    into the *same* --lake-root must refuse the second mode wholesale,
    never silently overwrite the first mode's bytes."""
    raw_cache = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)
    adjusted_cache = _build_cache(tmp_path / "adjusted-src", "QQQ", [date(2024, 5, 21)], adjusted=True)
    lake_root = tmp_path / "lake-root"

    first = await import_cache_root(cache_root=raw_cache, lake_root=lake_root)
    assert len(first.imported) == 1
    assert first.imported[0].price_adjustment_mode == "raw"

    second = await import_cache_root(cache_root=adjusted_cache, lake_root=lake_root)

    assert second.imported == []
    assert len(second.failed) == 1
    assert second.failed[0].reason == "lake_root_mode_conflict"
    assert second.failed[0].symbol == "QQQ"

    # The raw row from the first run is untouched, and no QQQ row exists.
    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch('SELECT "Symbol", "PriceAdjustmentMode" FROM "DataLakeArtifacts"')
    finally:
        await conn.close()
    assert [(r["Symbol"], r["PriceAdjustmentMode"]) for r in rows] == [("SPY", "raw")]


@pytest.mark.asyncio
async def test_import_cache_root_marks_failed_not_stranded_when_write_fails(
    clean_artifacts, pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failure between claim and complete must not leave the row stuck in
    'fetching' forever -- it is explicitly marked 'failed' so an external
    steal/retry tool (not this one-shot importer) can recover it later."""
    import app.data_lake.cache_import as cache_import_module

    def _boom(**kwargs):
        raise RuntimeError("disk full (simulated)")

    monkeypatch.setattr(cache_import_module, "atomic_write_and_promote", _boom)

    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)
    lake_root = tmp_path / "lake-root"

    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert report.imported == []
    assert len(report.failed) == 1
    assert report.failed[0].reason == "write_failed"

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "Status" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert row is not None
    assert row["Status"] == "failed"  # never left stuck in 'fetching'


@pytest.mark.asyncio
async def test_import_cache_root_zero_zips_warns_distinctly(
    clean_artifacts, pool, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A typo'd --cache-root that resolves to an empty (or non-cache)
    directory must not look indistinguishable from "fully imported, nothing
    left to do" -- it should say plainly that nothing was found."""
    empty_cache_root = tmp_path / "typo-ed-cache-root"
    empty_cache_root.mkdir()
    lake_root = tmp_path / "lake-root"

    with caplog.at_level("WARNING"):
        report = await import_cache_root(cache_root=empty_cache_root, lake_root=lake_root)

    assert report.imported == []
    assert report.failed == []
    assert any("zero trade zips" in record.message for record in caplog.records)


def _seed_real_lake_file(lake_root: Path, symbol: str, trading_date: date, content: bytes) -> Path:
    """Place a file at the exact LeanMinuteBarPath location under
    ``lake_root/lake``, with no cache_import marker -- simulating a root
    ensure_data's live pipeline already populated."""
    lake_dir = lake_root / "lake"
    rel = LeanMinuteBarPath(market="usa", symbol=symbol, trading_date=trading_date, data_type="trade").relative_path()
    dest = lake_dir / Path(*rel.parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


@pytest.mark.asyncio
async def test_import_cache_root_refuses_unmarked_nonempty_lake_root(clean_artifacts, pool, tmp_path: Path):
    """Layer 1, end to end: the reviewer's demonstrated scenario -- a real
    file already sits at the exact destination an adjusted import would
    target, but the root carries no cache_import marker (because
    ensure_data's live pipeline, not this importer, put it there). Must be
    refused, not treated as a fresh root."""
    lake_root = tmp_path / "lake-root"
    real_bytes = b"pretend this is ensure_data's real raw fetch bytes"
    dest = _seed_real_lake_file(lake_root, "SPY", date(2024, 5, 20), real_bytes)

    adjusted_cache = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=True)

    report = await import_cache_root(cache_root=adjusted_cache, lake_root=lake_root)

    assert report.imported == []
    assert len(report.failed) == 1
    assert report.failed[0].reason == "lake_root_mode_conflict"
    assert dest.read_bytes() == real_bytes  # never touched

    conn = await asyncpg.connect(_postgres_url())
    try:
        count = await conn.fetchval('SELECT count(*) FROM "DataLakeArtifacts"')
    finally:
        await conn.close()
    assert count == 0


@pytest.mark.asyncio
async def test_import_cache_root_claim_unmarked_root_as_stamps_and_proceeds(
    clean_artifacts, pool, tmp_path: Path
):
    """The remedy for the refusal above: an explicit operator assertion lets
    the import through and stamps the marker for subsequent runs."""
    lake_root = tmp_path / "lake-root"
    # A pre-existing file for a *different* symbol/date than what's being
    # imported, so this test isolates the marker/emptiness gate from the
    # file-level guard (covered separately below).
    _seed_real_lake_file(lake_root, "QQQ", date(2024, 1, 2), b"unrelated pre-existing raw content")

    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)

    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root, claim_unmarked_root_as="raw")

    assert report.failed == []
    assert len(report.imported) == 1
    assert report.imported[0].price_adjustment_mode == "raw"

    from app.data_lake.cache_import import _read_lake_root_mode

    assert _read_lake_root_mode(lake_root) == "raw"


@pytest.mark.asyncio
async def test_import_cache_root_refuses_destination_file_conflict_even_with_marker_set(
    clean_artifacts, pool, tmp_path: Path
):
    """Layer 2: the file-level guard must catch a destination collision even
    when layer 1 (the marker) would have let the run through -- e.g. a file
    placed out-of-band without the catalog ever knowing about it. Proves
    byte-clobbering is impossible even if the marker layer is bypassed or
    simply wrong."""
    lake_root = tmp_path / "lake-root"
    trading_date = date(2024, 5, 20)
    conflicting_bytes = b"some other content already sitting at this exact path"
    dest = _seed_real_lake_file(lake_root, "SPY", trading_date, conflicting_bytes)

    from app.data_lake.cache_import import _commit_lake_root_mode

    _commit_lake_root_mode(lake_root, "raw")  # pre-stamped -- layer 1 lets this through

    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=False)

    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert report.imported == []
    assert len(report.failed) == 1
    assert report.failed[0].reason == "destination_file_conflict"
    assert dest.read_bytes() == conflicting_bytes  # os.replace was never called

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "Status" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert row is not None
    assert row["Status"] == "failed"


@pytest.mark.asyncio
async def test_import_cache_root_completes_claim_when_destination_file_already_matches(
    clean_artifacts, pool, tmp_path: Path
):
    """If the exact same bytes are already sitting at the destination (no
    catalog row for them yet), the file-level guard treats it as an
    idempotent match: no redundant write, but the freshly-claimed row still
    gets completed rather than left stuck in 'fetching'. The marker is
    pre-stamped so layer 1 (marker/emptiness) isn't what's under test here
    -- layer 2 (the file-level guard) is."""
    lake_root = tmp_path / "lake-root"
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=False)
    zip_bytes = (
        cache_root / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    ).read_bytes()
    _seed_real_lake_file(lake_root, "SPY", trading_date, zip_bytes)  # identical bytes already there

    from app.data_lake.cache_import import _commit_lake_root_mode

    _commit_lake_root_mode(lake_root, "raw")

    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert report.failed == []
    assert len(report.imported) == 1
    assert report.imported[0].file_sha256 == hashlib.sha256(zip_bytes).hexdigest()

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "Status" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert row["Status"] == "complete"
