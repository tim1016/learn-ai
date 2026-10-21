"""Restore verification through the same parsers and gates normal readers use."""
from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from uuid import UUID

from app.data_lake.bar_validation import assert_publishable_stored_minute_bars
from app.data_lake.derived_daily import read_minute_trade_bars
from app.data_lake.derived_quote import build_minute_quote_zip_bytes
from app.data_lake.ensure_data import check_stored_minute_day
from app.data_lake.factor_files import read_recorded_factor_file
from app.data_lake.lean_writer import MinuteTradeBar
from app.data_lake.map_files import map_file_coverage
from app.data_lake.metadata_bundle import read_receipt, verify_bundle
from app.data_lake.option_bars import parse_option_day_zip
from app.data_lake.option_lake import zip_member_bytes
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
)
from app.engine.data.lean_format import LeanDailyDataReader
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.option_archive.backup import BackupManifest, BackupRefusedError, _require_work_window, _safe_relative
from app.option_archive.snapshot import ChainObservation, snapshot_path
from app.options.contract import OptionContract
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import ny_datetime


def decode_restored_lake(manifest: BackupManifest, roots: Mapping[str, Path], *,
                         should_pause: Callable[[], bool]) -> dict[str, int]:
    files = {str((roots[item.root_id] / _safe_relative(item.relative_path)).absolute()): item for item in manifest.files}
    def admission(path: Path) -> bytes:
        item = files.get(str(path.absolute()))
        if item is None or not item.artifact_ids:
            raise BackupRefusedError("restored file has no committed catalog receipt")
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item.sha256 or len(payload) != item.size_bytes:
            raise BackupRefusedError("restored bytes differ from their committed receipt")
        return payload

    counts: Counter[str] = Counter()
    for item in manifest.files:
        _require_work_window(should_pause)
        if item.artifact_kind is None:
            continue
        path = roots[item.root_id] / _safe_relative(item.relative_path)
        payload = admission(path)
        relative = Path(item.relative_path)
        if len(relative.parts) < 3 or relative.parts[0] != "lake":
            raise BackupRefusedError("catalog artifact is outside its mode root")
        mode_root = roots[item.root_id] / "lake" / relative.parts[1]
        artifact_path = relative.relative_to(Path("lake") / relative.parts[1]).as_posix()
        symbol = item.symbol
        day = et_date_at_ms(item.session_ms) if item.session_ms is not None else None
        if "/observations/" in item.relative_path:
            observation = ChainObservation.model_validate_json(payload)
            window = session_window_for_date(et_date_at_ms(observation.session_ms))
            if (window.open_ms_utc != observation.session_ms or str(snapshot_path(observation)) != artifact_path
                    or observation.underlying != symbol or item.market != "usa_options"):
                raise BackupRefusedError("snapshot identity differs from its committed path and underlying")
        elif "/session/v1/" in item.relative_path:
            if symbol is None or day is None or artifact_path != str(OptionSessionPath(symbol, day).relative_path()):
                raise BackupRefusedError("session-container receipt has the wrong identity")
            if symbol is None or day is None or read_option_session(symbol, day, lake_root=mode_root, admission=admission) is None:
                raise BackupRefusedError("restored session container lacks its committed identity")
        elif "/listing/" in item.relative_path:
            day_text = path.stem[:8]
            from datetime import date
            listing_day = date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:8]))
            if symbol is None or artifact_path != str(OptionListingPath(symbol, listing_day).relative_path()):
                raise BackupRefusedError("listing receipt has the wrong identity")
            if symbol is None or read_option_listing(symbol, listing_day, lake_root=mode_root, admission=admission) is None:
                raise BackupRefusedError("restored listing lacks its committed identity")
        elif item.artifact_kind == "factor_file":
            if symbol is None or artifact_path != str(LeanFactorFilePath(item.market or "usa", symbol).relative_path()):
                raise BackupRefusedError("factor receipt has the wrong identity")
            read_recorded_factor_file(mode_root, market=item.market or "usa", symbol=symbol, admission=admission)
        elif item.artifact_kind == "map_file":
            if symbol is None or artifact_path != str(LeanMapFilePath(item.market or "usa", symbol).relative_path()):
                raise BackupRefusedError("map receipt has the wrong identity")
            map_file_coverage(payload)
        elif item.market == "usa_options" and item.artifact_kind == "time_series_bars":
            if symbol is None or day is None:
                raise BackupRefusedError("option receipt lacks its expected identity")
            contract = OptionContract.from_occ_symbol(symbol)
            if artifact_path != str(OptionMinuteBarPath(contract.root, symbol, day).relative_path()):
                raise BackupRefusedError("option-bar receipt has the wrong identity")
            parse_option_day_zip(payload, symbol, day)
        elif item.artifact_kind == "time_series_bars":
            if symbol is None:
                raise BackupRefusedError("stock receipt lacks its expected identity")
            if item.resolution == "minute" and day is not None:
                expected = LeanMinuteBarPath(item.market or "usa", symbol, day, item.data_type or "trade")
                if artifact_path != str(expected.relative_path()):
                    raise BackupRefusedError("minute-bar receipt has the wrong identity")
                if item.data_type == "trade":
                    check_stored_minute_day(artifact_path, mode_root, symbol=symbol, trading_date=day, admission=admission)
                elif item.data_type == "quote":
                    trade_path = str(LeanMinuteBarPath(item.market or "usa", symbol, day, "trade").relative_path())
                    bars = read_minute_trade_bars(trade_path, mode_root, admission=admission)
                    assert_publishable_stored_minute_bars(bars, symbol=symbol, trading_date=day)
                    if payload != build_minute_quote_zip_bytes(symbol, f"{day:%Y%m%d}", bars):
                        raise BackupRefusedError("derived quote differs from the canonical same-day trade synthesis")
                else:
                    raise BackupRefusedError("unsupported minute-bar data type")
            elif item.resolution == "daily" and item.data_type == "trade":
                if artifact_path != str(LeanDailyBarPath(item.market or "usa", symbol).relative_path()):
                    raise BackupRefusedError("daily-bar receipt has the wrong identity")
                csv = zip_member_bytes(payload, symbol.lower() + ".csv", BackupRefusedError)
                bars = LeanDailyDataReader(mode_root, admission=admission).parse_history_zip(payload, symbol)
                if len(bars) != len([line for line in csv.splitlines() if line]):
                    raise BackupRefusedError("daily file contains a row the canonical reader cannot decode")
                prior = -1
                for bar in bars:
                    local = ny_datetime(bar.start_ms)
                    session_window_for_date(local.date())
                    if bar.start_ms <= prior:
                        raise BackupRefusedError("daily bars are not strictly increasing")
                    prior = bar.start_ms
                    assert_publishable_stored_minute_bars([MinuteTradeBar(local, bar.open, bar.high, bar.low, bar.close, bar.volume)],
                                                         symbol=symbol, trading_date=local.date())
            else:
                raise BackupRefusedError("unsupported stock artifact has no canonical restore decoder")
        elif item.artifact_kind == "metadata":
            receipt = read_receipt(mode_root)
            if receipt is None:
                raise BackupRefusedError("unknown metadata has no canonical bundle receipt")
            entries = (receipt.files.market_hours, receipt.files.symbol_properties, receipt.files.interest_rate)
            if artifact_path not in {entry.file_path for entry in entries if entry is not None}:
                raise BackupRefusedError("metadata receipt is not a member of its canonical bundle")
            for entry in entries:
                if entry is not None:
                    admission(mode_root / entry.file_path)
            verify_bundle(mode_root, expected_root_id=UUID(item.root_id), expected_mode=receipt.price_adjustment_mode,
                          expected_digest=receipt.lean_image_digest)
        else:
            raise BackupRefusedError("artifact kind has no canonical restore decoder")
        counts[item.artifact_kind] += 1
    return dict(counts)
