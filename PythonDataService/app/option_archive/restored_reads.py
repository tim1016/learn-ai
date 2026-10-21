"""Decode restored artifacts with their normal readers, admitting bytes against the backed-up receipts."""
from __future__ import annotations

import hashlib
import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.data_lake.bar_validation import assert_publishable_stored_minute_bars
from app.data_lake.derived_daily import read_minute_trade_bars
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.ensure_data import check_stored_minute_day
from app.data_lake.factor_files import read_recorded_factor_file
from app.data_lake.map_files import map_file_coverage
from app.data_lake.metadata_bundle import read_receipt, verify_bundle
from app.data_lake.option_bars import parse_option_day_zip
from app.data_lake.option_lake import OPTION_MARKET
from app.data_lake.option_listing import read_option_listing
from app.data_lake.option_session import read_option_session
from app.data_lake.path_policy import (
    LeanDailyBarPath,
    LeanFactorFilePath,
    LeanMapFilePath,
    LeanMinuteBarPath,
    OptionListingPath,
    OptionMinuteBarPath,
    OptionSessionPath,
    lake_root_within,
)
from app.engine.data.lean_format import LeanDailyDataReader
from app.engine.live.identity import confine_path_to_root
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.option_archive.backup import BackupFile, BackupManifest, BackupRefusedError, require_work_window
from app.option_archive.snapshot import ChainObservation, snapshot_path
from app.options.contract import OptionContract
from app.utils.session_anchors import et_date_at_ms


@dataclass(frozen=True)
class _Restored:
    """One committed artifact as restored, and the admission its reader must use."""

    item: BackupFile
    path: PurePosixPath
    mode_root: Path
    payload: bytes
    admission: Callable[[Path], bytes]

    @property
    def symbol(self) -> str:
        if self.item.symbol is None:
            raise BackupRefusedError("restored receipt lacks its symbol")
        return self.item.symbol

    @property
    def day(self) -> date:
        if self.item.session_ms is None:
            raise BackupRefusedError("restored receipt lacks its session")
        return et_date_at_ms(self.item.session_ms)

    def expect(self, expected: PurePosixPath) -> None:
        if self.path != expected:
            raise BackupRefusedError("restored receipt identity differs from its committed path")


def _observation(restored: _Restored) -> None:
    observation = ChainObservation.model_validate_json(restored.payload)
    if (session_window_for_date(et_date_at_ms(observation.session_ms)).open_ms_utc != observation.session_ms
            or observation.underlying != restored.symbol):
        raise BackupRefusedError("snapshot identity differs from its committed path and underlying")
    restored.expect(snapshot_path(observation))


def _listing(restored: _Restored) -> None:
    as_of = datetime.strptime(restored.path.name.partition("_")[0], "%Y%m%d").date()
    restored.expect(OptionListingPath(restored.symbol, as_of).relative_path())
    if read_option_listing(restored.symbol, as_of, lake_root=restored.mode_root, admission=restored.admission) is None:
        raise BackupRefusedError("restored listing lacks its committed identity")


def _session(restored: _Restored) -> None:
    restored.expect(OptionSessionPath(restored.symbol, restored.day).relative_path())
    if read_option_session(restored.symbol, restored.day, lake_root=restored.mode_root, admission=restored.admission) is None:
        raise BackupRefusedError("restored session container lacks its committed identity")


def _option_bars(restored: _Restored) -> None:
    contract = OptionContract.from_occ_symbol(restored.symbol)
    restored.expect(OptionMinuteBarPath(contract.root, restored.symbol, restored.day).relative_path())
    parse_option_day_zip(restored.payload, restored.symbol, restored.day)


def _stock_minute(restored: _Restored) -> None:
    item, symbol, day = restored.item, restored.symbol, restored.day
    restored.expect(LeanMinuteBarPath(item.market, symbol, day, item.data_type).relative_path())
    if item.data_type == "trade":
        check_stored_minute_day(str(restored.path), restored.mode_root, symbol=symbol, trading_date=day,
                                admission=restored.admission)
    elif item.data_type == "quote":
        trade = LeanMinuteBarPath(item.market, symbol, day, "trade").relative_path()
        bars = read_minute_trade_bars(str(trade), restored.mode_root, admission=restored.admission)
        assert_publishable_stored_minute_bars(bars, symbol=symbol, trading_date=day)
        if restored.payload != build_minute_quote_zip_bytes(symbol, f"{day:%Y%m%d}", bars):
            raise BackupRefusedError("derived quote differs from the canonical same-day trade synthesis")
    else:
        raise BackupRefusedError("unsupported minute-bar data type")


