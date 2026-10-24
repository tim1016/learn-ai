"""The vendor's option endpoints over a pinned :class:`~tests._helpers.option_chain.OptionChain`, served at the HTTP seam (#3540).

The Cross-sectional study saves the option inputs it needs into the lake and reads them back. A test of that goes through the
lake's real fetchers, writers, catalog admission and readers; only the vendor is faked. :class:`ChainVendor` is that vendor for
a chain:

* ``GET /v3/reference/options/contracts?as_of=D``: every contract the chain lists on ``D``, in the vendor's row shape, paged
  ``page_size`` at a time with a ``next_url`` cursor (the lake follows it until the vendor names no next page, as it does a
  real listing of tens of thousands of contracts).
* ``GET /v2/aggs/ticker/O:SYMBOL/range/1/minute/D/D``: one minute bar for each contract the chain prices on ``D``, stamped as the
  vendor stamps, with its START: the minute that starts at 15:59, so it closes at the session's close; its four prices are the
  chain's daily price and its volume ``volume``. A contract the chain does not price on that day gets the vendor's empty answer.
  ``tail`` adds bars after the close, ``extra`` replaces a day's bars outright.

It records what it was asked, and the most option requests it had in flight at once. Prices are the chain's, to four decimals,
so the lake stores them exactly.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
import respx

from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.options.contract import OptionContract
from tests._helpers.option_chain import OptionChain
from tests._helpers.option_vendor import bar_row, no_trades, traded

CONTRACTS_URL = "https://api.polygon.io/v3/reference/options/contracts"
_BARS_PATH = re.compile(r"/v2/aggs/ticker/O:(?P<symbol>[A-Z0-9]+)/range/1/minute/(?P<day>\d{4}-\d{2}-\d{2})/.*")
_BARS_URL = r"https://api\.polygon\.io/v2/aggs/ticker/O:[A-Z0-9]+/range/1/minute/.*"
MINUTE_MS = 60_000


def listing_row(ticker: str, kind: str, strike: float, expiry: date, underlying: str, **overrides: Any) -> dict[str, Any]:
    """A listed contract exactly as the vendor sends it: nine keys, and no ``additional_underlyings``."""
    row: dict[str, Any] = {
        "cfi": "OCASPS" if kind == "call" else "OPASPS",
        "contract_type": kind,
        "exercise_style": "american",
        "expiration_date": expiry.isoformat(),
        "primary_exchange": "BATO",
        "shares_per_contract": 100,
        "strike_price": int(strike) if strike == int(strike) else strike,
        "ticker": ticker,
        "underlying_ticker": underlying,
    }
    row.update(overrides)
    return {key: value for key, value in row.items() if value is not None}


@dataclass
class ChainVendor:
    """The vendor for one chain; ``mount`` it inside a ``respx.mock`` and read what it was asked."""

    chain: OptionChain
    volume: int = 500
    page_size: int = 1000
    listing_latency_s: float = 0.0
    bar_latency_s: float = 0.0
    #: A listing as of these dates answers this HTTP status instead of a listing.
    listing_status: dict[date, int] = field(default_factory=dict)
    #: A listing as of these dates answers with no contracts at all.
    listing_empty: set[date] = field(default_factory=set)
    #: A contract's bars (by OCC symbol, without the ``O:``) answer this HTTP status.
    bar_status: dict[str, int] = field(default_factory=dict)
    #: Rows to append after the 15:59 bar of a contract's day (the after-hours tail), by (symbol, day).
    tail: Callable[[str, date], list[dict[str, Any]]] = lambda symbol, day: []
    #: Rows that replace a contract's day outright, by (symbol, day); ``None`` leaves the day as the chain prices it.
    extra: Callable[[str, date], list[dict[str, Any]] | None] = lambda symbol, day: None
    #: A contract (its vendor ticker, ``O:...``) the listing as of a day leaves out, though the chain lists it.
    unlisted: Callable[[str, date], bool] = lambda ticker, day: False
    #: Overrides for the listing row of a contract, by (ticker, day): ``None`` leaves it as the chain lists it.
    row_overrides: Callable[[str, date], dict[str, Any] | None] = lambda ticker, day: None
    #: The price of a contract on a day, if not the chain's.
    price: Callable[[str, date], float | None] | None = None
    #: Called with (symbol, day) when an option-bars request arrives, to hold or to fail it.
    on_bar_request: Callable[[str, date], None] | None = None
    listing_requests: list[date] = field(default_factory=list)
    listing_pages: int = 0
    bar_requests: list[tuple[str, date]] = field(default_factory=list)
    in_flight: int = 0
    peak_in_flight: int = 0

    @property
    def requests(self) -> int:
        """Every request the vendor answered, listing pages and bars alike."""
        return self.listing_pages + len(self.bar_requests)

    def mount(self, router: respx.MockRouter | None = None) -> None:
        """Answer the two endpoints on ``router`` (the active ``respx.mock`` context), or on the default one."""
        target = router if router is not None else respx
        target.get(CONTRACTS_URL).mock(side_effect=self._listing)
        target.get(url__regex=_BARS_URL).mock(side_effect=self._bars)

    # -- the listing ---------------------------------------------------------

    async def _listing(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        day = date.fromisoformat(params["as_of"])
        cursor = int(params.get("cursor", 0))
        self.listing_pages += 1
        if cursor == 0:
            self.listing_requests.append(day)
        if self.listing_latency_s:
            await asyncio.sleep(self.listing_latency_s)
        if day in self.listing_status:
            return httpx.Response(self.listing_status[day], json={"status": "ERROR", "request_id": "r"})
        contracts = [] if day in self.listing_empty else self.chain.contracts_alive(day)
        rows = [
            listing_row(t, kind, strike, expiry, self.chain.underlying, **(self.row_overrides(t, day) or {}))
            for t, kind, strike, expiry in contracts
            if not self.unlisted(t, day)
        ]
        page = rows[cursor : cursor + self.page_size]
        body: dict[str, Any] = {"request_id": "r", "status": "OK", "results": page}
        if cursor + self.page_size < len(rows):
            body["next_url"] = f"{CONTRACTS_URL}?underlying_ticker={self.chain.underlying}&as_of={day.isoformat()}&limit={self.page_size}&cursor={cursor + self.page_size}"
        return httpx.Response(200, json=body)

    # -- the bars ------------------------------------------------------------

    async def _bars(self, request: httpx.Request) -> httpx.Response:
        match = _BARS_PATH.fullmatch(request.url.path)
        assert match is not None, request.url
        symbol, day = match["symbol"], date.fromisoformat(match["day"])
        self.bar_requests.append((symbol, day))
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.on_bar_request is not None:
                self.on_bar_request(symbol, day)
            if self.bar_latency_s:
                await asyncio.sleep(self.bar_latency_s)
            if symbol in self.bar_status:
                return httpx.Response(self.bar_status[symbol], json={"status": "ERROR", "request_id": "r"})
            rows = self._rows(symbol, day)
            return traded(symbol, rows) if rows else no_trades(symbol)
        finally:
            self.in_flight -= 1

    def _rows(self, symbol: str, day: date) -> list[dict[str, Any]]:
        replaced = self.extra(symbol, day)
        if replaced is not None:
            return replaced
        ticker = f"O:{symbol}"
        price = self.price(ticker, day) if self.price is not None else self.chain.daily_price(ticker, day)
        if price is None:
            return []
        last_regular_start = session_close_ms_utc(day) - MINUTE_MS
        row = bar_row(0, o=price, h=price, l=price, c=price, v=self.volume, vw=price, n=5, t=last_regular_start)
        return [row, *self.tail(symbol, day)]


def contract_of(ticker: str) -> OptionContract:
    """The contract a vendor ticker (``O:...``) names."""
    return OptionContract.from_occ_symbol(ticker.removeprefix("O:"))


def payload_by_ticker(payload: Mapping[str, Mapping[str, float]]) -> Callable[[str, date], float | None]:
    """A ``ChainVendor.price`` that serves a recorded table of ``{ticker: {iso day: price}}``; a day not in it is not traded."""
    return lambda ticker, day: payload.get(ticker, {}).get(day.isoformat())
