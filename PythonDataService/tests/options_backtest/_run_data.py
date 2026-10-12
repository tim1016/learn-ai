"""What a run reads, from dictionaries: bars, listings and a fake ``RunData`` that remembers how it was asked.

Minutes are counted after the session's open, as in ``_market.py``: minute ``k`` is the bar closing ``k`` minutes
after the open, so the bar of minute 5 holds the trades from the fourth to the fifth minute. A price list is
``{minute: price}`` for the underlying and ``{minute: (open, close)}`` or ``{minute: (open, high, low, close)}`` for a
contract; a minute that is not listed did not trade, and nothing is filled in.
"""

from __future__ import annotations

import gc
import hashlib
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract, OptionRight
from app.options_backtest.data import Captured
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import ListedContract, Listing
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup

D = Decimal

# Sessions the scenes run on. Wednesday to Friday are consecutive; the last of September and the first of October
# are two months; the day after Thanksgiving is a half day that closes at 13:00. Each is checked against the
# calendar when the module loads, so a wrong date fails here and does not leave a test that proves nothing.
WED = date(2026, 10, 7)
THU = date(2026, 10, 8)
FRI = date(2026, 10, 9)
HALF_DAY = date(2026, 11, 27)
SESSIONS_OF_TWO_MONTHS = (date(2026, 9, 29), date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2))
for _day in (WED, THU, FRI, HALF_DAY, *SESSIONS_OF_TWO_MONTHS):
    session_window_for_date(_day)  # raises LookupError for a day that is not a session
assert session_window_for_date(HALF_DAY).close_ms_utc - session_window_for_date(HALF_DAY).open_ms_utc == 210 * 60_000


def open_ms(day: date) -> int:
    return session_window_for_date(day).open_ms_utc


def close_ms(day: date, minute: int) -> int:
    """The instant of the bar closing ``minute`` minutes after ``day``'s open."""
    return open_ms(day) + minute * MS_PER_MINUTE


def contract_on(day: date, right: OptionRight, strike: int | str) -> OptionContract:
    """The SPY contract expiring on ``day`` itself: the one a same-day leg picks."""
    return OptionContract.of(root="SPY", expiry_date=day, right=right, strike=D(strike))


def option_bars(day: date, series: Mapping[int, tuple[str, ...]]) -> tuple[MinuteBar, ...]:
    """A contract's bars from ``{minute: (open, close)}`` or ``{minute: (open, high, low, close)}``, in time order."""
    bars = []
    for minute in sorted(series):
        prices = tuple(D(price) for price in series[minute])
        if len(prices) == 2:
            opened, closed = prices
            prices = (opened, max(opened, closed), min(opened, closed), closed)
        opened, high, low, closed = prices
        bars.append(MinuteBar(close_ms=close_ms(day, minute), open=opened, high=high, low=low, close=closed))
    return tuple(bars)


def underlying_bars(day: date, prices: Mapping[int, str]) -> tuple[MinuteBar, ...]:
    """The underlying's bars: one price a minute, as open, high, low and close."""
    return option_bars(day, {minute: (price, price) for minute, price in prices.items()})


def steady(price: str, first: int = 1, last: int = 390) -> dict[int, str]:
    """An underlying that closed at one price in every minute from ``first`` to ``last``."""
    return {minute: price for minute in range(first, last + 1)}


def flat(price: str, first: int, last: int) -> dict[int, tuple[str, ...]]:
    """A contract that traded at one price, open and close, in every minute from ``first`` to ``last``."""
    return {minute: (price, price) for minute in range(first, last + 1)}


def listing_on(
    day: date,
    *,
    strikes: Sequence[int] = (495, 500, 505, 510),
    expiries: Sequence[date] | None = None,
    complete: bool = True,
) -> Listing:
    """A listing as of ``day``: calls and puts at each strike, expiring on ``day`` unless ``expiries`` says otherwise."""
    return Listing(
        underlying="SPY",
        as_of=day,
        contracts=tuple(
            ListedContract(
                OptionContract.of(root="SPY", expiry_date=expiry, right=right, strike=D(strike)),
                multiplier=100,
                standard=True,
            )
            for expiry in (expiries or (day,))
            for right in ("call", "put")
            for strike in strikes
        ),
        complete=complete,
    )


def sha_of(kind: str, day: date, contract: OptionContract | None = None) -> str:
    """The pin the fake gives a stored file: a hash of what the file is, so a test knows what it should be."""
    name = f"{kind}|{day}|{'' if contract is None else contract.occ_symbol}"
    return hashlib.sha256(name.encode()).hexdigest()


