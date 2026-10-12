"""What an underlying had listed on a past trading date, and what each contract last traded at (#3030).

Formula: with ``T`` an instant of a session and a contract's bars named by their close, as ``market.MinuteBar`` says:
    the price at T   the ``close`` of the bar with the greatest ``close_ms <= T``, and that ``close_ms`` as its time
  A bar closing exactly at ``T`` holds the trades before ``T`` and counts; a bar closing after ``T`` never does. No
  price is carried from another day or filled between bars: a contract with no such bar has no price at ``T``.
  The money of a side is ``strike_for`` with the at-the-money rule on the standard ladder of that right and expiry,
  the picker's own, and the window is the ``n`` strikes of that ladder each side of it, clipped at its ends.
Reference: ``app/options_backtest/picking.py`` for the money and the ladder, ``classification.py`` for the tag of an
  expiry, ``market.py`` for what a bar's time means.
Canonical implementation: this file for the past-chain browser.
Validated against: tests/options_backtest/test_past_chain.py.

The module is pure: it reads through ``RunData`` and returns frozen values. It reads nothing but what ``RunData``
hands it, asks no clock and makes no request. ``LakeRunData`` is the reader that serves it from the lake, and a test
serves it from dictionaries.

Three questions have no price for an answer, and each says which it is: the data is not stored, the lake cannot read
it, or it was read and holds no trade by the instant. A price is shown only with the time of the bar it is the close of
(the end of the minute, not the trade's own time, which the bar does not carry).

The words of a "cannot read" reach the wire, and the lake's errors name a file by its path on the server's disk, so the
text is made here from what failed and the kind of error (``unreadable_problem``); the error's own text goes to the log.
A window never holds more contracts than a download takes (``MAX_WINDOW_CONTRACTS``): a window the page names the
contracts of is one call to download.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_bars import OptionBarsFileError
from app.data_lake.option_listing import OptionListingFileError
from app.lean_sidecar.trading_calendar import blocked_dates_in_range, session_window_for_date
from app.options.contract import OptionContract, OptionRight
from app.options.expiry import option_expiry_anchor_ms, option_expiry_date
from app.options_backtest.classification import ExpiryTag, tag_expiry
from app.options_backtest.data import Captured
from app.options_backtest.lake_data import StoredListing, UnderlyingDayUnreadable
from app.options_backtest.market import MinuteBar
from app.options_backtest.picking import ListedContract, strike_for
from app.options_backtest.setup import MS_PER_MINUTE, AtTheMoney
from app.utils.session_anchors import et_date_at_ms, require_schedulable_end

logger = logging.getLogger(__name__)

DEFAULT_STRIKES_EACH_SIDE = 10
# 14 strikes each side is 29 a right: 58 standard contracts, which fit under the cap below.
MAX_STRIKES_EACH_SIDE = 14
# The most contracts one window holds, calls and puts together, and so the most a download takes (``chain_download``
# uses this very number). The page names a window's contracts to download in one call. A window that non-standard
# contracts listed inside its strike range push over it is refused; the widest window's 58 standard ones leave room for two.
MAX_WINDOW_CONTRACTS = 60

_RIGHTS: tuple[OptionRight, ...] = ("call", "put")


# ---- Refusals: the request is the problem, not the market --------------------------------------


class ChainRefusal(ValueError):
    """A request the past chain cannot answer; ``code`` names why for the caller, the text says it in words."""

    code = "OPTIONS_BACKTEST_CHAIN_REFUSED"


class NotASession(ChainRefusal):
    code = "OPTIONS_BACKTEST_NOT_A_SESSION"


class NotAnExpiryAnchor(ChainRefusal):
    code = "OPTIONS_BACKTEST_NOT_AN_EXPIRY"


class InstantOutsideSession(ChainRefusal):
    code = "OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION"


class NotAContract(ChainRefusal):
    code = "OPTIONS_BACKTEST_NOT_A_CONTRACT"


class InstantNotOnAMinute(ChainRefusal):
    code = "OPTIONS_BACKTEST_INSTANT_NOT_ON_A_MINUTE"


class WindowTooWide(ChainRefusal):
    code = "OPTIONS_BACKTEST_WINDOW_TOO_WIDE"


# ---- A date, an expiry, a contract: how each arrives -------------------------------------------


@dataclass(frozen=True)
class ChainSession:
    """The session of a trading date, by the canonical calendar."""

    trading_date: date
    open_ms: int
    close_ms: int


def chain_session(trading_date_ms: int) -> ChainSession:
    """The session on the New York date that ``trading_date_ms`` falls on, with its own open and close.

    Any instant of the date names it (a page sends New York midnight). Raises ``NotASession`` with the reason in words
    for a weekend, a holiday, a date the calendar cannot schedule and a session that opened before 1970: the first
    hours of an instant of 1970 fall on 1969-12-31, a real session, but no data here reaches it and a wire instant is
    never negative.
    """
    try:
        require_schedulable_end(trading_date_ms)
    except ValueError as exc:
        raise NotASession(str(exc)) from exc
    day = et_date_at_ms(trading_date_ms)
    try:
        window = session_window_for_date(day)
    except LookupError as exc:
        why = {"weekend": "a weekend day", "holiday": "an exchange holiday"}.get(
            blocked_dates_in_range(day, day).get(day, ""), "not a trading day"
        )
        raise NotASession(f"{day.isoformat()} is {why}: the exchange had no session that day") from exc
    if window.open_ms_utc < 0:
        raise NotASession(f"{day.isoformat()} is before 1970: it is not a session this page can show")
    return ChainSession(trading_date=day, open_ms=window.open_ms_utc, close_ms=window.close_ms_utc)


def expiry_date_of(expiry_ms: int) -> date:
    """The expiry date an anchor instant names. Raises ``NotAnExpiryAnchor`` for an instant that is not 16:00 New York."""
    try:
        return option_expiry_date(expiry_ms)
    except ValueError as exc:
        raise NotAnExpiryAnchor(str(exc)) from exc


def contract_from_symbol(symbol: str) -> OptionContract:
    """The contract an OCC symbol names. Raises ``NotAContract`` for anything else."""
    try:
        return OptionContract.from_occ_symbol(symbol)
    except ValueError as exc:
        raise NotAContract(str(exc)) from exc


# ---- What a read came to -----------------------------------------------------------------------


class ChainData(Protocol):
    """What the past chain reads: the run core's ``RunData``, whose listing also says how many stored rows it left out."""

    def underlying_day(self, trading_date: date) -> Captured[Sequence[MinuteBar]] | None: ...

    def listing(self, trading_date: date) -> Captured[StoredListing] | None: ...

    def option_day(self, contract: OptionContract, trading_date: date) -> Captured[Sequence[MinuteBar]] | None: ...


