"""Every retained artifact kind is checked by its canonical production reader."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import respx

from app.data_lake.derived_daily import aggregate_minute_to_daily, build_daily_zip_bytes
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_session_repack import repack_option_session
from app.data_lake.root_identity import active_root_id
from app.option_archive.restored_reads import RestoredArtifact, decode_restored_lake
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
async def decoded_lake(fake_catalog, tmp_lake: Path) -> tuple[list[RestoredArtifact], dict[str, Path]]:
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
    rows = {row["file_path"]: row for row in fake_catalog.rows.values() if row["status"] == "complete"}
    artifacts = []
    for path in sorted((tmp_lake / "lake/raw").rglob("*")):
        if not path.is_file():
            continue
        artifact = path.relative_to(tmp_lake / "lake/raw").as_posix()
        fields = {}
        if artifact in rows:
            row = rows[artifact]
            fields = dict(artifact_kind=row["artifact_kind"], symbol=row["symbol"], market=row["market"],
                          trading_date=row["trading_date"], resolution=row["resolution"], data_type=row["data_type"])
        elif artifact in sources:
            minute = sources[artifact][1] == "minute"
            fields = dict(artifact_kind="time_series_bars", symbol="SPY", market="usa", trading_date=FRIDAY if minute else None,
                          resolution=sources[artifact][1], data_type=sources[artifact][2])
        elif path in (factor, mapping):
            fields = dict(artifact_kind="factor_file" if path == factor else "map_file", symbol="SPY", market="usa")
        elif artifact in ("market-hours/market-hours-database.json", "symbol-properties/symbol-properties-database.csv", "alternative/interest-rate/usa/interest-rate.csv"):
            fields = dict(artifact_kind="metadata")
        elif path == snapshot:
            fields = dict(artifact_kind="metadata", symbol="SPY", market="usa_options")
        if fields:
            payload = path.read_bytes()
            artifacts.append(RestoredArtifact(id=len(artifacts) + 1, root_id=root_id, mode="raw", file_path=artifact,
                sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload),
                **{"market": None, "symbol": None, "trading_date": None, "resolution": None, "data_type": None, **fields}))
    return artifacts, {root_id: tmp_lake}


async def test_all_kinds_partial_empty_and_traded_decode_without_provider_access(decoded_lake: tuple) -> None:
    artifacts, roots = decoded_lake
    with respx.mock(assert_all_called=False):
        counts = decode_restored_lake(artifacts, roots)
    assert set(counts) == {"time_series_bars", "metadata", "factor_file", "map_file"}
    assert counts["time_series_bars"] == 6


@pytest.mark.parametrize("damage", ["map_date", "minute_timestamp"])
async def test_hash_admitted_corruption_still_fails_canonical_decoder(decoded_lake: tuple, damage: str) -> None:
    import io
    import zipfile
    artifacts, roots = decoded_lake
    suffix = "equity/usa/map_files/spy.csv" if damage == "map_date" else "equity/usa/minute/spy/20250613_trade.zip"
    index = next(index for index, item in enumerate(artifacts) if item.file_path.endswith(suffix))
    item = artifacts[index]
    path = roots[item.root_id] / "lake/raw" / item.file_path
    if damage == "map_date":
        payload = b"not-a-date,spy\nnot-a-date,spy\n"
    else:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("20250613_spy_minute_trade.csv", "90000000,5000000,5000000,5000000,5000000,1\n")
        payload = buffer.getvalue()
    path.write_bytes(payload)
    artifacts[index] = replace(item, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
    with pytest.raises(ValueError):
        decode_restored_lake(artifacts, roots)


@pytest.mark.parametrize("kind", ["metadata", "session"])
async def test_reader_cannot_substitute_another_inventory_file_for_wrong_path(decoded_lake: tuple, kind: str) -> None:
    from app.option_archive.backup import BackupRefusedError
    artifacts, roots = decoded_lake
    index = next(index for index, item in enumerate(artifacts) if
        ("/session/v1/" in item.file_path if kind == "session" else item.file_path.startswith("market-hours/")))
    item = artifacts[index]
    source = roots[item.root_id] / "lake/raw" / item.file_path
    artifacts[index] = replace(item, file_path=item.file_path.replace(source.name, "wrong-" + source.name))
    (roots[item.root_id] / "lake/raw" / artifacts[index].file_path).write_bytes(source.read_bytes())
    with pytest.raises(BackupRefusedError):
        decode_restored_lake(artifacts, roots)
