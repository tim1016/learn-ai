"""One session's market, as the replay reads it, built from what a run loaded for that date (#3030).

The replay asks a ``SessionMarket`` three things: the underlying's close at a minute, the contract a leg's rules come
to at a price, and a contract's bar at a minute. ``DayMarket`` answers them from one trading date's underlying bars,
its listing and the bars of the contracts the session could trade.

The session's open and close come from the canonical calendar, so a half day closes when the calendar says it does
and no session time is written here.

A contract the market was never handed is not a contract that did not trade. ``option_bar`` answers ``None`` for a
contract whose day was loaded and had no bar at that minute (an empty day included), and raises
``ContractDayNotLoaded`` for a contract whose day was not loaded at all: that means ``session_needs`` named the wrong
contracts, which is a bug to see, and reading it as "did not trade" would turn a bug into a quiet result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.options.contract import OptionContract
from app.options_backtest.market import MinuteBar, Picked, PickRefused, SessionMarket
from app.options_backtest.picking import Listing, PreparedLeg, prepare_leg
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup
from app.utils.session_anchors import et_date_at_ms


class ContractDayNotLoaded(RuntimeError):
    """The replay asked for the bars of a contract whose day the market was not handed.

    A bug in ``session_needs``, never a fact about the market.
    """

    def __init__(self, contract: OptionContract, trading_date: date) -> None:
        self.contract = contract
        self.trading_date = trading_date
        super().__init__(
            f"the replay asked for the bars of {contract.occ_symbol} on {trading_date}, and that contract's day was "
            "never loaded: the contracts a session needs were worked out wrong"
        )


class MalformedBars(ValueError):
    """A series of bars that cannot be a day of minute bars named by their close."""


def bars_by_close(bars: Sequence[MinuteBar], *, trading_date: date, what: str) -> dict[int, MinuteBar]:
    """The bars keyed by the instant they close, after checking they are a plausible day of them.

    Every close is a whole minute on ``trading_date`` (New York date), strictly later than the one before it, and every
    price is a finite number above zero. A repeated or out-of-order bar would otherwise be overwritten in a dictionary
    and give a price nobody traded at. Bars outside the session on that date (pre-market, or an option's last
    quarter hour) are allowed: they are never asked for.
    """
    by_close: dict[int, MinuteBar] = {}
    previous: int | None = None
    for bar in bars:
        at = bar.close_ms
        if at % MS_PER_MINUTE:
            raise MalformedBars(f"{what} on {trading_date}: a bar closes at {at}, which is not on a whole minute")
        if et_date_at_ms(at) != trading_date:
            raise MalformedBars(
                f"{what} on {trading_date}: a bar closes at {at}, which falls on {et_date_at_ms(at)}: "
                "the bars are of another day"
            )
        if previous is not None and at <= previous:
            how = "twice" if at == previous else "after a later one"
            raise MalformedBars(
                f"{what} on {trading_date}: a bar closing at {at} comes {how}: bars must be in time order"
            )
        if not all(price.is_finite() and price > 0 for price in (bar.open, bar.high, bar.low, bar.close)):
            raise MalformedBars(f"{what} on {trading_date}: the bar closing at {at} has a price that is not above zero")
        by_close[at] = bar
        previous = at
    return by_close


class DayMarket:
    """A ``SessionMarket`` over the data loaded for one trading date."""

    def __init__(
        self,
        trading_date: date,
        *,
        underlying: Sequence[MinuteBar],
        listing: Listing,
        option_days: Mapping[OptionContract, Sequence[MinuteBar]],
    ) -> None:
        if listing.as_of != trading_date:
            raise ValueError(f"the listing describes {listing.as_of}, not {trading_date}")
        window = session_window_for_date(trading_date)
        self.trading_date = trading_date
        self.session_open_ms = window.open_ms_utc
        self.session_close_ms = window.close_ms_utc
        self._listing = listing
        self._underlying = {
            at: bar.close
            for at, bar in bars_by_close(underlying, trading_date=trading_date, what="the underlying").items()
        }
        self._options = {
            contract: bars_by_close(bars, trading_date=trading_date, what=contract.occ_symbol)
            for contract, bars in option_days.items()
        }
        self._legs: dict[LegSetup, PreparedLeg | PickRefused] = {}

    def underlying_close(self, close_ms: int) -> Decimal | None:
        return self._underlying.get(close_ms)

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        if at_ms % MS_PER_MINUTE or not self.session_open_ms < at_ms <= self.session_close_ms:
            raise ValueError(f"a pick is made on a minute close inside the session; {at_ms} is not one")
        prepared = self._legs.get(leg)
        if prepared is None:
            prepared = prepare_leg(leg, listing=self._listing, trading_date=self.trading_date)
            self._legs[leg] = prepared
        return prepared if isinstance(prepared, PickRefused) else prepared.pick(underlying_price)

    def option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        try:
            bars = self._options[contract]
        except KeyError:
            raise ContractDayNotLoaded(contract, self.trading_date) from None
        return bars.get(close_ms)


class _WindowOnlyMarket:
    """The market of a session the window rule skips: it knows when the session opens and closes, and nothing else."""

    def __init__(self, trading_date: date) -> None:
        window = session_window_for_date(trading_date)
        self.session_open_ms = window.open_ms_utc
        self.session_close_ms = window.close_ms_utc

    def underlying_close(self, close_ms: int) -> Decimal | None:
        raise self._not_to_be_asked()

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        raise self._not_to_be_asked()

    def option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        raise self._not_to_be_asked()

    @staticmethod
    def _not_to_be_asked() -> RuntimeError:
        return RuntimeError("a session the setup's window does not fit read market data: it should have been skipped")


def window_only_market(trading_date: date) -> SessionMarket:
    """A market for a session whose window the setup does not fit, so that nothing is read for it.

    The replay decides a skip from the session's open and close alone. Handing it this market means a replay that
    reached for data on a session it should have skipped fails at once instead of reading something.
    """
    return _WindowOnlyMarket(trading_date)
