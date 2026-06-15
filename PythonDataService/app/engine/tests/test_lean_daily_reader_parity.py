"""Bit-exact parity: LeanDailyDataReader vs raw LEAN daily zip.

Contract: reading ``equity/usa/daily/aapl.zip`` through
:class:`LeanDailyDataReader` must produce ``TradeBar`` objects whose OHLCV
values round-trip to exactly the integer deci-cent values stored on disk —
no float drift, no off-by-one on the deci-cent scale, no timezone-induced
date shifts.

We verify this three ways:

1. **Row count** — the reader must surface exactly one bar per CSV row in
   the target range.
2. **Deci-cent round-trip** — for every parsed bar we multiply back by
   ``PRICE_SCALE`` and compare to the raw integers from the CSV. Any
   mismatch means our Decimal math is wrong somewhere.
3. **DST boundary** — we deliberately include a range that crosses the
   2021-03-14 DST transition so a naive ``datetime`` build would fail
   against ``ZoneInfo``'s expected UTC offsets (``-05:00`` before,
   ``-04:00`` after).

Run with::

    cd PythonDataService
    python -m app.engine.tests.test_lean_daily_reader_parity
"""

from __future__ import annotations

import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.engine.data.lean_format import (
    PRICE_SCALE,
    LeanDailyDataReader,
)

LEAN_DATA_ROOT = Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")
SYMBOL = "aapl"


def _read_raw_rows(symbol: str) -> list[tuple[date, int, int, int, int, int]]:
    """Decode ``aapl.zip`` the dumb way: split CSV, int() the fields.

    Returns a list of ``(date, open, high, low, close, volume)`` tuples where
    the prices are the raw deci-cent integers exactly as stored on disk.
    This is the reference we compare the reader against.
    """
    zip_path = LEAN_DATA_ROOT / "equity" / "usa" / "daily" / f"{symbol}.zip"
    if not zip_path.exists():
        raise FileNotFoundError(
            f"Expected LEAN reference daily zip at {zip_path}. The parity test needs access to the Lean/Data mount."
        )
    rows: list[tuple[date, int, int, int, int, int]] = []
    with zipfile.ZipFile(zip_path) as zf, zf.open(f"{symbol}.csv") as f:
        for line in f.read().decode("ascii").splitlines():
            if not line:
                continue
            parts = line.split(",")
            if len(parts) != 6:
                continue
            ts, o, h, l, c, v = parts  # noqa: E741 — `l` is canonical OHLCV "low"
            date_str = ts.split(" ", 1)[0]
            d = date(int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]))
            rows.append((d, int(o), int(h), int(l), int(c), int(v)))
    return rows


