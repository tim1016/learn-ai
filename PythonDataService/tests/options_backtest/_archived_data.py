"""Synthetic run scenes stored with the canonical original ZIP writers."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.option_listing import build_option_listing_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath, OptionListingPath, OptionMinuteBarPath
from app.data_lake.polygon_option_contracts import PolygonOptionContract
from app.options_backtest.market import MinuteBar
from app.research.golden_search.proof import BlobStore
from app.research.options_golden_search.snapshots import Snapshot, SnapshotRunData
from app.utils.timestamps import ny_datetime
from tests.options_backtest._run_data import FakeRunData


def archived_scene(scene: FakeRunData, root: Path) -> SnapshotRunData:
    blobs = BlobStore(root)
    manifest: dict[str, str] = {}
    absent: set[str] = set()

    def bars_zip(symbol: str, day: date, bars: list[MinuteBar] | tuple[MinuteBar, ...]) -> bytes:
        return build_minute_trade_zip_bytes(symbol, f"{day:%Y%m%d}", [
            MinuteTradeBar(ny_datetime(bar.close_ms - 60_000), bar.open, bar.high, bar.low, bar.close, Decimal(1))
            for bar in bars])

    for day in sorted(set(scene._underlying) | set(scene._listings)):
        relative = str(LeanMinuteBarPath("usa", "SPY", day, "trade").relative_path())
        if day in scene._underlying:
            manifest[relative] = blobs.put(bars_zip("SPY", day, tuple(scene._underlying[day])))
        else:
            absent.add(relative)
        relative = str(OptionListingPath("SPY", day).relative_path())
        if day not in scene._listings:
            absent.add(relative)
            continue
        listing = scene._listings[day]
        assert listing.complete
        rows = [PolygonOptionContract(f"O:{row.contract.occ_symbol}", row.contract.right, row.contract.strike,
                    row.contract.expiry_date.isoformat(), "american", Decimal(row.multiplier), not row.standard)
                for row in listing.contracts]
        manifest[relative] = blobs.put(build_option_listing_zip_bytes("SPY", day, rows))
        for row in listing.contracts:
            contract = row.contract
            relative = str(OptionMinuteBarPath(contract.root, contract.occ_symbol, day).relative_path())
            if (contract, day) in scene._options:
                manifest[relative] = blobs.put(bars_zip(contract.occ_symbol, day, tuple(scene._options[(contract, day)])))
            else:
                absent.add(relative)
    return SnapshotRunData("SPY", Snapshot(manifest=manifest, absent=tuple(sorted(absent)),
        retained_bytes=sum(len(blobs.get(digest)) for digest in set(manifest.values()))), blobs)
