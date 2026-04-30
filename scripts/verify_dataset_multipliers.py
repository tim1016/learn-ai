"""
Programmatic check: fire dataset/generate-zip with several
(timespan, multiplier) combos against a stable RTH window and report
the dataset.csv row count vs the expected count.

The window is one full trading week (Mon–Fri, no holidays) so the math
is easy: rows ≈ trading_days * (RTH_minutes / multiplier_in_minutes).
"""

from __future__ import annotations

import csv
import io
import sys
import time
import zipfile
from typing import Any

import httpx

ENDPOINT = "http://localhost:8000/api/dataset/generate-zip"
TICKER = "SPY"
FROM_DATE = "2025-02-03"  # Monday
TO_DATE = "2025-02-07"  # Friday — full RTH trading week, no US holidays
TRADING_DAYS = 5
RTH_MINUTES = 390  # 09:30–16:00 ET


def expected_rows(timespan: str, multiplier: int) -> int:
    """Bars-per-day for the listed timespan in RTH, scaled across the
    trading week. Polygon hour bars in RTH come back as 7 (top-of-hour
    bars covering 09:00 through 15:00, the 09:00 one captures 09:30 RTH
    open since RTH starts mid-hour)."""
    if timespan == "minute":
        return TRADING_DAYS * (RTH_MINUTES // multiplier)
    if timespan == "hour":
        # Polygon RTH hours: 09:00, 10:00, 11:00, 12:00, 13:00, 14:00, 15:00 → 7 per day at multiplier=1.
        return TRADING_DAYS * (7 // multiplier)
    if timespan == "day":
        return TRADING_DAYS // multiplier
    raise ValueError(f"unknown timespan {timespan}")


CASES: list[tuple[str, int, str]] = [
    ("minute", 1, "1m"),
    ("minute", 5, "5m"),
    ("minute", 15, "15m"),
    ("minute", 30, "30m"),
    ("hour", 1, "1h"),
    ("day", 1, "1D"),
]


def fire(timespan: str, multiplier: int) -> tuple[int, int, str]:
    """POST one request, return (returned_rows, raw_polygon_bars, sample_first_iso)."""
    payload: dict[str, Any] = {
        "ticker": TICKER,
        "from_date": FROM_DATE,
        "to_date": TO_DATE,
        "indicator_entries": [],
        "session": "rth",
        "forward_fill": False,  # don't synthesize, just count what Polygon returned
        "warmup": False,
        "timespan": timespan,
        "multiplier": multiplier,
        "adjusted": True,
        "adjust_for_dividends": False,
        "sort": "asc",
        "limit": 50000,
        "options_companion": None,
        "include_quality_report": False,
        "include_previous_close": False,
        "include_splits": False,
        "include_dividends": False,
        "include_ticker_overview": False,
        "include_news": False,
        "include_financials": False,
        "include_trades": False,
        "include_quotes": False,
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(ENDPOINT, json=payload)
        resp.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    with zf.open("dataset.csv") as f:
        reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
        header = next(reader)
        rows = list(reader)
    raw = 0
    if "metadata.csv" in zf.namelist():
        with zf.open("metadata.csv") as f:
            for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
                if row and row[0] == "raw_bars_from_polygon":
                    raw = int(row[1])
                    break
    sample = rows[0][1] if rows else "(empty)"
    return len(rows), raw, sample


def main() -> int:
    print(f"Window: {TICKER} {FROM_DATE} → {TO_DATE} (RTH, 5 trading days)\n")
    print(f"{'timeframe':<12}{'rows':>8}{'expected':>12}{'diff':>8}{'raw_polygon':>14}  first_bar")
    print("-" * 88)
    failures = 0
    for timespan, multiplier, label in CASES:
        try:
            rows, raw, sample = fire(timespan, multiplier)
        except Exception as e:
            print(f"{label:<12}ERROR: {e}")
            failures += 1
            continue
        exp = expected_rows(timespan, multiplier)
        diff = rows - exp
        flag = "" if diff == 0 else "  <-- mismatch"
        print(f"{label:<12}{rows:>8}{exp:>12}{diff:>+8}{raw:>14}  {sample}{flag}")
        if diff != 0:
            failures += 1
        time.sleep(0.5)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
