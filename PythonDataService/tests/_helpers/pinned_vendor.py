"""Pinned synthetic Polygon minute aggregates, served at the vendor's HTTP seam (#3521).

A research study's lake path is tested end to end: these bars are what the
*vendor* says, ``mock_vendor`` serves them to the real ``ensure_data`` fetcher,
and the real lake writer, catalog (in-memory stand-in), admission and reader do
the rest. The same bars feed the old provider path's transformation, so the
provider-versus-lake comparison runs on one payload.

Everything is seeded: ``vendor_history(days, seed=...)`` returns the same bars
every time. Session shape comes from the canonical calendar (open, close,
early closes), never a literal clock time; the vendor's extended window is
expressed as offsets from the scheduled session (04:00 ET is 330 minutes
before a 09:30 open; the after-hours tape runs 240 minutes past the close, so
17:00 on an early close).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import httpx
import numpy as np
import respx

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc

_MINUTE_MS = 60_000
_PRE_MARKET_MINUTES = 330
_AFTER_HOURS_MINUTES = 240

#: A vendor bar, as the JSON carries it: t (start, ms UTC), o, h, l, c, v, vw, n.
VendorBar = dict[str, Any]


def vendor_session(
    day: date,
    *,
    seed: int,
    open_price: float,
    decimals: int = 2,
    regular_keep: float = 0.97,
    extended_keep: float = 0.45,
    fractional_volume: bool = False,
) -> list[VendorBar]:
    """One session's minute aggregates: extended and regular hours, with deterministic gaps.

    ``decimals`` is the precision of the vendor's prices; 4 or fewer sits on the
    lake's 1/10,000 grid, more is finer than the lake can hold.
    """
    rng = np.random.default_rng(seed * 1_000_003 + day.toordinal())
    open_ms = session_open_ms_utc(day)
    close_ms = session_close_ms_utc(day)
    stamps = list(range(open_ms - _PRE_MARKET_MINUTES * _MINUTE_MS, close_ms + _AFTER_HOURS_MINUTES * _MINUTE_MS, _MINUTE_MS))
    bars: list[VendorBar] = []
    price = open_price
    for t in stamps:
        regular = open_ms <= t < close_ms
        if rng.random() > (regular_keep if regular else extended_keep):
            continue
        step = float(rng.normal(0.0, 0.0006)) * price
        o = round(price, decimals)
        c = round(max(price + step, 0.01), decimals)
        wiggle = abs(float(rng.normal(0.0, 0.0003))) * price
        h = round(max(o, c) + wiggle, decimals)
        lo = round(max(min(o, c) - wiggle, 0.005), decimals)
        h, lo = max(h, o, c), min(lo, o, c)
        volume: float | int
        volume = round(float(rng.uniform(1, 5000)), 6) if fractional_volume else int(rng.integers(1, 5000))
        bars.append({"t": t, "o": o, "h": h, "l": lo, "c": c, "v": volume, "vw": round((o + c) / 2, decimals), "n": int(rng.integers(1, 90))})
        price = c
    return bars


def vendor_history(
    days: Sequence[date], *, seed: int = 7, start_price: float = 100.0, **session_options: Any
) -> dict[date, list[VendorBar]]:
    """Consecutive sessions, each opening where the last closed."""
    history: dict[date, list[VendorBar]] = {}
    price = start_price
    for day in days:
        session = vendor_session(day, seed=seed, open_price=price, **session_options)
        history[day] = session
        price = session[-1]["c"]
    return history


def scaled(history: Mapping[date, list[VendorBar]], factor: float, *, decimals: int = 2) -> dict[date, list[VendorBar]]:
    """The same tape with every price multiplied (a split-adjusted view of earlier days)."""
    out: dict[date, list[VendorBar]] = {}
    for day, bars in history.items():
        out[day] = [
            {
                **b,
                "o": round(b["o"] * factor, decimals),
                "h": round(b["h"] * factor, decimals),
                "l": round(b["l"] * factor, decimals),
                "c": round(b["c"] * factor, decimals),
                "vw": round(b["vw"] * factor, decimals),
                "v": round(b["v"] / factor, 6),  # a split-adjusted volume is the shares of the new basis
            }
            for b in bars
        ]
    return out


_AGG_URL = r"https://api\.polygon\.io/v2/aggs/ticker/{symbol}/range/1/minute/(\d{{4}}-\d{{2}}-\d{{2}})/(\d{{4}}-\d{{2}}-\d{{2}})"


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


class VendorCalls:
    """What the lake asked the vendor, so a test can say a held window cost no call."""

    def __init__(self) -> None:
        self.requests: list[tuple[date, date, bool]] = []

    @property
    def count(self) -> int:
        return len(self.requests)

    def days(self) -> list[date]:
        return [start for start, end, _ in self.requests if start == end]


def mock_vendor(
    symbol: str,
    raw: Mapping[date, list[VendorBar]],
    *,
    adjusted: Mapping[date, list[VendorBar]] | None = None,
    splits: Sequence[dict[str, Any]] = (),
    calls: VendorCalls | None = None,
) -> respx.Route:
    """Serve ``symbol``'s minute aggregates, and empty corporate-action and ticker-event feeds.

    The vendor's own ``adjusted`` query parameter picks the tape: ``adjusted``
    when given and asked for, else ``raw``. A day the tape lacks answers 200
    with no results, as the vendor does for a day without trades.
    """
    pattern = re.compile(_AGG_URL.format(symbol=re.escape(symbol)) + r".*")

    def _respond(request: httpx.Request) -> httpx.Response:
        match = pattern.match(str(request.url))
        if match is None:
            raise AssertionError(f"unexpected aggregate URL: {request.url}")
        start, end = date.fromisoformat(match.group(1)), date.fromisoformat(match.group(2))
        wants_adjusted = request.url.params.get("adjusted") == "true"
        if calls is not None:
            calls.requests.append((start, end, wants_adjusted))
        tape = adjusted if (wants_adjusted and adjusted is not None) else raw
        results = [bar for day, bars in sorted(tape.items()) if start <= day <= end for bar in bars]
        payload: dict[str, Any] = {"ticker": symbol, "status": "OK", "resultsCount": len(results)}
        if results:
            payload["results"] = results
        return httpx.Response(200, content=_json_bytes(payload), headers={"content-type": "application/json"})

    route = respx.get(url__regex=pattern).mock(side_effect=_respond)
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/splits.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": list(splits)})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/dividends.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": []})
    )
    respx.get(re.compile(r"https://api\.polygon\.io/v3/reference/tickers/.*/events.*")).mock(
        return_value=httpx.Response(200, json={"status": "OK", "results": {"events": []}})
    )
    return route
