"""Option quotes on a Clerk's IBKR connection: one line for each watched contract, evidence owned by its callbacks.

The option side of the supervisor that owns the underlying's subscriptions
(ADR 0067). That supervisor owns the connection, sees every reconnect and
drives this once a second. Nothing here opens a connection or keeps a timer,
and no route opens a line of its own.

For each contract with a line the newest bid and the newest ask are kept,
each with the time its callback delivered it, and the kind of data the
provider says it is. Reading a quote never renews a receipt.

A side is first held when a tick of its **price** arrives on this line, and
its price is that tick's own. The library keeps one cache of prices for a
contract across every request made for it, so a price it holds may be from
before a data loss or from a line given up long ago; nothing is read from
that cache. A later tick of the side's size is a receipt for the side,
because the price stands, and a size of nothing says the side has emptied. A
side IBKR delivers as ``-1`` has arrived and has nothing there, so it is kept
as zero: nothing bid, or nothing offered.

The feed's generation is one number for the connection's epoch. It moves on
every reconnect and every recovery from a data loss, and no quote from before
it is kept: after it each side waits for a price tick of its own. One
contract's line being replaced empties that contract's quote and does not
move the generation.

Which contracts hold a line is ``app.options.watch.plan_lines``'s answer,
asked on every pass. When IBKR refuses a line because the account's allowance
is in use, the number of lines open at that moment becomes the ceiling until
the next reconnect, so the lowest classes give way and a held contract is
asked for again. The ceiling is tried one line higher once a minute while
something is waiting for capacity. A line IBKR refuses for another reason
stays refused until nothing wants it or the connection changes; it is not
asked for again and again. A line that has not delivered a whole quote within
the subscription timeout of being opened is asked for again, which makes IBKR
send its current prices. One that delivered a whole quote and went quiet is
left alone.

The chain's listing is read once a New York day. A day's read replaces the
last only when it lands, so the listing in service, and the lines opened from
it, stay through the read. IBKR answers a listing request it cannot serve
with nothing, not with an error; nothing is a failed read, tried again, never
a chain with no contracts.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Hashable, Iterable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import partial
from types import MappingProxyType
from typing import Any

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.market_subscription import REQUEST_FAILURE_CODES, SUBSCRIPTION_TIMEOUT_MS, retry_delay_ms
from app.broker.ibkr.models import _coerce_optional_float
from app.broker.ibkr.option_chain import ListedContract, listed_contracts, listed_expiries, occ_root
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import Chain, WatchedContract
from app.options.watch import (
    LineClass,
    LinePlan,
    LineWant,
    NoLine,
    expiries_wanted,
    plan_lines,
    watched_window,
)
from app.schemas.action_plan import ActionPlan
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

# IBKR's tick ids for a side's price and for its size, live and delayed.
_BID_PRICE_TICKS, _BID_SIZE_TICKS = frozenset({1, 66}), frozenset({0, 69})
_ASK_PRICE_TICKS, _ASK_SIZE_TICKS = frozenset({2, 67}), frozenset({3, 70})
_DATA_TYPES = {
    1: QuoteDataType.LIVE,
    2: QuoteDataType.FROZEN,
    3: QuoteDataType.DELAYED,
    4: QuoteDataType.DELAYED_FROZEN,
}
# "Max number of tickers has been reached": the account's allowance of lines is in use.
_CAPACITY_CODE = 101
_MAX_OPENS_PER_PASS = 20
_MAX_PARALLEL_LISTINGS = 2
CEILING_PROBE_MS = 60_000
# A window centred on the underlying's live mid moves at most this often: a mid hovering between two strikes
# would otherwise open and give up the window's edges on every pass.
LIVE_CENTRE_HOLD_MS = 15_000

_NOTHING_THERE = Decimal(0)


@dataclass(frozen=True, slots=True)
class WatchRequest:
    """What one bot or page wants watched on one underlying."""

    # The ticker the underlying's own quote is asked for under.
    underlying: str
    # CANDIDATE for a running bot's window, PREVIEW for a page's.
    line_class: LineClass
    # Its option plan: a window is watched for it. None when it only holds contracts.
    plan: ActionPlan | None = None
    # The price its windows are centred on: the newest pricing bar's close. None: the underlying's live mid.
    money: Decimal | None = None
    # Contracts it holds or has a working order in. These keep a line whatever else gives way.
    held: tuple[OptionContract, ...] = ()


@dataclass(frozen=True, slots=True)
class LineUse:
    """How a Clerk's option lines stand. The count is this Clerk's own: it cannot see what other sessions
    under the same IBKR username use."""

    budget: int
    open: int
    # The number of lines IBKR last showed it would grant; None when it has refused none since the reconnect.
    ceiling: int | None
    # Wanted contracts with no line, by contract symbol, and why.
    without: Mapping[str, str]


def _priced(price: Any, now: int) -> QuoteSide | None:
    """A side as one price tick delivered it. IBKR's -1 says the side has nothing there now."""
    if price is None or (isinstance(price, float) and math.isnan(price)):
        return None
    return QuoteSide(Decimal(str(price)) if price > 0 else _NOTHING_THERE, now)