class _Handed(list):  # a list that can be weakly referenced, to see whether a run lets go of it
    pass


class FakeRunData:
    """A ``RunData`` over dictionaries. A key that is absent is not captured; an empty sequence never traded.

    Every answer is a new object, so a test can see whether the run still holds what it was handed. ``asked``
    lists the questions in order: ``("underlying", day)``, ``("listing", day)``, ``("option", day, occ symbol)``.
    """

    def __init__(
        self,
        *,
        underlying: Mapping[date, Sequence[MinuteBar]] | None = None,
        listings: Mapping[date, Listing] | None = None,
        options: Mapping[tuple[OptionContract, date], Sequence[MinuteBar]] | None = None,
    ) -> None:
        self._underlying = dict(underlying or {})
        self._listings = dict(listings or {})
        self._options = dict(options or {})
        self.asked: list[tuple[Any, ...]] = []
        self._handed: list[weakref.ref[object]] = []
        # How many things handed out earlier were still alive, each time the underlying of a new session was asked.
        self.alive_at_each_session: list[int] = []

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        self.alive_at_each_session.append(self.handed_out_and_still_alive())
        self.asked.append(("underlying", trading_date))
        if trading_date not in self._underlying:
            return None
        return self._hand_out(self._fresh(self._underlying[trading_date]), sha_of("underlying", trading_date))

    def listing(self, trading_date: date) -> Captured[Listing] | None:
        self.asked.append(("listing", trading_date))
        if trading_date not in self._listings:
            return None
        return self._hand_out(replace(self._listings[trading_date]), sha_of("listing", trading_date))

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None:
        self.asked.append(("option", trading_date, contract.occ_symbol))
        if (contract, trading_date) not in self._options:
            return None
        return self._hand_out(
            self._fresh(self._options[(contract, trading_date)]), sha_of("option", trading_date, contract)
        )

    def _fresh(self, bars: Sequence[MinuteBar]) -> _Handed:
        """The bars as new objects, each watched, so that a run that keeps a bar keeps it where a test can see."""
        handed = _Handed(replace(bar) for bar in bars)
        self._handed.extend(weakref.ref(bar) for bar in handed)
        return handed

    def _hand_out[T](self, value: T, sha256: str) -> Captured[T]:
        captured = Captured(value=value, sha256=sha256)
        self._handed.append(weakref.ref(value))  # type: ignore[arg-type]
        self._handed.append(weakref.ref(captured))
        return captured

    def handed_out_count(self) -> int:
        return len(self._handed)

    def handed_out_and_still_alive(self) -> int:
        gc.collect()
        return sum(1 for reference in self._handed if reference() is not None)

    def contracts_asked(self, trading_date: date) -> list[str]:
        return [str(question[2]) for question in self.asked if question[0] == "option" and question[1] == trading_date]


@dataclass
class Day:
    """What a scene stores of one session: the underlying, and each contract's bars by (right, strike).

    ``underlying`` is ``{minute: price}``, ``options`` is ``{("call", 500): {minute: (open, close)}}``. A contract named
    in the listing and not in ``options`` is a contract whose day is not captured.
    """

    underlying: dict[int, str]
    options: dict[tuple[OptionRight, int], dict[int, tuple[str, ...]]] = field(default_factory=dict)
    strikes: tuple[int, ...] = (495, 500, 505, 510)
    complete: bool = True


def fake_data(
    days: dict[date, Day], *, without_underlying: tuple[date, ...] = (), without_listing: tuple[date, ...] = ()
) -> FakeRunData:
    """A ``RunData`` over the days, with the stated gaps in what is captured."""
    return FakeRunData(
        underlying={
            day: underlying_bars(day, spec.underlying) for day, spec in days.items() if day not in without_underlying
        },
        listings={
            day: listing_on(day, strikes=spec.strikes, complete=spec.complete)
            for day, spec in days.items()
            if day not in without_listing
        },
        options={
            (contract_on(day, right, strike), day): option_bars(day, series)
            for day, spec in days.items()
            for (right, strike), series in spec.options.items()
        },
    )


def changed(written: OptionsBacktestSetup, **fields: Any) -> OptionsBacktestSetup:
    """The same setup with some of its fields changed, validated afresh."""
    return OptionsBacktestSetup.model_validate({**written.model_dump(mode="python"), **fields})
