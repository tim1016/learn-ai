"""Every retained artifact kind is checked by its canonical production reader."""
from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest
import respx

from app.data_lake.derived_daily import aggregate_minute_to_daily, build_daily_zip_bytes
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.root_identity import active_root_id
from app.option_archive.backup import BackupFile, BackupManifest, decode_restored_artifacts
from app.option_archive.snapshot import ChainObservation, snapshot_path
from app.options.contract import OptionContract
from app.utils.timestamps import ny_datetime
from tests._helpers.lake_fixture import seed_lake_corporate_actions, seed_lean_metadata_receipt
from tests.options_backtest._lake import (
    FRIDAY,
    FRIDAY_OPEN_MS,
    minute_bar,
    store_listing,
    store_option_day,
    vendor_listing_row,
)
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import tmp_lake as tmp_lake


@pytest.fixture
async def decoded_lake(fake_catalog, tmp_lake: Path) -> tuple[BackupManifest, dict[str, Path]]:
    root_id = str(active_root_id())
    raw = tmp_lake / "lake/raw"
    bars = [MinuteTradeBar(ny_datetime(FRIDAY_OPEN_MS), *([Decimal('500')] * 4), Decimal('1'))]
    sources = {
        "equity/usa/minute/spy/20250613_trade.zip": (build_minute_trade_zip_bytes("SPY", "20250613", bars), "minute", "trade"),
        "equity/usa/minute/spy/20250613_quote.zip": (build_minute_quote_zip_bytes("SPY", "20250613", bars), "minute", "quote"),
        "equity/usa/daily/spy.zip": (build_daily_zip_bytes("SPY", aggregate_minute_to_daily(bars)), "daily", "trade"),
    }
    for relative, (payload, _, _) in sources.items():
        path = raw / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    factor, mapping = seed_lake_corporate_actions(raw, "SPY")
    seed_lean_metadata_receipt(raw, data_root_id=active_root_id(), price_adjustment_mode="raw",
                               lean_image_digest="sha256:" + "a" * 64, include_interest_rate=True)
    with respx.mock:
        traded = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=500)
        empty = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=500)
        missing = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=501)
        await store_listing([vendor_listing_row(item) for item in (traded, empty, missing)])
        await store_option_day(traded, [minute_bar(0, 1, 1, 1, 1)])
        await store_option_day(empty, [])
        assert (await repack_option_session("SPY", FRIDAY)).failure is None
    observation = ChainObservation(underlying="SPY", session_ms=FRIDAY_OPEN_MS, observation_id="restore-test",
        started_ms=1749846600000, ended_ms=1749846600000, complete=True, reason=None, page_receipts=[], contracts=[], field_gaps={})
    snapshot = raw / str(snapshot_path(observation))
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(observation.model_dump_json())
    rows = {"lake/raw/" + row["file_path"]: row for row in fake_catalog.rows.values() if row["status"] == "complete"}
    files = []
    for index, path in enumerate(sorted((tmp_lake / "lake").rglob("*")), 1):
        if not path.is_file():
            continue
        relative = path.relative_to(tmp_lake).as_posix()
        artifact = relative.removeprefix("lake/raw/")
        fields = {}
        if relative in rows:
            row = rows[relative]
            fields = dict(artifact_kind=row["artifact_kind"], symbol=row["symbol"], market=row["market"],
                          session_ms=FRIDAY_OPEN_MS if row.get("trading_date") else None,
                          resolution=row["resolution"], data_type=row["data_type"])
        elif artifact in sources:
            fields = dict(artifact_kind="time_series_bars", symbol="SPY", market="usa", session_ms=FRIDAY_OPEN_MS,
                          resolution=sources[artifact][1], data_type=sources[artifact][2])
        elif path in (factor, mapping):
            fields = dict(artifact_kind="factor_file" if path == factor else "map_file", symbol="SPY", market="usa")
        elif artifact in ("market-hours/market-hours-database.json", "symbol-properties/symbol-properties-database.csv", "alternative/interest-rate/usa/interest-rate.csv"):
            fields = dict(artifact_kind="metadata")
        elif path == snapshot:
            fields = dict(artifact_kind="metadata", symbol="SPY", market="usa_options")
        payload = path.read_bytes()
        files.append(BackupFile(root_id=root_id, relative_path=relative, sha256=hashlib.sha256(payload).hexdigest(),
                                size_bytes=len(payload), artifact_ids=[index] if fields else [], **fields))
    manifest = BackupManifest(generation_id="a" * 32, started_ms=1000, completed_ms=2000, schema_hash="test",
        catalog_sha256="a" * 64, catalog_rows=len(files), root_ids=[root_id], files=files, root_markers={}, migration_ids=[], restore_files={})
    return manifest, {root_id: tmp_lake}


async def test_all_kinds_partial_empty_and_traded_decode_without_provider_access(decoded_lake: tuple) -> None:
    manifest, roots = decoded_lake
    with respx.mock(assert_all_called=False):
        counts = decode_restored_artifacts(manifest, roots)
    assert set(counts) == {"time_series_bars", "metadata", "factor_file", "map_file"}
    assert counts["time_series_bars"] == 6


@pytest.mark.parametrize("damage", ["map_date", "minute_timestamp"])
async def test_hash_admitted_corruption_still_fails_canonical_decoder(decoded_lake: tuple, damage: str) -> None:
    import io
    import zipfile
    manifest, roots = decoded_lake
    suffix = "equity/usa/map_files/spy.csv" if damage == "map_date" else "equity/usa/minute/spy/20250613_trade.zip"
    item = next(item for item in manifest.files if item.relative_path.endswith(suffix))
    path = roots[item.root_id] / item.relative_path
    if damage == "map_date":
        payload = b"not-a-date,spy\nnot-a-date,spy\n"
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("20250613_spy_minute_trade.csv", "90000000,5000000,5000000,5000000,5000000,1\n")
        payload = buffer.getvalue()
    path.write_bytes(payload)
    item.sha256, item.size_bytes = hashlib.sha256(payload).hexdigest(), len(payload)
    with pytest.raises(ValueError):
        decode_restored_artifacts(manifest, roots)


@pytest.mark.parametrize("kind", ["metadata", "session"])
async def test_reader_cannot_substitute_another_inventory_file_for_wrong_path(decoded_lake: tuple, kind: str) -> None:
    from app.option_archive.backup import BackupRefusedError
    manifest, roots = decoded_lake
    item = next(item for item in manifest.files if
        ("/session/v1/" in item.relative_path if kind == "session" else "/market-hours/" in item.relative_path))
    source = roots[item.root_id] / item.relative_path
    item.relative_path = item.relative_path.replace(source.name, "wrong-" + source.name)
    (roots[item.root_id] / item.relative_path).write_bytes(source.read_bytes())
    with pytest.raises(BackupRefusedError):
        decode_restored_artifacts(manifest, roots)