def _sized(held: QuoteSide | None, size: Any, now: int) -> QuoteSide | None:
    """A side after a tick of its size alone."""
    if not size:
        # A size of nothing says the side has emptied, whatever it held.
        return QuoteSide(_NOTHING_THERE, now)
    if held is None or held.price == _NOTHING_THERE:
        # Something is there now, and this line has not been told its price.
        return None
    return QuoteSide(held.price, now)


@dataclass
class OptionLine:
    """One contract's line: mutable ingestion state, written only by its own callback."""

    contract: OptionContract
    opened_at_ms: int
    ticker: Any = None
    callback: Any = None
    request_id: int | None = None
    bid: QuoteSide | None = None
    ask: QuoteSide | None = None
    market_data_type: int | None = None
    # IBKR's own model delta, when it has sent one on this line. Kept beside the quote, never used for a pick.
    vendor_delta: float | None = None
    # The model figures the library already held for the contract when this line opened: another line's.
    stale_greeks: Any = None

    def ingest(self, ticker: Any, now: int) -> None:
        """Accept one callback. Only the ticks it carries are read, never the library's cache of prices."""
        if self.market_data_type != ticker.marketDataType:
            # A quote is of one kind of data. A switch of kind leaves nothing received.
            self.bid = self.ask = None
            self.market_data_type = ticker.marketDataType
        for tick in ticker.ticks:
            if tick.tickType in _BID_PRICE_TICKS:
                self.bid = _priced(tick.price, now)
            elif tick.tickType in _ASK_PRICE_TICKS:
                self.ask = _priced(tick.price, now)
            elif tick.tickType in _BID_SIZE_TICKS:
                self.bid = _sized(self.bid, tick.size, now)
            elif tick.tickType in _ASK_SIZE_TICKS:
                self.ask = _sized(self.ask, tick.size, now)
        greeks = getattr(ticker, "modelGreeks", None)
        if greeks is not None and greeks is not self.stale_greeks:
            self.vendor_delta = _coerce_optional_float(greeks.delta)

    @property
    def has_a_whole_quote(self) -> bool:
        return self.bid is not None and self.ask is not None

    def held(self, generation: int) -> HeldQuote | None:
        """The quote as it was received, or None while no callback has arrived."""
        if self.market_data_type is None:
            return None
        return HeldQuote(
            bid=self.bid,
            ask=self.ask,
            # A kind IBKR has no name for is not live.
            data_type=_DATA_TYPES.get(self.market_data_type, QuoteDataType.DELAYED_FROZEN),
            generation=generation,
        )


