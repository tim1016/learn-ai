"""A pinned option chain: the generator behind the Cross-sectional option-input tests (#3524, #3540).

:class:`OptionChain` is a deterministic listed chain (weekly Friday expiries, a regular strike grid) whose option prices
are Black-Scholes prices of a known volatility on a known underlying path. It answers what the study asks of the lake:

* as a :class:`~app.research.options.option_inputs.ChainSource` (``listing_as_of``), in memory, for the contract finder;
* ``daily_price`` for a contract on a session, the price the lake path stores as the 15:59 minute's close
  (``tests/_helpers/option_chain_vendor.py`` serves it at the vendor's HTTP seam, through the lake's real writers);
* ``member_inputs`` builds a member's :class:`~app.research.options.option_inputs.MemberOptionInputs` in memory, for tests
  of the IV build that need no lake.

Because the prices come from a known volatility, what the study returns can be checked against the number that generated
it. Everything is integer arithmetic on the calendar (no ``sin``, no RNG), so the chain is the same on every platform.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from app.research.options.contract_finder import find_bracket_contracts
from app.research.options.option_inputs import (
    ListedOption,
    MemberOptionInputs,
    OptionDayClose,
    OptionInputCoverage,
    needed_contract_days,
)
from app.services.bs_greeks import bs_european_price

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


def stock_rows(days: Sequence[date], *, start: float = 150.0, step: float = 0.1) -> list[dict[str, Any]]:
    """Daily stock rows as the study hands them over: a gentle walk, each stamped with its session's open."""
    from app.lean_sidecar.trading_calendar import session_open_ms_utc

    return [
        {
            "timestamp": session_open_ms_utc(day),
            "open": start + step * i,
            "high": start + step * i + 1.0,
            "low": start + step * i - 1.0,
            "close": round(start + step * i, 4),
            "volume": 1_000_000.0,
        }
        for i, day in enumerate(days)
    ]


def default_sigma(day: date) -> float:
    """A volatility that moves a little day to day (22%-23%), as integer arithmetic on the date."""
    return 0.22 + 0.01 * ((day.toordinal() * 7) % 11) / 11


@dataclass
class OptionChain:
    """One underlying's listed chain, priced off a known volatility.

    ``spots`` is the underlying's price on each session, which the options are priced off; ``rate`` the
    risk-free rate they are priced with (the study's IV solve must use the same one to recover ``sigma``).
    """

    underlying: str
    spots: Mapping[date, float]
    rate: float
    sigma: Callable[[date], float] = default_sigma
    strike_step: float = 1.0
    #: Listed expiries; weekly Fridays around the underlying's path when omitted.
    expiries: tuple[date, ...] | None = None
    #: The first session an expiry is listed on; an expiry missing here is listed from the start. A listing as of an
    #: earlier date does not hold it, which is what forces the contract finder to list again.
    listed_on: Mapping[date, date] = field(default_factory=dict)
    #: Every session ``listing_as_of`` was asked for, in order: the lookups the contract finder made.
    listing_requests: list[date] = field(default_factory=list)

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

    def contracts_alive(self, day: date) -> list[tuple[str, str, float, date]]:
        """Every contract alive on ``day`` as ``(ticker, call or put, strike, expiry)``, in ticker order."""
        alive = [
            (option_ticker(self.underlying, expiry, kind, strike), kind, strike, expiry)
            for expiry in self._expiries()
            if expiry >= day and self.listed_on.get(expiry, date.min) <= day
            for strike in self._strikes()
            for kind in ("call", "put")
        ]
        return sorted(alive)

    # -- what the lake path serves --------------------------------------------

    def listing_as_of(self, day: date) -> tuple[ListedOption, ...]:
        """The chain as the contract finder reads it (``ChainSource``): standard contracts alive on ``day``."""
        self.listing_requests.append(day)
        return tuple(ListedOption(ticker, kind, strike, expiry) for ticker, kind, strike, expiry in self.contracts_alive(day))

    def daily_price(self, ticker: str, day: date) -> float | None:
        """The contract's price on a session: Black-Scholes at the day's volatility, rounded to 1e-4; ``None`` below a cent
        or on or after the expiry, as the chain's market printed nothing."""
        underlying, expiry, kind, strike = parse_option_ticker(ticker)
        assert underlying == self.underlying
        if day not in self.spots or day >= expiry:
            return None
        price = bs_european_price(self.spots[day], strike, (expiry - day).days / 365.0, self.rate, self.sigma(day), kind == "call")
        return None if price < 0.01 else round(price, 4)


class PricedChain(Protocol):
    """What ``member_inputs`` needs of a chain: the underlying's name, the chain as a ``ChainSource``, a price per session."""

    underlying: str

    def listing_as_of(self, day: date) -> tuple[ListedOption, ...]: ...

    def daily_price(self, ticker: str, day: date) -> float | None: ...


def member_inputs(
    chain: PricedChain,
    stock_rows: Sequence[dict[str, Any]],
    days: Sequence[date],
    *,
    volume: int = 500,
    price: Callable[[str, date], float | None] | None = None,
) -> MemberOptionInputs:
    """A member's option inputs for ``days``, in memory: the contracts the finder picks over ``chain``, each one's price.

    ``price`` replaces ``chain.daily_price`` where a test needs another number (a contract that did not trade is ``None``).
    Nothing is read from or written to a lake; the digests are placeholders.
    """
    priced = price or chain.daily_price
    bracket = find_bracket_contracts(chain.underlying, days[0].isoformat(), days[-1].isoformat(), chain, list(stock_rows))
    folded: dict[tuple[str, date], OptionDayClose] = {}
    needed = {} if bracket.empty else needed_contract_days(bracket)
    for day, contracts in needed.items():
        for contract in contracts:
            value = priced(contract, day)
            close = None if value is None else Decimal(repr(value))
            folded[(contract, day)] = OptionDayClose(contract, day, close, Decimal(0 if close is None else volume), "0" * 64)
    return MemberOptionInputs(
        ticker=chain.underlying,
        bracket=bracket,
        days=folded,
        listings_read={},
        coverage=OptionInputCoverage(days_expected=len(folded)),
        data_root_id="in-memory",
    )