class ListingState(StrEnum):
    STORED = "stored"
    NOT_STORED = "not_stored"
    UNREADABLE = "unreadable"


class UnderlyingState(StrEnum):
    PRICED = "priced"
    # The day is stored and no bar had closed by the instant.
    NO_BAR_YET = "no_bar_yet"
    NOT_STORED = "not_stored"
    UNREADABLE = "unreadable"


class PriceState(StrEnum):
    TRADED = "traded"
    # The day is stored and holds no bar closing by the instant: it traded only later, or not at all.
    DID_NOT_TRADE = "did_not_trade"
    NOT_STORED = "not_stored"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class _ListingRead:
    state: ListingState
    listing: StoredListing | None
    problem: str | None


def unreadable_problem(subject: str, error: Exception) -> str:
    """Why ``subject`` cannot be read, in words that name no file, for the wire.

    The lake's errors name the file that went wrong by its path on the server's disk. Those words stay in the log;
    the wire gets what could not be read, a plain reason and the kind of error as its code.
    """
    logger.warning(
        "a stored file cannot be read",
        extra={"subject": subject, "error_type": type(error).__name__, "error": str(error)},
    )
    why = (
        "its bytes do not match the receipt the catalog holds for them"
        if isinstance(error, LakeAdmissionError)
        else "the stored file is damaged"
    )
    return f"{subject} cannot be read: {why} ({type(error).__name__})"


def _read_listing(data: ChainData, underlying: str, trading_date: date) -> _ListingRead:
    try:
        captured = data.listing(trading_date)
    except (LakeAdmissionError, OptionListingFileError) as exc:
        problem = unreadable_problem(f"the {underlying} listing for {trading_date.isoformat()}", exc)
        return _ListingRead(ListingState.UNREADABLE, None, problem)
    if captured is None:
        return _ListingRead(ListingState.NOT_STORED, None, None)
    return _ListingRead(ListingState.STORED, captured.value, None)