@dataclass
class _Listing:
    """What one underlying lists, each part with the New York date it was read on."""

    expiries: tuple[date, ...] | None = None
    expiries_read_on: date | None = None
    contracts: dict[date, dict[str, ListedContract]] = field(default_factory=dict)
    contracts_read_on: dict[date, date] = field(default_factory=dict)


class _Backoff:
    """When something that failed may be tried again: after 1, 2, 4 seconds and so on, to the supervisor's cap."""

    def __init__(self) -> None:
        self._attempts: dict[Hashable, int] = {}
        self._retry_at: dict[Hashable, int] = {}

    def failed(self, key: Hashable, now: int) -> None:
        attempt = self._attempts.get(key, 0)
        self._attempts[key] = attempt + 1
        self._retry_at[key] = now + retry_delay_ms(attempt)

    def succeeded(self, key: Hashable) -> None:
        self._attempts.pop(key, None)
        self._retry_at.pop(key, None)

    def may_try(self, key: Hashable, now: int) -> bool:
        return now >= self._retry_at.get(key, 0)

    def keep_only(self, keys: Iterable[Hashable]) -> None:
        kept = set(keys)
        for known in (self._attempts, self._retry_at):
            for key in [key for key in known if key not in kept]:
                del known[key]

    def forget_all(self) -> None:
        self._attempts.clear()
        self._retry_at.clear()