def run_parity_test() -> None:
    raw_rows = _read_raw_rows(SYMBOL)
    print(f"raw CSV rows: {len(raw_rows)}")
    print(f"first raw row: {raw_rows[0]}")
    print(f"last raw row:  {raw_rows[-1]}")

    reader = LeanDailyDataReader(LEAN_DATA_ROOT)

    # -------------------------------------------------------------------- #
    # 1. Full-history parse — every row must round-trip exactly.
    # -------------------------------------------------------------------- #
    history = list(reader.iter_bars(SYMBOL, raw_rows[0][0], raw_rows[-1][0]))
    if len(history) != len(raw_rows):
        print(f"FAIL: row count mismatch — reader returned {len(history)} bars but CSV has {len(raw_rows)} rows")
        sys.exit(1)

    mismatches: list[str] = []
    for bar, raw in zip(history, raw_rows, strict=False):
        raw_date, raw_o, raw_h, raw_l, raw_c, raw_v = raw
        if bar.time.date() != raw_date:
            mismatches.append(f"date: reader={bar.time.date()} raw={raw_date}")
            continue
        # Round-trip through the scale factor. Because we parse via
        # ``Decimal(o) / PRICE_SCALE`` the reverse multiplication is exact.
        if int(bar.open * PRICE_SCALE) != raw_o:
            mismatches.append(f"{raw_date} open: {bar.open} → {int(bar.open * PRICE_SCALE)} vs {raw_o}")
        if int(bar.high * PRICE_SCALE) != raw_h:
            mismatches.append(f"{raw_date} high: {bar.high} → {int(bar.high * PRICE_SCALE)} vs {raw_h}")
        if int(bar.low * PRICE_SCALE) != raw_l:
            mismatches.append(f"{raw_date} low: {bar.low} → {int(bar.low * PRICE_SCALE)} vs {raw_l}")
        if int(bar.close * PRICE_SCALE) != raw_c:
            mismatches.append(f"{raw_date} close: {bar.close} → {int(bar.close * PRICE_SCALE)} vs {raw_c}")
        if bar.volume != raw_v:
            mismatches.append(f"{raw_date} volume: {bar.volume} vs {raw_v}")
        if len(mismatches) > 10:
            break

    if mismatches:
        print(f"FAIL: {len(mismatches)} row(s) mismatch:")
        for m in mismatches[:10]:
            print(f"  - {m}")
        sys.exit(1)

    # -------------------------------------------------------------------- #
    # 2. Partial range — must honor start/end inclusive boundaries.
    # -------------------------------------------------------------------- #
    narrow = list(reader.iter_bars(SYMBOL, date(2021, 3, 29), date(2021, 3, 31)))
    if len(narrow) != 3:
        print(f"FAIL: expected 3 bars in 2021-03-29..2021-03-31, got {len(narrow)}")
        sys.exit(1)
    if narrow[0].time.date() != date(2021, 3, 29):
        print(f"FAIL: narrow range start {narrow[0].time.date()} != 2021-03-29")
        sys.exit(1)
    if narrow[-1].time.date() != date(2021, 3, 31):
        print(f"FAIL: narrow range end {narrow[-1].time.date()} != 2021-03-31")
        sys.exit(1)

    # -------------------------------------------------------------------- #
    # 3. DST boundary — 2021-03-12 is EST (-05:00), 2021-03-15 is EDT (-04:00).
    #    ZoneInfo must resolve both correctly.
    # -------------------------------------------------------------------- #
    dst_window = list(reader.iter_bars(SYMBOL, date(2021, 3, 12), date(2021, 3, 15)))
    if len(dst_window) != 2:  # 03-12 Fri, 03-15 Mon (13/14 are weekend)
        print(f"FAIL: expected 2 bars across DST boundary (Fri+Mon), got {len(dst_window)}")
        sys.exit(1)
    pre_offset = dst_window[0].time.utcoffset()
    post_offset = dst_window[-1].time.utcoffset()
    if pre_offset is None or post_offset is None:
        print("FAIL: DST boundary bars have naive datetimes")
        sys.exit(1)
    if pre_offset.total_seconds() != -5 * 3600:
        print(f"FAIL: pre-DST offset {pre_offset} != -05:00")
        sys.exit(1)
    if post_offset.total_seconds() != -4 * 3600:
        print(f"FAIL: post-DST offset {post_offset} != -04:00")
        sys.exit(1)

    # -------------------------------------------------------------------- #
    # 4. End-time should mark the following midnight (period = 1 day).
    # -------------------------------------------------------------------- #
    first = history[0]
    period = first.end_time - first.time
    if period.total_seconds() != 86400:
        print(f"FAIL: daily bar period is {period.total_seconds()}s, expected 86400")
        sys.exit(1)

    # -------------------------------------------------------------------- #
    # 5. Spot check against known values we hand-verified earlier.
    # -------------------------------------------------------------------- #
    if history[0].open != Decimal("13.63"):
        print(f"FAIL: first open {history[0].open} != Decimal('13.63')")
        sys.exit(1)
    if history[-1].close != Decimal("122.15"):
        print(f"FAIL: last close {history[-1].close} != Decimal('122.15')")
        sys.exit(1)
    if history[-1].volume != 109019052:
        print(f"FAIL: last volume {history[-1].volume} != 109019052")
        sys.exit(1)

    print(
        f"PASS: LeanDailyDataReader round-trips {len(history)} AAPL daily "
        f"bars bit-exactly (1998-01-02 through 2021-03-31), DST handled, "
        f"partial ranges honored."
    )


if __name__ == "__main__":
    run_parity_test()
