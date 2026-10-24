"""Lake-file helpers for research-study tests: find a session's file, and repair or corrupt it with a committed receipt."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from app.data_lake.adjustment_versions import adjustment_companion, companion_path
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath, lake_subpath
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.pinned_vendor import VendorBar

ET = ZoneInfo("America/New_York")


def lake_file(lake: Path, mode: str, day: date, symbol: str = "SPY") -> Path:
    """Where the lake keeps ``symbol``'s minute bars for ``day`` under ``mode``."""
    return lake / lake_subpath(mode) / LeanMinuteBarPath("usa", symbol, day, "trade").relative_path()  # type: ignore[arg-type]


def commit_payload(catalog: FakeCatalog, lake: Path, day: date, payload: bytes, *, mode: str = "raw") -> None:
    """Replace a held day's bytes with ``payload`` and commit their receipt.

    This is what a repair of the lake looks like to a reader: the file changed and the catalog vouches for the new
    bytes (an adjusted file's corporate-action record is rewritten against them, at the version it already named).
    With corrupt ``payload`` it is a publish that predates the lake's validation.
    """
    path = lake_file(lake, mode, day)
    path.write_bytes(payload)
    if mode == "polygon_split_adjusted":
        record = companion_path(path)
        version = json.loads(record.read_text())["corporate_action_version"]
        record.write_bytes(adjustment_companion(PurePosixPath(path.name), payload, version)[1])
    row = next(
        r
        for r in catalog.rows.values()
        if r["trading_date"] == day and r["resolution"] == "minute" and r["price_adjustment_mode"] == mode
    )
    row["file_sha256"] = hashlib.sha256(payload).hexdigest()
    row["file_size_bytes"] = len(payload)


def minute_bar(day: date, hour: int, minute: int, price: str = "100.00") -> MinuteTradeBar:
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)
    p = Decimal(price)
    return MinuteTradeBar(bar_start_et=start, open=p, high=p, low=p, close=p, volume=Decimal(100))


def commit_bars(catalog: FakeCatalog, lake: Path, day: date, bars: list[MinuteTradeBar], *, mode: str = "raw") -> None:
    commit_payload(catalog, lake, day, build_minute_trade_zip_bytes("SPY", day.strftime("%Y%m%d"), bars), mode=mode)


def corrected_day(day: date, vendor_day: list[VendorBar], *, bump: str = "0.05") -> list[MinuteTradeBar]:
    """The vendor's bars for ``day`` with every close nudged: a valid repair of the day that changes its numbers."""
    return [
        MinuteTradeBar(
            bar_start_et=datetime.fromtimestamp(b["t"] / 1000, ET),
            open=Decimal(repr(b["o"])),
            high=Decimal(repr(b["h"])) + Decimal(bump),
            low=Decimal(repr(b["l"])),
            close=Decimal(repr(b["c"])) + Decimal(bump),
            volume=Decimal(repr(b["v"])),
        )
        for b in vendor_day
    ]
