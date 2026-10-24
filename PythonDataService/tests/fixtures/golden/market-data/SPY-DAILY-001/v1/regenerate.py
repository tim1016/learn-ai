"""Record, and measure against, the vendor's real daily bars for ``SPY-DAILY-001`` (#3524).

Fixture paperwork: run by hand, never by a test or by CI. Two steps.

``fetch`` records the vendor's daily aggregates for SPY, 2025-01-13 to 2025-01-17, ``adjusted=false``, byte for byte into
``input.json``. The recording in this fixture was made on 2026-10-24 by a one-off script that sent this same request
(the same path and query, the key in the same header). This tidied version of the step has not been run in that form.
Running it records a NEW fixture: the vendor's ``request_id`` differs on every call and its data for a past day can be
revised, so the expectations in ``tests/research/test_cross_sectional_lake_vs_provider.py`` must be re-measured and
re-read before it is registered. The key is read from the environment, sent in a header, never printed or written, and is
in no URL; no error this script raises holds it.

    POLYGON_API_KEY=<key> python regenerate.py fetch <an empty folder to write input.json into>

``measure`` writes ``output.json`` from ``input.json`` and the repository's SPY one-minute capture
(``tests/fixtures/polygon_capture/spy_minute_2025-01-13_2025-01-17/bars.json``). It calls no network and imports no
application code, so the fold it measures is written out here independently of the study's:

    python regenerate.py measure <input.json> <bars.json> <output.json>

The fold: the minutes stamped from 09:30:00 up to but not including 16:00:00 New York time (a minute is stamped with its
start). Open is the first minute's open, high and low the extremes, close the 15:59 minute's close, volume the exact sum.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
BASE_URL = "https://api.polygon.io"
DAILY_PATH = "/v2/aggs/ticker/SPY/range/1/day/2025-01-13/2025-01-17"
DAILY_QUERY = {"adjusted": "false", "sort": "asc", "limit": "50000"}
REGULAR_OPEN, REGULAR_CLOSE = time(9, 30), time(16, 0)
# What a key may hold to ride in a header: printable ASCII and no space. Anything else is refused before a request,
# because an HTTP library's error text for a bad header value quotes the value.
SENDABLE_KEY = re.compile(r"[\x21-\x7e]+")


def fetch(folder: Path) -> None:
    import httpx

    key = os.environ["POLYGON_API_KEY"].strip()
    if SENDABLE_KEY.fullmatch(key) is None:
        raise ValueError("POLYGON_API_KEY is empty, or holds a space or a character that is not printable ASCII")
    try:
        response = httpx.get(f"{BASE_URL}{DAILY_PATH}", params=DAILY_QUERY, headers={"Authorization": f"Bearer {key}"}, timeout=30.0)
    except httpx.HTTPError as error:
        raise RuntimeError(f"the request for {DAILY_PATH} failed ({type(error).__name__}); nothing was saved") from None
    if response.status_code != 200:
        raise RuntimeError(f"the request for {DAILY_PATH} answered HTTP {response.status_code}; nothing was saved")
    if key.encode() in response.content:
        raise RuntimeError(f"the answer to {DAILY_PATH} contains the key; nothing was saved")
    (folder / "input.json").write_bytes(response.content)


def regular_fold(minutes: list[dict]) -> dict:
    regular = [m for m in minutes if REGULAR_OPEN <= datetime.fromtimestamp(m["timestamp"] / 1000, NEW_YORK).time() < REGULAR_CLOSE]
    return {
        "open": regular[0]["open"],
        "high": max(m["high"] for m in regular),
        "low": min(m["low"] for m in regular),
        "close": regular[-1]["close"],
        "volume": math.fsum(m["volume"] for m in regular),
    }


def difference(a: float, b: float) -> str:
    """``a - b`` in exact decimal arithmetic on the prices as written, e.g. ``"0.15"``."""
    return str(Decimal(repr(a)) - Decimal(repr(b)))


def measure(vendor_path: Path, capture_path: Path, output_path: Path) -> None:
    vendor = json.loads(vendor_path.read_text(encoding="utf-8"))
    minutes_by_day: dict[date, list[dict]] = {}
    for minute in json.loads(capture_path.read_text(encoding="utf-8")):
        minutes_by_day.setdefault(datetime.fromtimestamp(minute["timestamp"] / 1000, NEW_YORK).date(), []).append(minute)

    sessions: dict[str, dict] = {}
    for daily in vendor["results"]:
        day = datetime.fromtimestamp(daily["t"] / 1000, NEW_YORK).date()
        minutes = minutes_by_day[day]
        fold = regular_fold(minutes)
        closing_minute = next(m for m in minutes if datetime.fromtimestamp(m["timestamp"] / 1000, NEW_YORK).time() == REGULAR_CLOSE)
        sessions[day.isoformat()] = {
            "vendor": {"open": daily["o"], "high": daily["h"], "low": daily["l"], "close": daily["c"], "volume": daily["v"]},
            "regular_fold": fold,
            "close_difference_fold_minus_vendor": difference(fold["close"], daily["c"]),
            "volume_share_fold_over_vendor": fold["volume"] / daily["v"],
            "with_the_minute_stamped_16_00": {
                "close": closing_minute["close"],
                "close_difference_fold_minus_vendor": difference(closing_minute["close"], daily["c"]),
            },
        }
    output_path.write_text(json.dumps({"fixture": "SPY-DAILY-001", "sessions": sessions}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    step, *paths = sys.argv[1:]
    if step == "fetch" and len(paths) == 1:
        fetch(Path(paths[0]))
    elif step == "measure" and len(paths) == 3:
        measure(*(Path(p) for p in paths))
    else:
        raise SystemExit(__doc__)
