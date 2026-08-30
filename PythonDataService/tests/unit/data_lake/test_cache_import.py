"""Unit tests for the lean-cache -> lake catalog import (#1832).

Pure-function tests (discovery, provenance loading, zip verification, DCH
tagging, provenance-preservation, and the claim/destination decisions that
encode idempotency + no-overwrite) need no database and always run.

Orchestration tests (``import_cache_root``) exercise the real catalog via
live Postgres, following the same skip-if-unconfigured pattern as
``test_ensure_data.py`` / ``test_catalog_write_ops.py``. They are
parametrized by adjustment mode: both cases pass against the schema as it
stands today (issue #1878 dropped the constraint that used to gate the
'polygon_split_adjusted' path). They all use the ``lake_root`` fixture,
which patches ``settings.LEAN_DATA_WRITE_ROOT`` to match the tmp_path root
they use and stamps it with a ``.data-root.json`` marker -- ``import_cache_root``
requires any ``--lake-root`` to carry a valid marker (issue #1878, PR B of
#1861; the old "must equal the canonical configured root" rule from finding
7 of the Codex round-1 review is gone -- any marked root is now accepted).
"""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app.config import settings
from app.data_lake import catalog_client, root_identity
from app.data_lake.cache_import import (
    ClaimDecision,
    CorruptCacheZipError,
    DestinationDecision,
    LakeRootIdentityError,
    MissingProvenanceError,
    UnrecognizedCacheEntry,
    build_provider_params,
    decide_claim_outcome,
    decide_destination_outcome,
    discover_cache_zips,
    import_cache_root,
    load_symbol_provenance,
    price_adjustment_mode_for,
    provenance_covers_date,
    verify_and_read_zip,
)
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath, lake_subpath, resolve_lake_root
from app.data_lake.types import ArtifactRecord
from app.lean_sidecar.trading_calendar import session_open_ms_utc

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


def _write_provenance(
    cache_root: Path,
    symbol: str,
    *,
    adjusted: bool,
    fetches: list[dict] | None = None,
    doc_overrides: dict | None = None,
) -> Path:
    prov_dir = cache_root / "provenance"
    prov_dir.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": 1,
        "symbol": symbol.upper(),
        "policy": {"source": "polygon", "adjusted": adjusted},
        "fetches": fetches
        if fetches is not None
        else [
            # Wide enough to cover every date the rest of this file's
            # default-fetches tests use, without each of them having to
            # know or care about finding 2's per-artifact coverage check.
            # Tests exercising that check pass their own narrow `fetches`.
            {
                "resolution": "minute",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
                "fetched_at_ms": 1_700_000_000_000,
            }
        ],
    }
    if doc_overrides:
        doc.update(doc_overrides)
    path = prov_dir / f"{symbol.lower()}.json"
    path.write_text(json.dumps(doc))
    return path