def _stock_daily(restored: _Restored) -> None:
    if restored.item.data_type != "trade":
        raise BackupRefusedError("unsupported daily-bar data type")
    restored.expect(LeanDailyBarPath(restored.item.market, restored.symbol).relative_path())
    LeanDailyDataReader(restored.mode_root, admission=restored.admission).available_dates(restored.symbol)


def _factor_file(restored: _Restored) -> None:
    market = restored.item.market
    restored.expect(LeanFactorFilePath(market, restored.symbol).relative_path())
    read_recorded_factor_file(restored.mode_root, market=market, symbol=restored.symbol, admission=restored.admission)


def _map_file(restored: _Restored) -> None:
    restored.expect(LeanMapFilePath(restored.item.market, restored.symbol).relative_path())
    map_file_coverage(restored.payload)


def _lean_metadata(restored: _Restored) -> None:
    receipt = read_receipt(restored.mode_root)
    if receipt is None:
        raise BackupRefusedError("unknown metadata has no canonical bundle receipt")
    entries = [entry for entry in (receipt.files.market_hours, receipt.files.symbol_properties,
                                   receipt.files.interest_rate) if entry is not None]
    if str(restored.path) not in {entry.file_path for entry in entries}:
        raise BackupRefusedError("metadata receipt is not a member of its canonical bundle")
    for entry in entries:
        restored.admission(restored.mode_root / entry.file_path)
    verify_bundle(restored.mode_root, expected_root_id=UUID(restored.item.root_id),
                  expected_mode=receipt.price_adjustment_mode, expected_digest=receipt.lean_image_digest)


# (catalog kind, resolution, path layout) -> the normal reader that decodes it.
# Catalog identity alone cannot pick one: a session container and a contract's
# bars, or a listing and an evening observation, share kind and market.
_DECODERS: dict[tuple[str, str | None, str], Callable[[_Restored], None]] = {
    ("metadata", None, "observations"): _observation,
    ("metadata", None, "option/listing"): _listing,
    ("time_series_bars", "minute", "option/session"): _session,
    ("time_series_bars", "minute", "option/minute"): _option_bars,
    ("time_series_bars", "minute", "equity/minute"): _stock_minute,
    ("time_series_bars", "daily", "equity/daily"): _stock_daily,
    ("factor_file", None, "equity/factor_files"): _factor_file,
    ("map_file", None, "equity/map_files"): _map_file,
    ("metadata", None, "lean-metadata"): _lean_metadata,
}


def _layout(path: PurePosixPath) -> tuple[str, str | None]:
    """The layout an artifact's path names, and the market its directories imply."""
    parts = path.parts
    if len(parts) > 2 and parts[0] == "equity":
        return f"equity/{parts[2]}", parts[1]
    if len(parts) > 2 and parts[0] == "option":
        return f"option/{parts[2]}", OPTION_MARKET
    if len(parts) > 2 and parts[2] == "observations":
        return "observations", parts[0]
    return "lean-metadata", None


def decode_restored_lake(manifest: BackupManifest, roots: Mapping[str, Path], *,
                         should_pause: Callable[[], bool] = lambda: False) -> dict[str, int]:
    """Admit each restored artifact against its backed-up receipt and decode it with its normal reader."""
    receipts = {confine_path_to_root(roots[item.root_id] / item.relative_path, roots[item.root_id], label="restored file"): item
                for item in manifest.files}

    def admission(path: Path) -> bytes:
        item = receipts.get(Path(os.path.realpath(path)))
        if item is None or not item.artifact_ids:
            raise BackupRefusedError("restored file has no committed catalog receipt")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item.sha256 or len(payload) != item.size_bytes:
            raise BackupRefusedError("restored bytes differ from their committed receipt")
        return payload

    counts: Counter[str] = Counter()
    for path, item in receipts.items():
        require_work_window(should_pause)
        if item.artifact_kind is None:
            continue
        relative = PurePosixPath(item.relative_path)
        if len(relative.parts) < 3 or relative.parts[0] != "lake":
            raise BackupRefusedError("catalog artifact is outside its mode root")
        artifact = PurePosixPath(*relative.parts[2:])
        layout, market = _layout(artifact)
        decoder = _DECODERS.get((item.artifact_kind, item.resolution, layout))
        if decoder is None or (market is not None and item.market != market):
            raise BackupRefusedError("artifact kind has no canonical restore decoder")
        decoder(_Restored(item, artifact, lake_root_within(roots[item.root_id], relative.parts[1]), admission(path), admission))
        counts[item.artifact_kind] += 1
    return dict(counts)
