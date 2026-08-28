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
    MissingProvenanceError,
    build_provider_params,
    decide_claim_outcome,
    discover_cache_zips,
    import_cache_root,
    load_symbol_provenance,
    price_adjustment_mode_for,
    verify_and_read_zip,
)
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
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

    refs = discover_cache_zips(tmp_path)

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

    refs = discover_cache_zips(tmp_path)

    assert len(refs) == 1
    assert refs[0].zip_path.name == "20240520_trade.zip"


def test_discover_cache_zips_missing_minute_tree_returns_empty(tmp_path: Path):
    assert discover_cache_zips(tmp_path) == []


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


def test_verify_and_read_zip_valid_zip_returns_metadata(tmp_path: Path):
    trading_date = date(2024, 5, 20)
    bars = [_bar(9, 30, trading_date, "500.00"), _bar(9, 31, trading_date, "500.05")]
    zip_path = _write_valid_zip(tmp_path, "SPY", trading_date, bars=bars)

    verified = verify_and_read_zip(zip_path, "SPY", trading_date)

    assert verified.row_count == 2
    assert verified.first_bar_start_ms < verified.last_bar_start_ms
    assert verified.raw_bytes == zip_path.read_bytes()


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
    import hashlib

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
