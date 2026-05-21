"""Polygon ticker-event fetcher.

Polygon's /v3/reference/tickers/{ticker}/events returns the history of
ticker-change events for a symbol. We normalize to TickerEvent for the LEAN
map-file builder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_TIMEOUT_S = 30.0


@dataclass(frozen=True, order=True)
class TickerEvent:
    """A point at which the symbol's ticker changed to `new_ticker`."""

    date: str  # YYYY-MM-DD
    new_ticker: str


async def fetch_ticker_events(symbol: str, api_key: str) -> list[TickerEvent]:
    """Fetch all ticker-change events for symbol, sorted ascending by date.

    Returns an empty list if the symbol has no events or the endpoint returns
    404 (some symbols never changed tickers and have no event history).
    """
    url = f"{_POLYGON_BASE}/v3/reference/tickers/{symbol.upper()}/events"
    params = {"types": "ticker_change", "apiKey": api_key}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.get(url, params=params)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        payload = resp.json()

    events_raw = (payload.get("results") or {}).get("events") or []
    out: list[TickerEvent] = []
    for ev in events_raw:
        if ev.get("type") != "ticker_change":
            continue
        chg = ev.get("ticker_change") or {}
        ticker = chg.get("ticker")
        ev_date = ev.get("date")
        if ticker and ev_date:
            out.append(TickerEvent(date=ev_date, new_ticker=ticker))
    return sorted(out)
