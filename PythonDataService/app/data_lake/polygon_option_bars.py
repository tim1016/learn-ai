"""Polygon option minute-bar fetcher: one contract, one session, a confirmed answer or a typed error (#3030).

``GET /v2/aggs/ticker/O:<symbol>/range/1/minute/<day>/<day>`` answers one body
for four different facts: a listed contract that did not trade, a contract that
never existed, a contract after it expired, and a day with no session. Each is
HTTP 200, ``status: "OK"``, the ticker echoed, ``resultsCount: 0`` and no
``results`` at all (probed 2026-10-12). An empty answer therefore proves nothing
by itself. This fetcher only says whether the answer is a *well-formed* one; that
a contract was listed that day is for the caller to prove from the stored listing.

A confirmed answer is exactly: HTTP 200, a JSON object, a ``status`` of ``OK`` or
``DELAYED``, the ticker asked for, an ``adjusted`` that is absent or false (the request
asks for raw bars), a whole-number ``resultsCount`` of at least 0,
no ``next_url``, and a ``results`` list of exactly that many bars (absent or empty
when the count is 0). A session of one contract is far under the page limit, so a
next page is an error, never something to follow. Anything else raises one of the
lake's typed Polygon errors, and nothing is returned. The key rides in a header
(``polygon_option_http``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.data_lake.polygon_fetcher import _POLYGON_BASE, _TIMEOUT_S, PolygonFetchError
from app.data_lake.polygon_option_http import OPTION_TICKER_PREFIX, get_json_object, require_confirmed_status
from app.options.contract import OptionContract

#: The vendor's largest page for aggregates. A session is about 1,400 minutes at most.
_PAGE_LIMIT = 50_000

_PRICE_FIELDS = ("o", "h", "l", "c")


@dataclass(frozen=True)
class PolygonOptionBar:
    """One minute of trades in one contract, as the vendor wrote it.

    ``t_ms`` is the bar's start, epoch ms UTC. Prices and volume are the exact
    decimals in the JSON text; no float stands between the vendor's digits and the
    integer the lake stores.
    """

    t_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


async def fetch_option_minute_bars(contract_symbol: str, day: date, api_key: str) -> list[PolygonOptionBar]:
    """The minute bars of one option contract for one date, or a typed error.

    ``contract_symbol`` is the OCC symbol as ``OptionContract.occ_symbol`` writes
    it; the vendor's ``O:`` is added here. An empty list means the vendor confirmed
    "no bars for this ticker on this date" and nothing more. Raises the ``Polygon*``
    errors of ``polygon_fetcher``; never returns part of an answer.
    """
    OptionContract.from_occ_symbol(contract_symbol)  # a ticker that is not an OCC symbol never reaches a URL
    ticker = f"{OPTION_TICKER_PREFIX}{contract_symbol}"
    url = f"{_POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{day:%Y-%m-%d}/{day:%Y-%m-%d}"
    params = {"adjusted": "false", "sort": "asc", "limit": _PAGE_LIMIT}
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        payload = await get_json_object(client, url, params=params, api_key=api_key, label=ticker)
    return _confirmed_bars(payload, ticker)


def _confirmed_bars(payload: dict[str, Any], ticker: str) -> list[PolygonOptionBar]:
    require_confirmed_status(payload, ticker, 200)
    if payload.get("ticker") != ticker:
        raise PolygonFetchError(f"Polygon answered for ticker {payload.get('ticker')!r:.60}, not {ticker}", 200)
    if payload.get("next_url") is not None:
        raise PolygonFetchError(f"Polygon sent a next page for one session of {ticker}", 200)
    if "adjusted" in payload and payload["adjusted"] is not False:  # the request asks for raw bars
        raise PolygonFetchError(f"Polygon answer for {ticker} is not raw: adjusted is {payload['adjusted']!r:.40}", 200)
    count = payload.get("resultsCount")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise PolygonFetchError(f"Polygon answer for {ticker} has no whole-number resultsCount: {count!r:.40}", 200)
    rows = payload.get("results", [])  # absent means none; a null is not a list
    if not isinstance(rows, list):
        raise PolygonFetchError(f"Polygon results for {ticker} are not a list", 200)
    if len(rows) != count:
        raise PolygonFetchError(f"Polygon resultsCount {count} for {ticker} disagrees with its {len(rows)} bars", 200)
    return [_bar_from_row(row, ticker, index) for index, row in enumerate(rows)]


def _bar_from_row(row: Any, ticker: str, index: int) -> PolygonOptionBar:
    if not isinstance(row, dict):
        raise PolygonFetchError(f"Polygon bar {index} of {ticker} is not a JSON object", 200)
    t_ms = row.get("t")
    if isinstance(t_ms, bool) or not isinstance(t_ms, int):
        raise PolygonFetchError(f"Polygon bar {index} of {ticker} has no whole-number time t: {t_ms!r:.40}", 200)
    open_, high, low, close = (_number(row.get(name), name, ticker, index) for name in _PRICE_FIELDS)
    return PolygonOptionBar(
        t_ms=t_ms, open=open_, high=high, low=low, close=close, volume=_number(row.get("v"), "v", ticker, index)
    )


def _number(value: Any, name: str, ticker: str, index: int) -> Decimal:
    """A JSON number as the exact decimal it was written as; anything else is an error.

    A float can only arrive from a ``NaN`` or ``Infinity`` literal (a decimal point
    parses as ``Decimal``), which is no number the lake will store.
    """
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise PolygonFetchError(f"Polygon bar {index} of {ticker} has no number {name}: {value!r:.40}", 200)
    exact = Decimal(value)
    if not exact.is_finite():
        raise PolygonFetchError(f"Polygon bar {index} of {ticker} has a {name} that is not finite: {value!r:.40}", 200)
    return exact
