"""The vendor's side of the Realized vs IV comparisons: an independent stand-in for its 15-minute and day aggregates (#3525).

The lake path folds minute bars itself. To say what the move changes, the other side has to be built without that fold:
these functions aggregate the pinned *minute* tape in the plainest way (first open, max high, min low, last close, summed
volume per bucket) and hand the result to the real provider code as Polygon ``Agg`` objects. Two assumptions live here and
are unverified against live data, because they cannot be proven offline:

* Polygon's 15-minute aggregate equals a fold of its minute aggregates (the same trades build both).
* Polygon's day aggregate covers the regular session only, with the first and last regular minute standing in for the
  official opening and closing prints (which a real day bar uses and a minute bar does not).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.pinned_vendor import VendorBar

FIFTEEN_MINUTES_MS = 15 * 60_000


def vendor_aggregates(
    tape: dict[date, list[VendorBar]], window: Sequence[date], *, bar_size: str, regular_only: bool
) -> list[VendorBar]:
    """The vendor's ``bar_size`` aggregates over ``window``, from the minute tape, in the vendor's JSON shape.

    A 15-minute bar is a bucket of the minute tape (the offsets between UTC and ET are whole hours, so the boundaries
    agree). A day bar is one per session, stamped at ET midnight, over the regular session if ``regular_only`` and over
    every minute otherwise.
    """
    if bar_size == "15m":
        folded: dict[int, VendorBar] = {}
        for day in window:
            for m in tape[day]:
                key = m["t"] // FIFTEEN_MINUTES_MS * FIFTEEN_MINUTES_MS
                if key not in folded:
                    folded[key] = {**m, "t": key}
                else:
                    bar = folded[key]
                    bar["h"], bar["l"], bar["c"] = max(bar["h"], m["h"]), min(bar["l"], m["l"]), m["c"]
                    bar["v"] += m["v"]
                    bar["n"] += m["n"]
        return [folded[k] for k in sorted(folded)]
    rows: list[VendorBar] = []
    for day in window:
        minutes = [b for b in tape[day] if not regular_only or session_open_ms_utc(day) <= b["t"] < session_close_ms_utc(day)]
        rows.append(
            {
                "t": et_midnight_ms(day),
                "o": minutes[0]["o"],
                "h": max(b["h"] for b in minutes),
                "l": min(b["l"] for b in minutes),
                "c": minutes[-1]["c"],
                "v": sum(b["v"] for b in minutes),
                "vw": round(float(np.mean([b["vw"] for b in minutes])), 4),
                "n": sum(b["n"] for b in minutes),
            }
        )
    return rows


def aggregates_as_rows(aggregates: Sequence[VendorBar]) -> list[dict]:
    """Aggregates as the study's bar rows: the vendor's own floats, volume as a float."""
    return [
        {"timestamp": a["t"], "open": a["o"], "high": a["h"], "low": a["l"], "close": a["c"], "volume": float(a["v"])}
        for a in aggregates
    ]


def frame_of(rows: Sequence[dict]) -> pd.DataFrame:
    """Rows as the estimators take them: an ``int64 ms UTC`` index and float columns."""
    index = pd.Index([r["timestamp"] for r in rows], name="ts", dtype=np.int64)
    return pd.DataFrame({c: [float(r[c]) for r in rows] for c in ("open", "high", "low", "close", "volume")}, index=index)