def _seed_real_lake_file(
    lake_root: Path, symbol: str, trading_date: date, content: bytes, *, mode: str = "raw"
) -> Path:
    """Place a file at the exact LeanMinuteBarPath location under
    ``lake_root/lake/<mode>`` -- simulating a root ensure_data's live
    pipeline already populated."""
    lake_dir = lake_root / lake_subpath(mode)
    rel = LeanMinuteBarPath(market="usa", symbol=symbol, trading_date=trading_date, data_type="trade").relative_path()
    dest = lake_dir / Path(*rel.parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return dest


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
# _unrecognized_to_failures (pure translation; the only producer of a
# FailedArtifact with trading_date=None)
# ---------------------------------------------------------------------------


def test_unrecognized_to_failures_produces_reason_and_no_trading_date():
    from app.data_lake.cache_import import _unrecognized_to_failures

    entries = [
        UnrecognizedCacheEntry(symbol="SPY", path=Path("/whatever/spy_2024-05-21_trade.zip"), detail="bad filename")
    ]

    failures = _unrecognized_to_failures(entries)

    assert len(failures) == 1
    assert failures[0].symbol == "SPY"
    assert failures[0].trading_date is None
    assert failures[0].reason == "unrecognized_filename"
    assert failures[0].detail == "bad filename"


# ---------------------------------------------------------------------------
# load_symbol_provenance / price_adjustment_mode_for (adjustment-mode tagging
# and full-document validation: schema shape, symbol match, policy.source)
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


def test_load_symbol_provenance_not_a_json_object_raises(tmp_path: Path):
    prov_dir = tmp_path / "provenance"
    prov_dir.mkdir(parents=True)
    (prov_dir / "spy.json").write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


def test_load_symbol_provenance_wrong_schema_version_raises(tmp_path: Path):
    _write_provenance(tmp_path, "SPY", adjusted=False, doc_overrides={"schema_version": 2})
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


def test_load_symbol_provenance_symbol_mismatch_raises(tmp_path: Path):
    # File lives under provenance/spy.json (the directory-derived symbol is
    # "SPY") but the document itself claims a different symbol.
    _write_provenance(tmp_path, "SPY", adjusted=False, doc_overrides={"symbol": "QQQ"})
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


def test_load_symbol_provenance_non_polygon_source_raises(tmp_path: Path):
    _write_provenance(tmp_path, "SPY", adjusted=False, doc_overrides={"policy": {"source": "ibkr", "adjusted": False}})
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


def test_load_symbol_provenance_missing_fetches_list_raises(tmp_path: Path):
    _write_provenance(tmp_path, "SPY", adjusted=False, doc_overrides={"fetches": "not-a-list"})
    with pytest.raises(MissingProvenanceError):
        load_symbol_provenance(tmp_path, "SPY")


# ---------------------------------------------------------------------------
# provenance_covers_date (per-artifact date-range coverage, pure)
# ---------------------------------------------------------------------------


def _provenance(fetches: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "symbol": "SPY",
        "policy": {"source": "polygon", "adjusted": False},
        "fetches": fetches,
    }


def test_provenance_covers_date_true_when_within_a_minute_fetch_range():
    provenance = _provenance(
        [{"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-05-20", "fetched_at_ms": 1}]
    )
    assert provenance_covers_date(provenance, date(2024, 5, 10)) is True
    # Inclusive on both ends.
    assert provenance_covers_date(provenance, date(2024, 5, 1)) is True
    assert provenance_covers_date(provenance, date(2024, 5, 20)) is True


def test_provenance_covers_date_false_when_outside_every_range():
    provenance = _provenance(
        [{"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-05-20", "fetched_at_ms": 1}]
    )
    assert provenance_covers_date(provenance, date(2024, 6, 1)) is False


def test_provenance_covers_date_ignores_non_minute_resolution():
    provenance = _provenance(
        [{"resolution": "daily", "from_date": "2024-05-01", "to_date": "2024-05-20", "fetched_at_ms": 1}]
    )
    assert provenance_covers_date(provenance, date(2024, 5, 10)) is False


def test_provenance_covers_date_skips_malformed_entries_without_raising():
    provenance = _provenance(
        [
            {"resolution": "minute", "from_date": "not-a-date", "to_date": "2024-05-20"},
            "not even a dict",
            {"resolution": "minute"},  # missing dates entirely
        ]
    )
    assert provenance_covers_date(provenance, date(2024, 5, 10)) is False


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

    assert _import_minute_trade_dch(adjusted=False) != _minute_trade_dch("raw")


# ---------------------------------------------------------------------------
# build_provider_params (provenance preservation + temporal-rigor anchoring)
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
    # evidence of the refetch leak (#1830) -- survives byte-for-byte,
    # untouched, as an opaque audit document (not re-parsed for values below).
    assert params["original_provenance"] == provenance
    assert params["original_provenance"]["fetches"] == fetches


def test_build_provider_params_anchors_fetch_ranges_to_int64_ms_via_canonical_calendar(tmp_path: Path):
    """finding 5: the original document's ISO from_date/to_date strings must
    not be the only temporal representation persisted -- a first-class,
    session-open-anchored int64-ms field is required at the top level,
    computed via the canonical calendar (app.lean_sidecar.trading_calendar),
    never a hardcoded UTC-midnight or fixed-offset guess."""
    fetches = [
        {"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-05-10", "fetched_at_ms": 1},
        {"resolution": "daily", "from_date": "2024-01-01", "to_date": "2024-01-02", "fetched_at_ms": 2},
    ]
    _write_provenance(tmp_path, "SPY", adjusted=False, fetches=fetches)
    provenance = load_symbol_provenance(tmp_path, "SPY")

    params = build_provider_params(tmp_path, provenance)

    # Both entries anchor (provenance_covers_date filters by resolution for
    # *coverage* purposes, but build_provider_params anchors every fetch
    # entry with parseable dates regardless of resolution -- it's an audit
    # trail of everything that was fetched, not a coverage computation).
    assert len(params["fetch_ranges_ms"]) == 2
    first = params["fetch_ranges_ms"][0]
    assert first["from_date_ms"] == session_open_ms_utc(date(2024, 5, 1))
    assert first["to_date_ms"] == session_open_ms_utc(date(2024, 5, 10))
    assert all(isinstance(v, int) for entry in params["fetch_ranges_ms"] for v in entry.values())


def test_build_provider_params_does_not_mutate_the_original_document(tmp_path: Path):
    _write_provenance(tmp_path, "SPY", adjusted=False)
    provenance = load_symbol_provenance(tmp_path, "SPY")
    original_copy = json.loads(json.dumps(provenance))

    build_provider_params(tmp_path, provenance)

    assert provenance == original_copy


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


def test_verify_and_read_zip_bad_crc_member_raises(tmp_path: Path):
    """finding 6: a corrupted (bad-CRC) member raises zipfile.BadZipFile at
    *read* time, distinct from the open-time BadZipFile the "not a valid zip
    file at all" test exercises -- must not escape as an uncaught error."""
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    csv_name = "20240520_spy_minute_trade.csv"
    body = b"34200000,5000000,5000000,5000000,5000000,100\n"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(csv_name, body)

    # ZIP_STORED means the "compressed" bytes are the content bytes
    # verbatim, so flipping one content byte in place breaks the stored CRC
    # without touching the zip's structure at all.
    raw = bytearray(zip_path.read_bytes())
    marker = body[:8]
    idx = raw.find(marker)
    assert idx != -1
    raw[idx] ^= 0xFF
    zip_path.write_bytes(bytes(raw))

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


def test_verify_and_read_zip_negative_price_raises(tmp_path: Path):
    """finding 10: a negative price field is upstream corruption, refused
    the same way app.data_lake.lean_writer.to_deci_cent refuses it."""
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("20240520_spy_minute_trade.csv", "34200000,-5000000,5000000,5000000,5000000,100\n")

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


def test_verify_and_read_zip_duplicate_timestamp_raises(tmp_path: Path):
    """finding 1: finite ingestion is fail-fast -- a duplicate
    ms_since_midnight is refused, never silently deduplicated."""
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "20240520_spy_minute_trade.csv",
            "34200000,5000000,5000000,5000000,5000000,100\n34200000,5001000,5001000,5001000,5001000,50\n",
        )

    with pytest.raises(CorruptCacheZipError):
        verify_and_read_zip(zip_path, "SPY", trading_date)