def _last_bar_at_or_before(bars: Sequence[MinuteBar], instant_ms: int) -> MinuteBar | None:
    return max((bar for bar in bars if bar.close_ms <= instant_ms), key=lambda bar: bar.close_ms, default=None)


def _dates_on_or_after(listing: StoredListing, trading_date: date) -> set[date]:
    """The distinct expiry dates the listing holds on or after the trading date."""
    dates = {option_expiry_date(ms) for ms in {listed.contract.expiry_ms for listed in listing.contracts}}
    return {expiry for expiry in dates if expiry >= trading_date}


# ---- The day: every expiry listed ---------------------------------------------------------------


@dataclass(frozen=True)
class ExpirySummary:
    expiry: date
    expiry_ms: int
    tag: ExpiryTag
    calls: int
    puts: int
    standard_calls: int
    standard_puts: int


@dataclass(frozen=True)
class ChainDay:
    underlying: str
    session: ChainSession
    listing: ListingState
    # The lake's own error, when the listing is stored and cannot be read.
    listing_problem: str | None
    # Ascending by date.
    expiries: tuple[ExpirySummary, ...]
    # Stored rows that name no contract the picker can use, or carry no share count: in no count above.
    rows_left_out_unusable: int
    # Listed contracts that expire before the trading date. A listing as of a date holds none; one that did is not tagged.
    rows_left_out_expired: int


def describe_day(data: ChainData, *, underlying: str, session: ChainSession) -> ChainDay:
    """Every expiry the listing of ``underlying`` holds on the session's date, with its tag and its counts."""
    read = _read_listing(data, underlying, session.trading_date)
    if read.listing is None:
        return ChainDay(underlying, session, read.state, read.problem, (), 0, 0)
    dates: dict[int, date] = {}
    counts: Counter[tuple[date, OptionRight, bool]] = Counter()
    expired = 0
    for listed in read.listing.contracts:
        expiry_ms = listed.contract.expiry_ms
        if expiry_ms not in dates:
            dates[expiry_ms] = option_expiry_date(expiry_ms)
        expiry = dates[expiry_ms]
        if expiry < session.trading_date:
            expired += 1
            continue
        counts[(expiry, listed.contract.right, listed.is_pickable)] += 1
    held = sorted({expiry for expiry, _, _ in counts})
    summaries = tuple(
        ExpirySummary(
            expiry=expiry,
            expiry_ms=option_expiry_anchor_ms(expiry),
            tag=tag_expiry(expiry, trading_date=session.trading_date, listed=held),
            calls=counts[(expiry, "call", True)] + counts[(expiry, "call", False)],
            puts=counts[(expiry, "put", True)] + counts[(expiry, "put", False)],
            standard_calls=counts[(expiry, "call", True)],
            standard_puts=counts[(expiry, "put", True)],
        )
        for expiry in held
    )
    return ChainDay(underlying, session, read.state, None, summaries, read.listing.rows_left_out, expired)


# ---- One expiry at one instant -------------------------------------------------------------------


@dataclass(frozen=True)
class UnderlyingQuote:
    state: UnderlyingState
    price: Decimal | None
    # The close of the bar the price is the close of.
    price_time_ms: int | None
    problem: str | None


@dataclass(frozen=True)
class ChainContract:
    contract: OptionContract
    # A standard contract of a hundred shares, which is what a backtest trades.
    standard: bool
    # Listed strikes above (positive) or below (negative) the money on the standard ladder; None for a contract off it.
    steps_from_the_money: int | None
    state: PriceState
    price: Decimal | None
    price_time_ms: int | None
    problem: str | None


@dataclass(frozen=True)
class ChainSide:
    right: OptionRight
    # Every contract of this right the listing holds at the expiry, and how many of them are standard.
    listed: int
    standard_listed: int
    # The money strike of the standard ladder, as ``OptionContract.strike`` says it and in whole thousandths.
    at_the_money_strike: Decimal | None
    at_the_money_strike_thousandths: int | None
    # Ascending by strike: the window around the money, and any non-standard contract listed inside its range.
    contracts: tuple[ChainContract, ...]


