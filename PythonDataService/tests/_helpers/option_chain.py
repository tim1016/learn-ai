"""A pinned option chain, served at the Polygon SDK client's seam (#3524).

The cross-sectional study keeps its option inputs (expiries, contracts, option bars) with the provider. A test of
the whole study therefore needs a provider to ask. :class:`OptionChain` is that provider: a deterministic listed
chain (weekly Friday expiries, a regular strike grid) whose daily option bars are Black-Scholes prices of a known
volatility on a known underlying path. It is installed as ``PolygonClientService.client`` — the SDK object whose
``list_options_contracts`` and ``list_aggs`` the service wraps — so the real wrapper code, the real contract
finder, the real IV builder and the real solver run over it, and what comes back out can be checked against the
volatility that generated the prices.

It serves *option* tickers only. A request for the underlying's own bars fails the test: the study's stock bars
come from the lake, and a provider fetch of the underlying is the bypass #3524 removed.

Everything is integer arithmetic on the calendar (no ``sin``, no RNG), so the chain is the same on every platform.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from polygon.rest.models import Agg, OptionsContract

from app.services.bs_greeks import bs_european_price
from app.utils.session_anchors import et_midnight_ms

FRIDAY = 4


def option_ticker(underlying: str, expiry: date, kind: str, strike: float) -> str:
    return f"O:{underlying}{expiry:%y%m%d}{'C' if kind == 'call' else 'P'}{round(strike * 1000):08d}"


def parse_option_ticker(ticker: str) -> tuple[str, date, str, float]:
    body = ticker[2:]
    tail = body[-15:]
    return (
        body[:-15],
        date(2000 + int(tail[0:2]), int(tail[2:4]), int(tail[4:6])),
        "call" if tail[6] == "C" else "put",
        int(tail[7:]) / 1000.0,
    )


def default_sigma(day: date) -> float:
    """A volatility that moves a little day to day (22%-23%), as integer arithmetic on the date."""
    return 0.22 + 0.01 * ((day.toordinal() * 7) % 11) / 11


@dataclass
class OptionChain:
    """The provider's option side for one underlying.

    ``spots`` is the underlying's price on each session, which the options are priced off; ``rate`` the
    risk-free rate they are priced with (the study's IV solve must use the same one to recover ``sigma``).
    ``bar_failure`` / ``contract_failure`` name a request that should fail: they return the exception to raise
    for an option ticker / an expiry, or ``None`` to answer it.
    """

    underlying: str
    spots: Mapping[date, float]
    rate: float
    sigma: Callable[[date], float] = default_sigma
    strike_step: float = 1.0
    #: Listed expiries; weekly Fridays around the underlying's path when omitted.
    expiries: tuple[date, ...] | None = None
    bar_failure: Callable[[str], Exception | None] = lambda ticker: None
    contract_failure: Callable[[str | None], Exception | None] = lambda expiry: None
    contract_requests: list[dict[str, Any]] = field(default_factory=list)
    bar_requests: list[str] = field(default_factory=list)

    # -- the listed chain ----------------------------------------------------

    def _expiries(self) -> list[date]:
        if self.expiries is not None:
            return sorted(self.expiries)
        first = min(self.spots) - timedelta(days=10)
        last = max(self.spots) + timedelta(days=70)
        days = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
        return [day for day in days if day.weekday() == FRIDAY]

    def _strikes(self) -> list[float]:
        low = math.floor(min(self.spots.values()) * 0.7)
        high = math.ceil(max(self.spots.values()) * 1.3)
        count = int((high - low) / self.strike_step) + 1
        return [low + i * self.strike_step for i in range(count)]

    # -- the SDK surface -----------------------------------------------------

    def list_options_contracts(self, **kwargs: Any) -> Iterator[OptionsContract]:
        self.contract_requests.append(kwargs)
        expiry_asked = kwargs.get("expiration_date")
        failure = self.contract_failure(expiry_asked)
        if failure is not None:
            raise failure
        as_of = date.fromisoformat(kwargs["as_of"]) if kwargs.get("as_of") else None
        lo = date.fromisoformat(kwargs["expiration_date_gte"]) if kwargs.get("expiration_date_gte") else None
        hi = date.fromisoformat(kwargs["expiration_date_lte"]) if kwargs.get("expiration_date_lte") else None
        kind = kwargs.get("contract_type")
        for expiry in self._expiries():
            if expiry_asked is not None and expiry.isoformat() != expiry_asked:
                continue
            if as_of is not None and expiry < as_of:
                continue
            if (lo is not None and expiry < lo) or (hi is not None and expiry > hi):
                continue
            for strike in self._strikes():
                if kwargs.get("strike_price_gte") is not None and strike < kwargs["strike_price_gte"]:
                    continue
                if kwargs.get("strike_price_lte") is not None and strike > kwargs["strike_price_lte"]:
                    continue
                for contract_type in ("call", "put"):
                    if kind is not None and kind != contract_type:
                        continue
                    yield OptionsContract.from_dict(
                        {
                            "ticker": option_ticker(self.underlying, expiry, contract_type, strike),
                            "underlying_ticker": self.underlying,
                            "contract_type": contract_type,
                            "strike_price": strike,
                            "expiration_date": expiry.isoformat(),
                            "exercise_style": "american",
                            "shares_per_contract": 100,
                            "primary_exchange": "BATO",
                        }
                    )

    def list_aggs(self, *, ticker: str, multiplier: int, timespan: str, from_: str, to: str, **_: Any) -> Iterator[Agg]:
        if not ticker.startswith("O:"):
            raise AssertionError(f"the study asked the provider for {ticker}'s own bars; stock bars come from the lake")
        self.bar_requests.append(ticker)
        failure = self.bar_failure(ticker)
        if failure is not None:
            raise failure
        underlying, expiry, kind, strike = parse_option_ticker(ticker)
        assert underlying == self.underlying and (multiplier, timespan) == (1, "day")
        start, end = date.fromisoformat(from_), date.fromisoformat(to)
        for day in sorted(self.spots):
            if not (start <= day <= end) or day >= expiry:
                continue
            price = bs_european_price(
                self.spots[day], strike, (expiry - day).days / 365.0, self.rate, self.sigma(day), kind == "call"
            )
            if price < 0.01:
                continue
            price = round(price, 4)
            yield Agg.from_dict(
                {"t": et_midnight_ms(day), "o": price, "h": price, "l": price, "c": price, "v": 500, "vw": price, "n": 12}
            )
