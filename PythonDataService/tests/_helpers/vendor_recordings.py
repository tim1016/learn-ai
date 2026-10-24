"""The vendor's real answers kept under ``tests/fixtures/golden/lake-studies``, as a test reads them (#3522).

Each recording is a body Polygon sent, saved byte for byte (``attribution.md`` beside it says how and when). A test serves it
at the vendor's HTTP seam (``pinned_vendor.mock_vendor``) or compares against it, so nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests._helpers.pinned_vendor import VendorBar

ET = ZoneInfo("America/New_York")

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SPY_15_MINUTE = _FIXTURES / "golden" / "lake-studies" / "lake-studies-spy-15min-fold-2025-01-13" / "v1"
WMT_ADJUSTED = _FIXTURES / "golden" / "lake-studies" / "lake-studies-wmt-split-adjusted-grid-2024-02-20" / "v1"
#: The repo's own SPY minute capture the 15-minute recording is held to.
SPY_MINUTE_CAPTURE = _FIXTURES / "polygon_capture" / "spy_minute_2025-01-13_2025-01-17"


def recorded_bars(fixture: Path) -> list[VendorBar]:
    """The ``results`` of the one body ``fixture`` records, as the vendor sent them."""
    recording = json.loads((fixture / "recording.json").read_text(encoding="utf-8"))
    (request,) = recording["requests"]
    return json.loads((fixture / request["file"]).read_text(encoding="utf-8"))["results"]


def by_session(bars: list[VendorBar]) -> dict[date, list[VendorBar]]:
    """Vendor bars grouped by the New York date they start on, in the order given."""
    sessions: dict[date, list[VendorBar]] = {}
    for bar in bars:
        sessions.setdefault(datetime.fromtimestamp(bar["t"] / 1000, ET).date(), []).append(bar)
    return sessions


def spy_minute_capture() -> dict[date, list[VendorBar]]:
    """The repo's SPY minute capture as the vendor's own minute aggregates, by session.

    Refuses a capture other than the one the 15-minute recording names, so a replaced capture fails here and does not
    leave the comparison holding something else.
    """
    held = (SPY_MINUTE_CAPTURE / "bars.json").read_bytes()
    named = json.loads((SPY_15_MINUTE / "recording.json").read_text(encoding="utf-8"))["compared_with"]["bars_sha256"]
    recorded = json.loads((SPY_MINUTE_CAPTURE / "metadata.json").read_text(encoding="utf-8"))["bars_sha256"]
    if not hashlib.sha256(held).hexdigest() == named == recorded:
        raise AssertionError("the SPY minute capture is not the one the 15-minute recording was compared with")
    return by_session(
        [
            {
                "t": row["timestamp"],
                "o": row["open"],
                "h": row["high"],
                "l": row["low"],
                "c": row["close"],
                "v": row["volume"],
                "vw": row["vwap"],
                "n": row["transactions"],
            }
            for row in json.loads(held)
        ]
    )