@dataclass(frozen=True)
class ChainExpiry:
    underlying: str
    session: ChainSession
    expiry: date
    at_ms: int
    strikes_each_side: int
    listing: ListingState
    listing_problem: str | None
    # None when the listing could not be read: nothing can be said of whether the expiry is in it.
    expiry_listed: bool | None
    tag: ExpiryTag | None
    underlying_quote: UnderlyingQuote
    calls: ChainSide
    puts: ChainSide


def _underlying_quote(data: ChainData, underlying: str, session: ChainSession, instant_ms: int) -> UnderlyingQuote:
    try:
        captured = data.underlying_day(session.trading_date)
    except (LakeAdmissionError, UnderlyingDayUnreadable) as exc:
        problem = unreadable_problem(f"the {underlying} minute bars for {session.trading_date.isoformat()}", exc)
        return UnderlyingQuote(UnderlyingState.UNREADABLE, None, None, problem)
    if captured is None:
        return UnderlyingQuote(UnderlyingState.NOT_STORED, None, None, None)
    bar = _last_bar_at_or_before(captured.value, instant_ms)
    if bar is None:
        return UnderlyingQuote(UnderlyingState.NO_BAR_YET, None, None, None)
    return UnderlyingQuote(UnderlyingState.PRICED, bar.close, bar.close_ms, None)


def _price_of(
    data: ChainData, listed: ListedContract, steps: int | None, trading_date: date, instant_ms: int
) -> ChainContract:
    contract = listed.contract

    def contract_at(
        state: PriceState, price: Decimal | None = None, time_ms: int | None = None, problem: str | None = None
    ) -> ChainContract:
        return ChainContract(
            contract=contract,
            standard=listed.is_pickable,
            steps_from_the_money=steps,
            state=state,
            price=price,
            price_time_ms=time_ms,
            problem=problem,
        )

    try:
        captured = data.option_day(contract, trading_date)
    except (LakeAdmissionError, OptionBarsFileError) as exc:
        problem = unreadable_problem(f"the bars of {contract.occ_symbol} for {trading_date.isoformat()}", exc)
        return contract_at(PriceState.UNREADABLE, problem=problem)
    if captured is None:
        return contract_at(PriceState.NOT_STORED)
    bar = _last_bar_at_or_before(captured.value, instant_ms)
    if bar is None:
        return contract_at(PriceState.DID_NOT_TRADE)
    return contract_at(PriceState.TRADED, bar.close, bar.close_ms)


@dataclass(frozen=True)
class _Window:
    """The contracts one side shows, chosen from the listing and the underlying's price alone, before any is priced."""

    right: OptionRight
    listed: int
    standard_listed: int
    # A standard contract at the money strike, which names the strike; None with no ladder or no price.
    at_the_money: OptionContract | None
    # Each contract shown, ascending by strike, with its steps from the money on the standard ladder (None off it).
    shown: tuple[tuple[ListedContract, int | None], ...]


def _window(
    *,
    underlying: str,
    right: OptionRight,
    on_expiry: Sequence[ListedContract],
    quote: UnderlyingQuote,
    strikes_each_side: int,
) -> _Window:
    of_right = [listed for listed in on_expiry if listed.contract.right == right]
    standard_listed = sum(1 for listed in of_right if listed.is_pickable)

    def on_the_ladder(listed: ListedContract) -> bool:
        # The picker's ladder: standard contracts of the underlying's own root. A contract off it neither is picked
        # nor moves a count of steps.
        return listed.is_pickable and listed.contract.root == underlying

    standard_at: dict[int, OptionContract] = {}
    for listed in of_right:
        if on_the_ladder(listed):
            standard_at.setdefault(listed.contract.strike_thousandths, listed.contract)
    ladder = sorted(standard_at)
    money_strike = (
        strike_for(AtTheMoney(kind="atm"), ladder_thousandths=ladder, underlying_price=quote.price)
        if ladder and quote.price is not None
        else None
    )
    if money_strike is None:
        return _Window(right, len(of_right), standard_listed, None, ())
    place = {strike: index for index, strike in enumerate(ladder)}
    money = place[money_strike]
    low = ladder[max(0, money - strikes_each_side)]
    high = ladder[min(len(ladder) - 1, money + strikes_each_side)]
    shown = sorted(
        (listed for listed in of_right if low <= listed.contract.strike_thousandths <= high),
        key=lambda listed: (listed.contract.strike_thousandths, listed.contract.occ_symbol),
    )
    return _Window(
        right,
        len(of_right),
        standard_listed,
        standard_at[money_strike],
        tuple(
            (listed, place[listed.contract.strike_thousandths] - money if on_the_ladder(listed) else None)
            for listed in shown
        ),
    )