def test_verify_and_read_zip_out_of_order_timestamp_raises(tmp_path: Path):
    """finding 1: an out-of-order row is refused, never silently reordered."""
    trading_date = date(2024, 5, 20)
    day_dir = tmp_path / "equity" / "usa" / "minute" / "spy"
    day_dir.mkdir(parents=True)
    zip_path = day_dir / "20240520_trade.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "20240520_spy_minute_trade.csv",
            "34260000,5000000,5000000,5000000,5000000,100\n34200000,5001000,5001000,5001000,5001000,50\n",
        )

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
# decide_destination_outcome (layer 2: file-level guard, pure -- no fixtures,
# no Postgres. This is the CI-executed coverage for the guard that otherwise
# only lived in Postgres-gated orchestration tests the PR gate never runs.)
# ---------------------------------------------------------------------------


def test_decide_destination_outcome_writes_when_nothing_at_destination():
    decision = decide_destination_outcome(existing_dest_hash=None, content_hash="deadbeef")
    assert decision == DestinationDecision(action="write")


def test_decide_destination_outcome_already_present_when_hash_matches():
    decision = decide_destination_outcome(existing_dest_hash="deadbeef", content_hash="deadbeef")
    assert decision.action == "already_present"


def test_decide_destination_outcome_conflict_when_hash_differs():
    decision = decide_destination_outcome(existing_dest_hash="aaaa", content_hash="bbbb")
    assert decision.action == "conflict"
    assert "aaaa" in decision.detail
    assert "bbbb" in decision.detail


