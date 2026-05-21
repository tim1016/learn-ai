"""Polygon corp-action fetchers: splits + dividends.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

Both endpoints follow the same paginated `next_url` pattern as the aggregate
fetcher (see polygon_fetcher.py). Results are sorted ascending by event date
so downstream consumers (factor_files.py) can build cumulative adjustments
left-to-right.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_TIMEOUT_S = 30.0


@dataclass(frozen=True, order=True)
class SplitEvent:
    """One split event. split_from:split_to (e.g. 1:4 = 4-for-1 split).

    `order=True` lets callers `sorted([...])` in execution_date order
    (the field is YYYY-MM-DD ISO so lexical sort = chronological sort).
    """

    execution_date: str
    split_from: float
    split_to: float


@dataclass(frozen=True, order=True)
class DividendEvent:
    """One cash dividend (USD, ex-date)."""

    ex_dividend_date: str
    cash_amount: float


async def fetch_splits(symbol: str, api_key: str) -> list[SplitEvent]:
    """Fetch all split events for symbol, sorted ascending by execution_date."""
    url = f"{_POLYGON_BASE}/v3/reference/splits"
    params = {"ticker": symbol.upper(), "limit": 1000, "apiKey": api_key}
    rows = await _paginated_get(url, params)
    out = [
        SplitEvent(
            execution_date=r["execution_date"],
            split_from=float(r["split_from"]),
            split_to=float(r["split_to"]),
        )
        for r in rows
        if r.get("ticker", "").upper() == symbol.upper()
    ]
    return sorted(out)


async def fetch_dividends(symbol: str, api_key: str) -> list[DividendEvent]:
    """Fetch all cash dividend events for symbol, sorted ascending by ex_dividend_date."""
    url = f"{_POLYGON_BASE}/v3/reference/dividends"
    params = {"ticker": symbol.upper(), "limit": 1000, "apiKey": api_key}
    rows = await _paginated_get(url, params)
    out = [
        DividendEvent(
            ex_dividend_date=r["ex_dividend_date"],
            cash_amount=float(r["cash_amount"]),
        )
        for r in rows
        if r.get("ticker", "").upper() == symbol.upper()
    ]
    return sorted(out)


async def _paginated_get(url: str, params: dict) -> list[dict]:
    """Follow next_url pagination until exhausted. Returns all result rows."""
    out: list[dict] = []
    next_url: str | None = url
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        while next_url is not None:
            req_params = params if next_url == url else {"apiKey": params["apiKey"]}
            resp = await client.get(next_url, params=req_params)
            resp.raise_for_status()
            payload = resp.json()
            out.extend(payload.get("results") or [])
            next_url = payload.get("next_url")
    return out
