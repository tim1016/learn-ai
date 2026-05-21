"""Polygon /v2/aggs minute-trade fetcher.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 4.6

Always requests `adjusted=false` (raw bars; LEAN normalization mode='Raw' per
the v1 single-canonical-root constraint). Paginated via Polygon's next_url
header.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import httpx

logger = logging.getLogger(__name__)

_POLYGON_BASE = "https://api.polygon.io"
_TIMEOUT_S = 30.0


class PolygonFetchError(RuntimeError):
    """Base for all Polygon fetch failures."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PolygonAuthError(PolygonFetchError):
    """401 — bad/missing API key."""


class PolygonEntitlementError(PolygonFetchError):
    """403 — plan tier doesn't permit this data."""


class PolygonRateLimitedError(PolygonFetchError):
    """429 — back off and retry slower."""


class PolygonUnknownSymbolError(PolygonFetchError):
    """200 OK with status='NOT_FOUND' or 404 — Polygon doesn't recognize the symbol."""


@dataclass(frozen=True)
class PolygonBar:
    """One minute bar from Polygon /v2/aggs.

    t_ms is the bar's start time in UTC ms (Polygon's `t` field). Prices are
    raw floats from the JSON. Volume is an int.
    """

    t_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    n: int  # number of trades aggregated


async def fetch_minute_trade_aggregates(
    symbol: str,
    start: date,
    end: date,
    api_key: str,
) -> list[PolygonBar]:
    """Fetch minute-resolution trade aggregates for [start, end] inclusive.

    Returns bars in the order Polygon returned them (ascending t_ms).
    Pagination is handled transparently.

    Errors map onto Polygon* exception subclasses for callers to translate
    into ArtifactFailure.reason values.
    """
    url = (
        f"{_POLYGON_BASE}/v2/aggs/ticker/{symbol.upper()}/range/1/minute/"
        f"{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
    )
    params = {
        "adjusted": "false",
        "sort": "asc",
        "limit": 50_000,
        "apiKey": api_key,
    }
    out: list[PolygonBar] = []
    next_url: str | None = url

    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        while next_url is not None:
            # Polygon's next_url already includes apiKey; only pass params on
            # the first request.
            req_params = params if next_url == url else {"apiKey": api_key}
            try:
                resp = await client.get(next_url, params=req_params)
            except httpx.TimeoutException as exc:
                raise PolygonFetchError(f"Polygon request timed out for {symbol}: {exc}", status_code=None) from exc
            except httpx.RequestError as exc:
                raise PolygonFetchError(f"Polygon transport error for {symbol}: {exc}", status_code=None) from exc
            _raise_for_status(resp, symbol)
            payload = resp.json()
            _raise_for_payload_status(payload, symbol, resp.status_code)
            for r in payload.get("results") or []:
                out.append(
                    PolygonBar(
                        t_ms=int(r["t"]),
                        open=float(r["o"]),
                        high=float(r["h"]),
                        low=float(r["l"]),
                        close=float(r["c"]),
                        volume=int(r["v"]),
                        vwap=float(r.get("vw", 0.0)),
                        n=int(r.get("n", 0)),
                    )
                )
            next_url = payload.get("next_url")
    return out


def _raise_for_status(resp: httpx.Response, symbol: str) -> None:
    code = resp.status_code
    if code == 200:
        return
    if code == 401:
        raise PolygonAuthError(f"Polygon 401 for {symbol}: {resp.text[:200]}", code)
    if code == 403:
        raise PolygonEntitlementError(f"Polygon 403 for {symbol}: {resp.text[:200]}", code)
    if code == 404:
        raise PolygonUnknownSymbolError(f"Polygon 404 for {symbol}", code)
    if code == 429:
        raise PolygonRateLimitedError(f"Polygon 429 for {symbol}", code)
    raise PolygonFetchError(f"Polygon {code} for {symbol}: {resp.text[:200]}", code)


def _raise_for_payload_status(payload: dict, symbol: str, http_code: int) -> None:
    status = payload.get("status")
    if status == "OK" or status is None:
        return
    if status in ("NOT_FOUND", "ERROR_NOT_FOUND"):
        raise PolygonUnknownSymbolError(f"Polygon status={status} for {symbol}", http_code)
    # Other status values: treat as generic fetch error.
    raise PolygonFetchError(
        f"Polygon payload status={status} for {symbol}: {payload.get('error', '')[:200]}",
        http_code,
    )