# ---------------------------------------------------------------------------
# One lake root per adjustment mode -- now structural, not enforced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        ("raw", "polygon_split_adjusted"),
        ("raw", "lean_adjusted"),
        ("polygon_split_adjusted", "lean_adjusted"),
    ],
)
def test_two_modes_never_resolve_to_the_same_artifact_path(left: str, right: str):
    """The regression that replaces the whole-root marker gate (#1839).

    Six tests used to live here, exercising ``check_lake_root_mode``: a
    marker committing a tree to one mode, an emptiness probe, an operator
    ``--claim-unmarked-root-as`` override, and the refusals around them. All
    of it existed because ``LeanMinuteBarPath`` carried no mode component, so
    a 'raw' and a 'polygon_split_adjusted' artifact for one (symbol, date)
    resolved to the *identical* file and either could overwrite the other.

    The mode is now a segment of the root itself, so the collision cannot be
    constructed -- which is why the gate is gone rather than ported. This
    asserts the property the gate was protecting, directly: different modes,
    same artifact identity, different absolute paths.
    """
    rel = LeanMinuteBarPath(
        market="usa", symbol="SPY", trading_date=date(2024, 5, 20), data_type="trade"
    ).relative_path()

    left_path = resolve_lake_root(left) / rel
    right_path = resolve_lake_root(right) / rel

    assert left_path != right_path
    # ...and the divergence is the mode segment alone: the LEAN-relative tail
    # is identical, which is what keeps catalog FilePath root-relative and
    # unchanged, and what lets LEAN read either root unmodified.
    assert left_path.parts[-len(rel.parts):] == right_path.parts[-len(rel.parts):]


def test_the_mode_segment_sits_above_the_lean_tree():
    """LEAN must find ``equity/`` directly inside whatever root it is given.

    If the mode were inserted *inside* the LEAN tree instead, every reader
    and the sidecar mount would need to learn a non-LEAN layout. Pinning the
    segment's position is what makes "no reader changes" true rather than
    incidental.
    """
    root = resolve_lake_root("polygon_split_adjusted")

    assert root.name == "polygon_split_adjusted"
    assert root.parent.name == "lake"
    assert (root / "equity" / "usa" / "minute").parts[-3:] == ("equity", "usa", "minute")


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


def _marked_root(base: Path, name: str, root_id: UUID) -> Path:
    """Build and stamp a fresh physical root at ``base / name`` with
    ``root_id``, via the same administrative path a real operator uses
    (``root_identity.init_empty_root``) -- never hand-writing the marker
    JSON, so these tests exercise the real validation path."""
    root = base / name
    root_identity.init_empty_root(root, root_id)
    return root


@pytest.fixture
def lake_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The default lake root for orchestration tests: a tmp_path directory
    with settings.LEAN_DATA_WRITE_ROOT patched to match it (several other
    fixtures/helpers in this file still read that setting), stamped with the
    service's own default active-root id (issue #1878's ``import_cache_root``
    requires any ``--lake-root`` to carry a valid marker; this keeps the
    identity every pre-#1878 test in this file implicitly assumed)."""
    root = _marked_root(tmp_path, "lake-root", root_identity.active_root_id())
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(root))
    return root


def _build_cache(tmp_path: Path, symbol: str, dates: list[date], *, adjusted: bool) -> Path:
    cache_root = tmp_path / "cache"
    for d in dates:
        _write_valid_zip(cache_root, symbol, d)
    _write_provenance(cache_root, symbol, adjusted=adjusted)
    return cache_root


@pytest.mark.asyncio
async def test_import_cache_root_refuses_unmarked_root(clean_artifacts, pool, tmp_path: Path):
    """Issue #1878: a --lake-root with no .data-root.json marker at all is
    refused -- this importer never stamps a root itself, unmarked or not.
    Deliberately does NOT use the lake_root fixture (which stamps a marker),
    so the directory genuinely carries none."""
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)
    unmarked_root = tmp_path / "unmarked-root"

    with pytest.raises(LakeRootIdentityError):
        await import_cache_root(cache_root=cache_root, lake_root=unmarked_root)

    # Nothing was written anywhere -- the marker check runs before any I/O.
    conn = await asyncpg.connect(_postgres_url())
    try:
        count = await conn.fetchval('SELECT count(*) FROM "DataLakeArtifacts"')
    finally:
        await conn.close()
    assert count == 0


@pytest.mark.asyncio
async def test_import_cache_root_refuses_malformed_marker(clean_artifacts, pool, tmp_path: Path):
    """A marker that exists but doesn't parse (wrong schema_version here)
    must never be treated the same as no marker -- root_identity.read_marker
    raises for it, and import_cache_root propagates that refusal rather than
    silently proceeding."""
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)
    malformed_root = tmp_path / "malformed-root"
    marker_dir = malformed_root / "lake"
    marker_dir.mkdir(parents=True)
    (marker_dir / ".data-root.json").write_text(json.dumps({"schema_version": 99, "data_root_id": str(root_identity.active_root_id())}))

    with pytest.raises(LakeRootIdentityError):
        await import_cache_root(cache_root=cache_root, lake_root=malformed_root)


