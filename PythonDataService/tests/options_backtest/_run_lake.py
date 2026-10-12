"""A fake vendor for the whole of a run's data: the underlying's minute bars, a day's listing and each contract's day.

A run is tested over the lake's own functions, so the vendor is faked at the HTTP layer with ``respx``, answering in
the shapes the vendor really sent (``tests/_helpers/option_vendor.py``). A scene is a dictionary of ``Day`` (the
fixture of ``_run_data.py``): minute ``k`` is the bar that closes ``k`` minutes after the session's open, so the
vendor stamps it with the start ``k - 1`` minutes after the open. A contract listed in a day's ``strikes`` and absent
from its ``options`` is listed and never traded: the vendor answers it with no results. A test using this is
decorated with ``@respx.mock`` and asks for ``fake_catalog`` and ``tmp_lake`` (``_lake.py``); the launcher the stock
path asks for its calendar is mocked too.
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

from app.options.contract import OptionContract
from app.options_backtest.setup import MS_PER_MINUTE
from tests._helpers.fake_lake_catalog import mock_launcher
from tests._helpers.option_vendor import no_trades, traded
from tests._helpers.option_vendor import outside_plan as outside_the_plan
from tests.options_backtest._lake import CONTRACTS_URL, vendor_listing_row
from tests.options_backtest._run_data import Day, open_ms

# respx hands a named group to the side effect as a keyword; the handlers read the request, so the mounts use
# patterns without names and the handlers read the path with these.
_OPTION_BARS_PATH = re.compile(r"/v2/aggs/ticker/O:(?P<symbol>[A-Z0-9]+)/range/1/minute/.*")
_STOCK_BARS_PATH = re.compile(r"/v2/aggs/ticker/(?P<symbol>[A-Z.]+)/range/1/minute/(?P<day>\d{4}-\d{2}-\d{2})/.*")
_OPTION_BARS_URL = r"https://api\.polygon\.io/v2/aggs/ticker/O:[A-Z0-9]+/range/1/minute/.*"
_STOCK_BARS_URL = r"https://api\.polygon\.io/v2/aggs/ticker/[A-Z.]+/range/1/minute/\d{4}-\d{2}-\d{2}/.*"


@dataclass
class Vendor:
    """What the fake vendor was asked, and a switch for each way it can fail."""

    scene: Mapping[date, Day]
    underlying: str = "SPY"
    # Seconds each option-bars request takes, so requests overlap and the most in flight can be seen.
    option_latency_s: float = 0.0
    # Contracts whose bars request is answered with a status (a symbol -> HTTP status), for a failure mid-download.
    option_errors: dict[str, int] = field(default_factory=dict)
    # Contracts whose request is answered "outside the plan".
    outside_plan: set[str] = field(default_factory=set)
    listing_errors: dict[date, int] = field(default_factory=dict)
    stock_errors: dict[date, int] = field(default_factory=dict)
    requests: list[str] = field(default_factory=list)
    option_requests: list[str] = field(default_factory=list)
    stock_requests: list[date] = field(default_factory=list)
    listing_requests: list[date] = field(default_factory=list)
    in_flight: int = 0
    peak_in_flight: int = 0
    # A hook run when an option request arrives, to cancel a run mid-download.
    on_option_request: Callable[[str], None] | None = None

    def mount(self) -> None:
        mock_launcher()
        respx.get(CONTRACTS_URL).mock(side_effect=self._listing)
        respx.get(url__regex=_OPTION_BARS_URL).mock(side_effect=self._option_bars)
        respx.get(url__regex=_STOCK_BARS_URL).mock(side_effect=self._stock_bars)

    @property
    def vendor_requests(self) -> int:
        return len(self.requests)

    def _log(self, request: httpx.Request) -> None:
        self.requests.append(str(request.url))

    async def _listing(self, request: httpx.Request) -> httpx.Response:
        self._log(request)
        day = date.fromisoformat(request.url.params["as_of"])
        self.listing_requests.append(day)
        if day in self.listing_errors:
            return httpx.Response(self.listing_errors[day], json={"status": "ERROR"})
        scene_day = self.scene[day]
        rows = [
            vendor_listing_row(
                OptionContract.of(root=self.underlying, expiry_date=day, right=right, strike=strike),
                underlying=self.underlying,
            )
            for right in ("call", "put")
            for strike in scene_day.strikes
        ]
        return httpx.Response(200, json={"request_id": "r", "status": "OK", "results": rows})

    async def _option_bars(self, request: httpx.Request) -> httpx.Response:
        self._log(request)
        match = _OPTION_BARS_PATH.fullmatch(request.url.path)
        assert match is not None
        symbol = match["symbol"]
        self.option_requests.append(symbol)
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.on_option_request is not None:
                self.on_option_request(symbol)
            if self.option_latency_s:
                await asyncio.sleep(self.option_latency_s)
            if symbol in self.outside_plan:
                return outside_the_plan()
            if symbol in self.option_errors:
                return httpx.Response(self.option_errors[symbol], json={"status": "ERROR", "error": "fake"})
            contract = OptionContract.from_occ_symbol(symbol)
            day = contract.expiry_date
            series = self.scene[day].options.get((contract.right, int(contract.strike)))
            if not series:
                return no_trades(symbol)
            return traded(symbol, [_option_row(day, minute, series[minute]) for minute in sorted(series)])
        finally:
            self.in_flight -= 1

    async def _stock_bars(self, request: httpx.Request) -> httpx.Response:
        self._log(request)
        match = _STOCK_BARS_PATH.fullmatch(request.url.path)
        assert match is not None
        day = date.fromisoformat(match["day"])
        self.stock_requests.append(day)
        if day in self.stock_errors:
            return httpx.Response(self.stock_errors[day], json={"status": "ERROR"})
        prices = self.scene[day].underlying
        results: list[dict[str, Any]] = []
        for minute in sorted(prices):
            price = prices[minute]
            opened, high, low, closed = (price, price, price, price) if isinstance(price, str) else price
            results.append(
                {
                    "v": 1000,
                    "vw": float(closed),
                    "o": float(opened),
                    "c": float(closed),
                    "h": float(high),
                    "l": float(low),
                    "t": open_ms(day) + (minute - 1) * MS_PER_MINUTE,
                    "n": 10,
                }
            )
        return httpx.Response(200, json={"ticker": match["symbol"], "status": "OK", "results": results})


def _option_row(day: date, minute: int, prices: tuple[str, ...]) -> dict[str, Any]:
    if len(prices) == 2:
        opened, closed = prices
        high, low = max(float(opened), float(closed)), min(float(opened), float(closed))
    else:
        opened, high, low, closed = prices  # type: ignore[assignment]
    return {
        "v": 10,
        "vw": float(closed),
        "o": float(opened),
        "c": float(closed),
        "h": float(high),
        "l": float(low),
        "t": open_ms(day) + (minute - 1) * MS_PER_MINUTE,
        "n": 1,
    }