class OptionQuoteLines:
    """One Clerk's option quote lines."""

    def __init__(
        self,
        *,
        requests: Callable[[], Sequence[WatchRequest]],
        budget: int,
        strikes_each_side: int,
        clock: Clock = now_ms_utc,
    ) -> None:
        if budget < 0:
            raise ValueError(f"an option line budget cannot be {budget}")
        if strikes_each_side < 0:
            raise ValueError(f"a window cannot reach {strikes_each_side} strikes either side")
        self._requests, self._budget, self._reach, self._clock = requests, budget, strikes_each_side, clock
        self._wanted: tuple[WatchRequest, ...] = ()
        self._generation = 0
        self._owner: IbkrClient | None = None
        self._lines: dict[str, OptionLine] = {}
        self._listings: dict[str, _Listing] = {}
        self._reading: dict[tuple[str, date | None], asyncio.Task[None]] = {}
        self._reads_at_once = asyncio.Semaphore(_MAX_PARALLEL_LISTINGS)
        self._read_backoff = _Backoff()
        self._line_backoff = _Backoff()
        self._ceiling: int | None = None
        self._ceiling_set_at_ms = 0
        # Why each wanted contract without a line has none, by contract symbol.
        self._without: dict[str, str] = {}
        self._refused: dict[str, str] = {}
        # Where each underlying's windows were last centred on its live mid, and when.
        self._live_centres: dict[str, tuple[Decimal, int]] = {}

    # ---- what the underlying's supervisor calls -------------------------------

    def underlyings(self) -> tuple[str, ...]:
        """Read what is wanted now, and return the tickers whose own quotes must be watched for it."""
        self._wanted = tuple(self._requests())
        return tuple(dict.fromkeys(request.underlying.upper() for request in self._wanted))

    def adopt(self, client: IbkrClient, now: int, *, maintained: bool) -> None:
        """A new connection epoch: no quote from before it is kept.

        ``maintained`` is IBKR's recovery with its requests kept alive: the
        lines stay open, and each side waits for a price tick of its own
        before it is held again.
        """
        self._generation += 1
        self._ceiling = None
        self._refused.clear()
        self._line_backoff.forget_all()
        self._read_backoff.forget_all()
        if maintained and client is self._owner:
            for symbol, line in tuple(self._lines.items()):
                line.ticker.updateEvent -= line.callback
                self._lines[symbol] = OptionLine(line.contract, now, ticker=line.ticker)
                self._attach(self._lines[symbol], client)
        else:
            self.release()
        self._owner = client

    def release(self) -> None:
        """Give every line up, and stop reading listings on a connection that is going."""
        for task in self._reading.values():
            task.cancel()
        self._reading.clear()
        for symbol in tuple(self._lines):
            self._drop(symbol)
        self._owner = None

    def reconcile(self, client: IbkrClient, now: int, money_of: Callable[[str], Decimal | None]) -> None:
        """Bring the open lines to what is wanted now. ``money_of`` is the underlying's live mid, when a
        decision could use one."""
        wants, listed, read = self._wants(client, now, money_of)
        wanted = {want.contract.occ_symbol for want in wants}
        self._line_backoff.keep_only(wanted)
        for symbol in [symbol for symbol in self._refused if symbol not in wanted]:
            del self._refused[symbol]
        self._ask_again_for_lines_with_no_quote(now)
        plan = plan_lines(wants, budget=self._budget, ceiling=self._ceiling, open_lines=self._lines.keys())
        self._apply(plan, listed, read, client, now)
        waiting_for_capacity = NoLine.NO_CAPACITY in plan.without.values()
        if self._ceiling is not None and waiting_for_capacity and now - self._ceiling_set_at_ms >= CEILING_PROBE_MS:
            # Another session may have given lines back. Ask for one more and see.
            self._ceiling += 1
            self._ceiling_set_at_ms = now
        if self._ceiling is not None and self._ceiling >= self._budget:
            self._ceiling = None

    def on_error(self, req_id: int, code: int) -> None:
        """IBKR's answer to a request for a line."""
        line = next((line for line in self._lines.values() if line.request_id == req_id), None)
        if line is None or code not in REQUEST_FAILURE_CODES:
            return
        symbol = line.contract.occ_symbol
        self._drop(symbol)
        if code != _CAPACITY_CODE:
            self._refused[symbol] = f"IBKR refused market data for this contract (code {code})"
            logger.warning(
                "IBKR refused an option line", extra={"action": "option_line_refused", "contract": symbol, "code": code}
            )
            return
        now = self._clock()
        self._ceiling, self._ceiling_set_at_ms = len(self._lines), now
        self._line_backoff.failed(symbol, now)
        logger.warning(
            "IBKR has no quote line left for an option contract",
            extra={"action": "option_line_no_capacity", "contract": symbol, "lines_open": len(self._lines)},
        )

    # ---- what a reader asks ---------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    def chain(self, underlying: str) -> Chain | None:
        """The underlying's chain as it is known now, with the quote held for each contract that has a line.
        None until its expiries are known."""
        listing = self._listings.get(underlying.upper())
        if listing is None or listing.expiries is None:
            return None
        root = occ_root(underlying)
        watched = {
            symbol: WatchedContract(line.held(self._generation), line.vendor_delta)
            for symbol, line in self._lines.items()
            if line.contract.root == root
        }
        return self._chain(listing, watched)

    def line_use(self) -> LineUse:
        return LineUse(
            budget=self._budget,
            open=len(self._lines),
            ceiling=self._ceiling,
            without=MappingProxyType(self._without | self._refused),
        )

    # ---- what is wanted -------------------------------------------------------

    def _wants(
        self, client: IbkrClient, now: int, money_of: Callable[[str], Decimal | None]
    ) -> tuple[list[LineWant], dict[str, ListedContract], set[tuple[str, date]]]:
        """Every contract a request wants a line for, the listed contracts they can be asked for under, and
        the (root, expiry) pairs whose contracts have been read: only those can be asked for, or found
        unlisted."""
        today = et_date_at_ms(now)
        wants: list[LineWant] = []
        listed: dict[str, ListedContract] = {}
        read: set[tuple[str, date]] = set()
        for request in self._wanted:
            underlying = request.underlying.upper()
            listing = self._listing(underlying, client, today, now)
            # A held contract is wanted whatever is known of the chain: its line is asked for when it can be.
            wants.extend(LineWant(contract, LineClass.HELD) for contract in request.held)
            needed = {contract.expiry_date for contract in request.held}
            if request.plan is not None and listing.expiries is not None:
                needed |= expiries_wanted(request.plan, listing.expiries, today)
            for expiry in sorted(needed):
                # A date gone by lists nothing any more; what was read of it stays as it was.
                if expiry >= today and listing.contracts_read_on.get(expiry) != today:
                    self._read(
                        (underlying, expiry), partial(self._read_contracts, client, underlying, expiry, today), now
                    )
            for expiry, contracts in listing.contracts.items():
                listed.update(contracts)
                read.add((occ_root(underlying), expiry))
            if request.plan is None or listing.expiries is None:
                continue
            centre = self._centre(request, underlying, money_of, now)
            if centre is None:
                continue
            window = watched_window(
                request.plan,
                chain=self._chain(listing, watched={}),
                money=centre,
                today=today,
                strikes_each_side=self._reach,
            )
            wants.extend(LineWant(seen.contract, request.line_class, seen.distance) for seen in window)
        watched_for = {request.underlying.upper() for request in self._wanted}
        for underlying in [underlying for underlying in self._live_centres if underlying not in watched_for]:
            del self._live_centres[underlying]
        return wants, listed, read

    def _centre(
        self, request: WatchRequest, underlying: str, money_of: Callable[[str], Decimal | None], now: int
    ) -> Decimal | None:
        """The price a request's windows are centred on: its own bar close, or the underlying's live mid."""
        if request.money is not None:
            return request.money
        kept = self._live_centres.get(underlying)
        if kept is None or now - kept[1] >= LIVE_CENTRE_HOLD_MS:
            mid = money_of(underlying)
            if mid is not None:
                kept = self._live_centres[underlying] = (mid, now)
        # With no usable quote just now a window stays where it was: not given up, and not moved by a quote
        # no decision could use.
        return None if kept is None else kept[0]

    @staticmethod
    def _chain(listing: _Listing, watched: Mapping[str, WatchedContract]) -> Chain:
        assert listing.expiries is not None
        return Chain(
            listings=tuple(listed.listing for contracts in listing.contracts.values() for listed in contracts.values()),
            watched=MappingProxyType(dict(watched)),
            expiries=listing.expiries,
        )

    # ---- the listing ----------------------------------------------------------

    def _listing(self, underlying: str, client: IbkrClient, today: date, now: int) -> _Listing:
        listing = self._listings.setdefault(underlying, _Listing())
        if listing.expiries_read_on != today:
            # Read once for the day: expiries and strikes are added overnight. Yesterday's stays in service
            # until today's lands.
            self._read((underlying, None), partial(self._read_expiries, client, underlying, today), now)
        return listing

    def _read(self, key: tuple[str, date | None], read: Callable[[], Awaitable[None]], now: int) -> None:
        """Start reading a listing off the supervisor's pass, unless it is being read or waiting to be retried."""
        if key not in self._reading and self._read_backoff.may_try(key, now):
            self._reading[key] = asyncio.create_task(self._reading_of(key, read))

    async def _reading_of(self, key: tuple[str, date | None], read: Callable[[], Awaitable[None]]) -> None:
        try:
            async with self._reads_at_once:
                await read()
            self._read_backoff.succeeded(key)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._read_backoff.failed(key, self._clock())
            logger.exception(
                "Could not read an option listing from IBKR",
                extra={"action": "option_listing_failed", "symbol": key[0], "expiry": str(key[1])},
            )
        finally:
            # A reconnect may already have started this listing's next read under the same key.
            if self._reading.get(key) is asyncio.current_task():
                del self._reading[key]

    async def _read_expiries(self, client: IbkrClient, underlying: str, today: date) -> None:
        expiries = await listed_expiries(client, underlying)
        listing = self._listings[underlying]
        listing.expiries, listing.expiries_read_on = expiries, today
        for expired in [expiry for expiry in listing.contracts if expiry < today]:
            del listing.contracts[expired]
            listing.contracts_read_on.pop(expired, None)

    async def _read_contracts(self, client: IbkrClient, underlying: str, expiry: date, today: date) -> None:
        contracts = await listed_contracts(client, underlying, expiry)
        listing = self._listings[underlying]
        listing.contracts[expiry] = contracts
        listing.contracts_read_on[expiry] = today

    # ---- the lines ------------------------------------------------------------

    def _ask_again_for_lines_with_no_quote(self, now: int) -> None:
        """A line that has not delivered a whole quote within the timeout of being opened is given up, to be
        asked for again: IBKR answers a new request with its current prices. One that delivered a whole
        quote and went quiet is left alone. An option can go a long time with nothing new to say, and
        replacing its line would turn a stale quote, which a search walks past, into one not yet arrived,
        which stops it."""
        for symbol, line in tuple(self._lines.items()):
            if not line.has_a_whole_quote and now - line.opened_at_ms > SUBSCRIPTION_TIMEOUT_MS:
                self._drop(symbol)
                self._line_backoff.failed(symbol, now)
                logger.warning(
                    "Asking again for an option line that delivered no whole quote",
                    extra={"action": "option_line_silent", "contract": symbol},
                )

    def _apply(
        self,
        plan: LinePlan,
        listed: Mapping[str, ListedContract],
        read: set[tuple[str, date]],
        client: IbkrClient,
        now: int,
    ) -> None:
        """Give up the lines the plan drops, then ask for the ones it adds, highest priority first."""
        for symbol in plan.closes:
            self._drop(symbol)
        without: dict[str, str] = {symbol: str(reason) for symbol, reason in plan.without.items()}
        asked = 0
        for contract in plan.opens:
            symbol = contract.occ_symbol
            if symbol in self._refused:
                continue
            if (contract.root, contract.expiry_date) not in read:
                without[symbol] = str(NoLine.LISTING_NOT_READ)
            elif symbol not in listed:
                self._refused[symbol] = "IBKR does not list this contract in the underlying's own class"
            elif asked >= _MAX_OPENS_PER_PASS or not self._line_backoff.may_try(symbol, now):
                without[symbol] = str(NoLine.WAITING_TO_BE_ASKED_FOR)
            else:
                self._open(contract, listed[symbol], client, now)
                asked += 1
        self._without = without

    def _open(self, contract: OptionContract, listed: ListedContract, client: IbkrClient, now: int) -> None:
        ticker = client.ib.reqMktData(copy(listed.ibkr_contract), "", False, False)
        line = OptionLine(contract, now, ticker=ticker, stale_greeks=getattr(ticker, "modelGreeks", None))
        self._lines[contract.occ_symbol] = line
        self._attach(line, client)

    def _attach(self, line: OptionLine, client: IbkrClient) -> None:
        generation, symbol = self._generation, line.contract.occ_symbol

        def receive(ticker: Any) -> None:
            if (
                self._lines.get(symbol) is not line
                or self._generation != generation
                or client.connection_state != "connected"
            ):
                return
            line.ingest(ticker, self._clock())
            if line.has_a_whole_quote:
                self._line_backoff.succeeded(symbol)

        line.callback = receive
        line.ticker.updateEvent += receive
        line.request_id = self._request_id(client, line)

    @staticmethod
    def _request_id(client: IbkrClient, line: OptionLine) -> int | None:
        request_id = client.ib.wrapper.ticker2ReqId["mktData"].get(line.ticker)
        return request_id if isinstance(request_id, int) else None

    def _drop(self, symbol: str) -> None:
        line = self._lines.pop(symbol)
        line.ticker.updateEvent -= line.callback
        owner = self._owner
        # After a reconnect the library has forgotten the request; there is nothing left to cancel.
        if owner is not None and owner.is_connected() and self._request_id(owner, line) is not None:
            owner.ib.cancelMktData(line.ticker.contract)