def _priced(data: ChainData, window: _Window, trading_date: date, instant_ms: int) -> ChainSide:
    money = window.at_the_money
    return ChainSide(
        window.right,
        window.listed,
        window.standard_listed,
        None if money is None else money.strike,
        None if money is None else money.strike_thousandths,
        tuple(_price_of(data, listed, steps, trading_date, instant_ms) for listed, steps in window.shown),
    )


def describe_expiry(
    data: ChainData,
    *,
    underlying: str,
    session: ChainSession,
    expiry: date,
    at_ms: int | None = None,
    strikes_each_side: int = DEFAULT_STRIKES_EACH_SIDE,
) -> ChainExpiry:
    """One expiry as it stood at ``at_ms`` (the session's close when None): the money and a window of strikes.

    The trading date is the session's and the expiry is its own; neither is derived from the other. The instant is
    read as the close of a minute: the price shown is the last bar closing at or before it. Raises
    ``InstantOutsideSession`` for an instant outside ``[open, close]``, ``InstantNotOnAMinute`` for one that is not on
    a whole minute, ``ValueError`` for a window wider than ``MAX_STRIKES_EACH_SIDE`` or narrower than none, and
    ``WindowTooWide`` for a window that holds more than ``MAX_WINDOW_CONTRACTS`` contracts (before any is priced).
    """
    instant_ms = session.close_ms if at_ms is None else at_ms
    if not session.open_ms <= instant_ms <= session.close_ms:
        raise InstantOutsideSession(
            f"{instant_ms} is outside the {session.trading_date.isoformat()} session, which ran from "
            f"{session.open_ms} to {session.close_ms}"
        )
    if instant_ms % MS_PER_MINUTE:
        raise InstantNotOnAMinute(
            f"{instant_ms} is not on a whole minute: the chain is read at the close of a minute, so send an instant "
            f"that is a multiple of {MS_PER_MINUTE} ms"
        )
    if not 0 <= strikes_each_side <= MAX_STRIKES_EACH_SIDE:
        raise ValueError(f"a window is 0 to {MAX_STRIKES_EACH_SIDE} strikes each side; got {strikes_each_side}")
    read = _read_listing(data, underlying, session.trading_date)
    quote = _underlying_quote(data, underlying, session, instant_ms)
    listing = read.listing
    anchor = option_expiry_anchor_ms(expiry)
    # A listing as of a date holds no expiry before it; one that did is left out, as the day's counts leave it out.
    on_expiry = (
        [listed for listed in listing.contracts if listed.contract.expiry_ms == anchor]
        if listing is not None and expiry >= session.trading_date
        else []
    )
    windows = [
        _window(
            underlying=underlying, right=right, on_expiry=on_expiry, quote=quote, strikes_each_side=strikes_each_side
        )
        for right in _RIGHTS
    ]
    held = sum(len(window.shown) for window in windows)
    if held > MAX_WINDOW_CONTRACTS:
        raise WindowTooWide(
            f"{strikes_each_side} strikes each side of the {expiry.isoformat()} expiry hold {held} contracts, calls and "
            f"puts together, and a download takes at most {MAX_WINDOW_CONTRACTS}: ask for fewer strikes each side"
        )
    calls, puts = (_priced(data, window, session.trading_date, instant_ms) for window in windows)
    tag = (
        tag_expiry(expiry, trading_date=session.trading_date, listed=_dates_on_or_after(listing, session.trading_date))
        if on_expiry and listing is not None
        else None
    )
    return ChainExpiry(
        underlying=underlying,
        session=session,
        expiry=expiry,
        at_ms=instant_ms,
        strikes_each_side=strikes_each_side,
        listing=read.state,
        listing_problem=read.problem,
        expiry_listed=bool(on_expiry) if listing is not None else None,
        tag=tag,
        underlying_quote=quote,
        calls=calls,
        puts=puts,
    )