@pytest.mark.asyncio
async def test_import_cache_root_uses_the_markers_root_id_not_the_active_root(
    clean_artifacts, pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The regression #1861 exists to prevent, from the write side: a root's
    catalog identity comes from *that root's own marker*, never from the
    service's configured active root -- even when they disagree. Configures
    an active root the marker deliberately does not match, so a bug that
    fell back to active_root_id() would be caught here."""
    configured_active_root = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(configured_active_root))
    marked_root_id = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    other_root = _marked_root(tmp_path, "other-root", marked_root_id)
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)

    report = await import_cache_root(cache_root=cache_root, lake_root=other_root)

    assert len(report.imported) == 1
    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "DataRootId" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert row["DataRootId"] == marked_root_id
    assert row["DataRootId"] != configured_active_root


@pytest.mark.asyncio
async def test_import_cache_root_into_two_marked_roots_coexist(clean_artifacts, pool, tmp_path: Path):
    """The coexistence proof issue #1878 asks for: the same symbol, date,
    artifact kind, and adjustment mode, imported into two different marked
    roots, produces two rows that both persist -- with the same root-relative
    FilePath -- and each resolves only from its own root. This is the
    regression #1861 exists to prevent: importing into a second physical
    root must never make the first root appear covered, nor collide with it."""
    root_a_id = UUID("11111111-1111-1111-1111-111111111111")
    root_b_id = UUID("22222222-2222-2222-2222-222222222222")
    root_a = _marked_root(tmp_path, "root-a", root_a_id)
    root_b = _marked_root(tmp_path, "root-b", root_b_id)
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=False)

    report_a = await import_cache_root(cache_root=cache_root, lake_root=root_a)
    report_b = await import_cache_root(cache_root=cache_root, lake_root=root_b)

    assert report_a.failed == []
    assert len(report_a.imported) == 1
    assert report_b.failed == []
    assert len(report_b.imported) == 1

    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch('SELECT "DataRootId", "FilePath" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    by_root = {r["DataRootId"]: r["FilePath"] for r in rows}
    assert set(by_root) == {root_a_id, root_b_id}
    # Root-relative FilePath is byte-identical across roots -- the mode
    # segment plus the root itself is what disambiguates on disk, not the
    # catalog column.
    assert by_root[root_a_id] == by_root[root_b_id]

    # Each root's physical bytes landed under its own tree, not the other's.
    expected_rel = LeanMinuteBarPath(
        market="usa", symbol="SPY", trading_date=trading_date, data_type="trade"
    ).relative_path()
    assert (root_a / lake_subpath("raw") / Path(*expected_rel.parts)).is_file()
    assert (root_b / lake_subpath("raw") / Path(*expected_rel.parts)).is_file()

    # Coverage for root A cannot be satisfied by root B's row, and vice
    # versa -- importing into root B left root A's availability unchanged.
    coverage_a = await catalog_client.select_coverage_minute_bars(
        market="usa",
        symbol="SPY",
        data_type="trade",
        start_trading_date=trading_date,
        end_trading_date=trading_date,
        price_adjustment_mode="raw",
        data_root_id=root_a_id,
    )
    coverage_b = await catalog_client.select_coverage_minute_bars(
        market="usa",
        symbol="SPY",
        data_type="trade",
        start_trading_date=trading_date,
        end_trading_date=trading_date,
        price_adjustment_mode="raw",
        data_root_id=root_b_id,
    )
    assert len(coverage_a) == 1
    assert len(coverage_b) == 1
    assert coverage_a[0].id != coverage_b[0].id

    # Storage totals are isolated per root, not summed across both.
    totals_a = await catalog_client.select_storage_totals_by_kind("usa", data_root_id=root_a_id)
    totals_b = await catalog_client.select_storage_totals_by_kind("usa", data_root_id=root_b_id)
    assert sum(t.artifact_count for t in totals_a) == 1
    assert sum(t.artifact_count for t in totals_b) == 1


@pytest.mark.asyncio
async def test_import_cache_root_same_root_duplicate_claim_still_dedupes(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """The new DataRootId-leading ON CONFLICT target must still catch a
    genuine duplicate within one root -- rebuilding the index must not
    accidentally turn dedup off. Two separate import runs against the same
    root and the same cache content is exactly test_import_cache_root_is_idempotent_on_rerun
    below; this asserts the same property directly against the row count so
    a regression in the conflict target shows up as a row-count doubling."""
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)

    await import_cache_root(cache_root=cache_root, lake_root=lake_root)
    await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    conn = await asyncpg.connect(_postgres_url())
    try:
        count = await conn.fetchval('SELECT count(*) FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert count == 1


@pytest.mark.parametrize("adjusted", [False, True])
@pytest.mark.asyncio
async def test_import_cache_root_creates_complete_rows_under_true_adjustment_mode(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path, adjusted: bool
):
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20), date(2024, 5, 21)], adjusted=adjusted)

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
        assert len(params["fetch_ranges_ms"]) >= 1

    # The physical zip bytes were placed under the lake's canonical layout,
    # untouched.
    for a in report.imported:
        lake_zip = (
            lake_root / lake_subpath(expected_mode) / "equity" / "usa" / "minute" / "spy" / f"{a.trading_date.strftime('%Y%m%d')}_trade.zip"
        )
        assert lake_zip.is_file()
        cache_zip = cache_root / "equity" / "usa" / "minute" / "spy" / f"{a.trading_date.strftime('%Y%m%d')}_trade.zip"
        assert lake_zip.read_bytes() == cache_zip.read_bytes()


@pytest.mark.parametrize("adjusted", [False, True])
@pytest.mark.asyncio
async def test_import_cache_root_is_idempotent_on_rerun(clean_artifacts, pool, lake_root: Path, tmp_path: Path, adjusted: bool):
    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=adjusted)

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
    clean_artifacts, pool, lake_root: Path, tmp_path: Path, adjusted: bool
):
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=adjusted)

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
    lake_zip = (
        lake_root
        / lake_subpath("polygon_split_adjusted" if adjusted else "raw")
        / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    )
    assert hashlib.sha256(lake_zip.read_bytes()).hexdigest() == original_hash


@pytest.mark.asyncio
async def test_import_cache_root_refuses_corrupt_zip_with_no_catalog_row_but_imports_the_rest(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    cache_root = tmp_path / "cache"
    good_date = date(2024, 5, 20)
    bad_date = date(2024, 5, 21)
    _write_valid_zip(cache_root, "SPY", good_date)
    _write_provenance(cache_root, "SPY", adjusted=False)

    bad_dir = cache_root / "equity" / "usa" / "minute" / "spy"
    (bad_dir / f"{bad_date.strftime('%Y%m%d')}_trade.zip").write_bytes(b"garbage, not a zip")

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
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    cache_root = tmp_path / "cache"
    _write_valid_zip(cache_root, "SPY", date(2024, 5, 20))
    # No provenance file written for SPY.

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
async def test_import_cache_root_refuses_artifact_not_covered_by_provenance(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """finding 2: a well-formed provenance document that simply doesn't
    attest to a particular day (no minute fetch range covers it) refuses
    that specific artifact, not the whole symbol -- a covered day for the
    same symbol still imports."""
    cache_root = tmp_path / "cache"
    covered_date = date(2024, 5, 10)
    uncovered_date = date(2024, 6, 15)
    _write_valid_zip(cache_root, "SPY", covered_date)
    _write_valid_zip(cache_root, "SPY", uncovered_date)
    _write_provenance(
        cache_root,
        "SPY",
        adjusted=False,
        fetches=[{"resolution": "minute", "from_date": "2024-05-01", "to_date": "2024-05-20", "fetched_at_ms": 1}],
    )

    report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert len(report.imported) == 1
    assert report.imported[0].trading_date == covered_date
    assert len(report.failed) == 1
    assert report.failed[0].trading_date == uncovered_date
    assert report.failed[0].reason == "provenance_coverage_mismatch"

    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch('SELECT "TradingDate" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert [r["TradingDate"] for r in rows] == [covered_date]


@pytest.mark.asyncio
async def test_import_cache_root_makes_zero_provider_calls(clean_artifacts, pool, lake_root: Path, tmp_path: Path):
    """Wrapping the run in an httpx mock with zero registered routes means any
    accidental network call raises instead of silently reaching a real host."""
    import respx

    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=True)

    with respx.mock:
        report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert len(report.imported) == 1


@pytest.mark.asyncio
async def test_import_cache_root_marks_failed_not_stranded_when_write_fails(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failure between claim and complete must not leave the row stuck in
    'fetching' forever -- it is explicitly marked 'failed' so an external
    steal/retry tool (not this one-shot importer) can recover it later."""
    import app.data_lake.cache_import as cache_import_module

    def _boom(**kwargs):
        raise RuntimeError("disk full (simulated)")

    # issue #1888: the "proceed" (fresh-claim) branch now writes through the
    # lease-gated publication interface, not the unconditional
    # atomic_write_and_promote -- see cache_import._import_one_zip.
    monkeypatch.setattr(cache_import_module, "publish_artifact", _boom)

    cache_root = _build_cache(tmp_path, "SPY", [date(2024, 5, 20)], adjusted=False)

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
async def test_import_cache_root_destination_unreadable_fails_artifact_and_continues(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """finding 4: destination inspection happens inside the guarded section
    -- an unreadable destination refuses that one artifact (fail_artifact,
    typed FailedArtifact) and the import continues with the rest, rather
    than aborting the whole run with an uncaught exception."""
    cache_root = tmp_path / "cache"
    unreadable_date = date(2024, 5, 20)
    ok_date = date(2024, 5, 21)
    _write_valid_zip(cache_root, "SPY", unreadable_date)
    _write_valid_zip(cache_root, "SPY", ok_date)
    _write_provenance(cache_root, "SPY", adjusted=False)

    # Pre-create the destination file for one date and strip all
    # permissions, so _inspect_destination's read_bytes() raises
    # PermissionError instead of finding a clean "absent" or "matches" case.
    dest = _seed_real_lake_file(lake_root, "SPY", unreadable_date, b"pre-existing, about to become unreadable")
    dest.chmod(0o000)
    try:
        report = await import_cache_root(cache_root=cache_root, lake_root=lake_root)
    finally:
        dest.chmod(0o644)  # restore so tmp_path teardown never has to care

    assert len(report.imported) == 1
    assert report.imported[0].trading_date == ok_date
    assert len(report.failed) == 1
    assert report.failed[0].trading_date == unreadable_date
    assert report.failed[0].reason == "write_failed"

    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch('SELECT "TradingDate", "Status" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    by_date = {r["TradingDate"]: r["Status"] for r in rows}
    assert by_date[ok_date] == "complete"
    assert by_date[unreadable_date] == "failed"


@pytest.mark.asyncio
async def test_adjusted_import_coexists_with_a_populated_raw_root(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """The scenario the whole-root marker used to refuse, now succeeding.

    Two tests stood here: one asserting that an adjusted import into a root
    ensure_data had already populated with raw bytes was refused wholesale
    (``reason="lake_root_mode_conflict"``), and one asserting that
    ``--claim-unmarked-root-as`` was the operator's way back in. Both
    described a limitation, not a requirement -- and #1839 removed the
    limitation by giving the root an adjustment segment.

    The adjusted import now lands beside the raw bytes rather than being
    refused by them, and the raw file is untouched because nothing adjusted
    ever resolves to its path.
    """
    trading_date = date(2024, 5, 20)
    real_bytes = b"pretend this is ensure_data's real raw fetch bytes"
    raw_dest = _seed_real_lake_file(lake_root, "SPY", trading_date, real_bytes)

    adjusted_cache = _build_cache(tmp_path, "SPY", [trading_date], adjusted=True)

    report = await import_cache_root(cache_root=adjusted_cache, lake_root=lake_root)

    assert report.failed == []
    assert len(report.imported) == 1
    assert report.imported[0].price_adjustment_mode == "polygon_split_adjusted"

    assert raw_dest.read_bytes() == real_bytes
    rel = LeanMinuteBarPath(
        market="usa", symbol="SPY", trading_date=trading_date, data_type="trade"
    ).relative_path()
    adjusted_dest = lake_root / lake_subpath("polygon_split_adjusted") / Path(*rel.parts)
    assert adjusted_dest.is_file()
    assert adjusted_dest.read_bytes() != real_bytes

    conn = await asyncpg.connect(_postgres_url())
    try:
        rows = await conn.fetch(
            'SELECT "PriceAdjustmentMode", "FilePath" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY"
        )
    finally:
        await conn.close()
    # One catalog row, and its FilePath is the LEAN-relative tail with no
    # mode in it -- the mode lives in the identity column, which is what
    # made this change cost zero catalog migration.
    assert [r["PriceAdjustmentMode"] for r in rows] == ["polygon_split_adjusted"]
    assert rows[0]["FilePath"] == str(rel)


@pytest.mark.asyncio
async def test_import_cache_root_refuses_destination_file_conflict_even_with_marker_set(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """Layer 2: the file-level guard must catch a destination collision even
    a file was placed out-of-band without the catalog ever knowing about
    it. The mode segment cannot help here: both writers agree on the mode,
    so they land in the same tree and disagree about content instead."""
    trading_date = date(2024, 5, 20)
    conflicting_bytes = b"some other content already sitting at this exact path"
    dest = _seed_real_lake_file(lake_root, "SPY", trading_date, conflicting_bytes)

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
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """If the exact same bytes are already sitting at the destination (no
    catalog row for them yet), the file-level guard treats it as an
    idempotent match: no redundant write, but the freshly-claimed row still
    gets completed rather than left stuck in 'fetching'."""
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=False)
    zip_bytes = (
        cache_root / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    ).read_bytes()
    _seed_real_lake_file(lake_root, "SPY", trading_date, zip_bytes)  # identical bytes already there


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


@pytest.mark.asyncio
async def test_import_cache_root_restores_missing_destination_on_idempotent_rerun(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """finding 9: the duplicate-skip branch (catalog complete + hash match)
    must not trust the catalog's word alone -- if the physical file has
    gone missing since the first import, re-running restores it from the
    cache zip rather than silently reporting success over nothing."""
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=False)

    first = await import_cache_root(cache_root=cache_root, lake_root=lake_root)
    assert len(first.imported) == 1

    dest = lake_root / lake_subpath("raw") / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    assert dest.is_file()
    dest.unlink()
    assert not dest.exists()

    second = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert second.failed == []
    assert second.imported == []
    assert len(second.skipped) == 1
    assert dest.is_file()  # restored
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == first.imported[0].file_sha256

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "Status", "FileSha256" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    assert row["Status"] == "complete"
    assert row["FileSha256"] == first.imported[0].file_sha256


@pytest.mark.asyncio
async def test_import_cache_root_refuses_when_destination_corrupted_on_idempotent_rerun(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path
):
    """finding 9, the other half: if the physical file has *changed* since
    the first import (rather than gone missing), re-running must refuse,
    not silently trust the catalog's recorded hash over unknown bytes."""
    trading_date = date(2024, 5, 20)
    cache_root = _build_cache(tmp_path, "SPY", [trading_date], adjusted=False)

    first = await import_cache_root(cache_root=cache_root, lake_root=lake_root)
    assert len(first.imported) == 1
    original_hash = first.imported[0].file_sha256

    dest = lake_root / lake_subpath("raw") / "equity" / "usa" / "minute" / "spy" / f"{trading_date.strftime('%Y%m%d')}_trade.zip"
    dest.write_bytes(b"someone corrupted this file out of band")

    second = await import_cache_root(cache_root=cache_root, lake_root=lake_root)

    assert second.imported == []
    assert second.skipped == []
    assert len(second.failed) == 1
    assert second.failed[0].reason == "destination_file_conflict"
    # The corrupted file is left exactly as found -- refusing, not guessing
    # which version (cache zip vs. on-disk) is correct.
    assert dest.read_bytes() == b"someone corrupted this file out of band"

    conn = await asyncpg.connect(_postgres_url())
    try:
        row = await conn.fetchrow('SELECT "Status", "FileSha256" FROM "DataLakeArtifacts" WHERE "Symbol" = $1', "SPY")
    finally:
        await conn.close()
    # The row that used to be 'complete' is now marked 'failed' -- it no
    # longer honestly describes what's on disk.
    assert row["Status"] == "failed"
    assert row["FileSha256"] == original_hash  # untouched, not silently updated


@pytest.mark.asyncio
async def test_import_cache_root_zero_zips_warns_distinctly(
    clean_artifacts, pool, lake_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """A typo'd --cache-root that resolves to an empty (or non-cache)
    directory must not look indistinguishable from "fully imported, nothing
    left to do" -- it should say plainly that nothing was found."""
    empty_cache_root = tmp_path / "typo-ed-cache-root"
    empty_cache_root.mkdir()

    with caplog.at_level("WARNING"):
        report = await import_cache_root(cache_root=empty_cache_root, lake_root=lake_root)

    assert report.imported == []
    assert report.failed == []
    assert any("zero trade zips" in record.message for record in caplog.records)
