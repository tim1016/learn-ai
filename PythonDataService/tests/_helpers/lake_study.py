"""Helpers the lake-sourced research studies' tests share.

* **Lake files.** Find a session's file, and repair or corrupt it with a committed receipt.
* **The two paths.** What the provider path would have handed a study for a pinned vendor tape
  (``vendor_rows``, ``provider_rows``), the strict-float comparison of two reports, and the key a
  study caches under.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from polygon.rest.models import Agg

from app.data_lake.adjustment_versions import adjustment_companion, companion_path
from app.data_lake.capture_finality import VENDOR_DELAY_MS
from app.data_lake.lean_writer import MinuteTradeBar, build_minute_trade_zip_bytes
from app.data_lake.path_policy import LeanMinuteBarPath, lake_subpath
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.research.lake_study import LakeStudy, cache_key
from app.services.polygon_client import PolygonClientService
from tests._helpers.fake_lake_catalog import FakeCatalog
from tests._helpers.pinned_vendor import VendorBar

ET = ZoneInfo("America/New_York")

#: The repo's strict-float default for indicators and closed-form calculations.
ATOL = 1e-9
RTOL = 0.0

#: Dense enough that the 15-minute target clears the studies' 70% validity gate; the gate is not under test where
#: these are used.
DENSE = {"regular_keep": 0.995, "extended_keep": 0.92}


def lake_file(lake: Path, mode: str, day: date, symbol: str = "SPY") -> Path:
    """Where the lake keeps ``symbol``'s minute bars for ``day`` under ``mode``."""
    return lake / lake_subpath(mode) / LeanMinuteBarPath("usa", symbol, day, "trade").relative_path()  # type: ignore[arg-type]


def row_of(catalog: FakeCatalog, day: date, mode: str = "raw") -> dict:
    """The catalog's minute row for ``day`` under ``mode``."""
    return next(
        r
        for r in catalog.rows.values()
        if r["trading_date"] == day and r["resolution"] == "minute" and r["price_adjustment_mode"] == mode
    )


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


def leave_early_capture(
    catalog: FakeCatalog, lake: Path, tape: dict[date, list[VendorBar]], day: date, *, completed_at_ms: int | None, mode: str = "raw"
) -> None:
    """Leave ``day`` as a regular-hours chart opened right after the close leaves it.

    The artifact holds the vendor's bars up to the quarter hour it had delivered by then, and the catalog says when
    the capture completed. The next fetch of the day would return the whole tape.
    """
    delivered_until = session_close_ms_utc(day) + VENDOR_DELAY_MS
    commit_bars(catalog, lake, day, corrected_day(day, [b for b in tape[day] if b["t"] < delivered_until], bump="0"), mode=mode)
    row_of(catalog, day, mode)["completed_at_ms"] = completed_at_ms


def leave_publication_in_flight(catalog: FakeCatalog, lake: Path, day: date, *, lease_ms_from_now: int, mode: str = "raw") -> bytes:
    """Leave ``day`` as a writer caught between the rename and the commit leaves it.

    The new bytes are renamed into place, the catalog row is still ``fetching`` and no receipt names those bytes.
    ``lease_ms_from_now`` positive: the writer is alive and holds its lease. Negative: it died, and its lease has
    expired. Returns the stray bytes.
    """
    row = next(
        r
        for r in catalog.rows.values()
        if r["trading_date"] == day and r["resolution"] == "minute" and r["price_adjustment_mode"] == mode
    )
    row["status"] = "fetching"
    row["lease_expires_at_ms"] = int(time.time() * 1000) + lease_ms_from_now
    stray = build_minute_trade_zip_bytes("SPY", day.strftime("%Y%m%d"), [minute_bar(day, 10, 0)])
    lake_file(lake, mode, day).write_bytes(stray)
    return stray


# ---------------------------------------------------------------------------
# The provider path, on the same pinned tape
# ---------------------------------------------------------------------------


def vendor_fold(minutes: list[VendorBar], bar_minutes: int) -> list[VendorBar]:
    """An independent stand-in for the vendor's own N-minute aggregate: first/high/low/last/sum per UTC-epoch bucket.

    It floors UTC milliseconds, which is the exchange wall clock only for a bar length that divides the hour; the
    studies refuse every other length, and ``test_signal_study.py`` shows what the two grids do for one that does not.
    """
    bucket_ms = bar_minutes * 60_000
    out: dict[int, VendorBar] = {}
    for m in minutes:
        key = m["t"] // bucket_ms * bucket_ms
        if key not in out:
            out[key] = {**m, "t": key}
        else:
            bar = out[key]
            bar["h"], bar["l"], bar["c"], bar["v"] = max(bar["h"], m["h"]), min(bar["l"], m["l"]), m["c"], bar["v"] + m["v"]
    return [out[k] for k in sorted(out)]


def vendor_rows(tape: Mapping[date, list[VendorBar]], *, bar_minutes: int = 1) -> list[dict]:
    """What the provider path would hand the runner for the same tape: the vendor's own floats."""
    minutes = [b for day in sorted(tape) for b in tape[day]]
    return [
        {"timestamp": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"], "volume": float(b["v"])}
        for b in (minutes if bar_minutes == 1 else vendor_fold(minutes, bar_minutes))
    ]


class _SdkTape:
    """The SDK's ``RESTClient.list_aggs`` over a pinned tape: it pages ``Agg`` objects exactly as the SDK would."""

    def __init__(self, raw: Mapping[date, list[VendorBar]], adjusted: Mapping[date, list[VendorBar]] | None) -> None:
        self.raw, self.adjusted = raw, adjusted

    def list_aggs(self, *, ticker, multiplier, timespan, from_, to, limit, adjusted, sort):
        tape = self.adjusted if (adjusted and self.adjusted is not None) else self.raw
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        minutes = [b for day, bars in sorted(tape.items()) if start <= day <= end for b in bars]
        for bar in minutes if multiplier == 1 else vendor_fold(minutes, multiplier):
            yield Agg.from_dict(bar)


def provider_rows(
    raw: Mapping[date, list[VendorBar]],
    window: list[date],
    *,
    bar_minutes: int = 1,
    adjusted: Mapping[date, list[VendorBar]] | None = None,
) -> list[dict]:
    """What a study read before it read the lake: ``fetch_aggregates`` for the window, adjusted, every session of the day."""
    service = PolygonClientService()
    service.client = _SdkTape(raw, adjusted)  # type: ignore[assignment]
    return service.fetch_aggregates(
        ticker="SPY", multiplier=bar_minutes, timespan="minute", from_date=window[0].isoformat(), to_date=window[-1].isoformat()
    )


# ---------------------------------------------------------------------------
# Comparing and keying
# ---------------------------------------------------------------------------


def assert_same(a: Any, b: Any, where: str = "report") -> None:
    """Deep equality, floats at the strict-float tolerance (NaN equals NaN: a warmup region is part of the answer)."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), where
        for key in a:
            assert_same(a[key], b[key], f"{where}.{key}")
    elif isinstance(a, list | tuple):
        assert isinstance(b, list | tuple) and len(a) == len(b), f"{where}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            assert_same(x, y, f"{where}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        assert np.isclose(a, b, atol=ATOL, rtol=RTOL, equal_nan=True), f"{where}: {a!r} != {b!r}"
    else:
        assert a == b, f"{where}: {a!r} != {b!r}"


def cache_key_of(study: LakeStudy, identity: Any, admitted: Any) -> dict[str, Any]:
    """The key the study's result is cached under, as the shared run builds it."""
    return cache_key(identity, admitted, calculation_version=study.calculation_version, extra=study.cache_extra)
