"""Option quotes on a Clerk's IBKR connection: one line for each watched contract, evidence owned by its callbacks.

The option side of the supervisor that owns the underlying's subscriptions
(ADR 0067). That supervisor owns the connection, sees every reconnect and
drives this once a second. Nothing here opens a connection or keeps a timer,
and no route opens a line of its own.

For each contract with a line the newest bid and the newest ask are kept,
each with the time its callback delivered it, and the kind of data the
provider says it is. Reading a quote never renews a receipt. A tick of a
side's size is a receipt for that side: the price stands. A side IBKR
delivers as ``-1`` has arrived and has nothing there, so it is kept as zero:
nothing bid, or nothing offered.

The feed's generation is one number for the connection's epoch. It moves on
every reconnect and every recovery from a data loss, and nothing delivered
before it is kept. One contract's line being replaced empties that
contract's quote and does not move the generation.

Which contracts hold a line is ``app.options.watch.plan_lines``'s answer,
asked on every pass. When IBKR refuses a line because the account's
allowance is in use, the number of lines open at that moment becomes the
ceiling until the next reconnect, so the lowest classes give way and a held
contract is asked for again. The ceiling is tried one line higher once a
minute while something is waiting for capacity. A line IBKR refuses for
another reason stays refused until nothing wants it or the connection
changes; it is not asked for again and again. A line that delivers nothing
at all for the subscription timeout is asked for again; one that delivered
and went quiet is left alone.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import partial
from types import MappingProxyType
from typing import Any

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.market_subscription import SUBSCRIPTION_TIMEOUT_MS
from app.broker.ibkr.models import _coerce_optional_float
from app.broker.ibkr.option_chain import ListedContract, listed_contracts, listed_expiries, occ_root
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import Chain, WatchedContract
from app.options.watch import (
    LineClass,
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

# IBKR's tick ids: a side's price and its size, live and delayed.
_BID_TICKS = frozenset({0, 1, 66, 69})
_ASK_TICKS = frozenset({2, 3, 67, 70})
_DATA_TYPES = {
    1: QuoteDataType.LIVE,
    2: QuoteDataType.FROZEN,
    3: QuoteDataType.DELAYED,
    4: QuoteDataType.DELAYED_FROZEN,
}
# "Max number of tickers has been reached": the account's allowance of lines is in use.
_CAPACITY_CODE = 101
_REFUSAL_CODES = frozenset({200, 354, 10089, 10090, 10167, 10186})
_MAX_OPENS_PER_PASS = 20
_MAX_PARALLEL_LISTINGS = 2
_MAX_RETRY_MS = 30_000
CEILING_PROBE_MS = 60_000


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


def _side(price: Any, now: int) -> QuoteSide | None:
    if price is None or (isinstance(price, float) and math.isnan(price)):
        return None
    # IBKR's -1 says the side has nothing there now.
    return QuoteSide(Decimal(str(price)) if price > 0 else Decimal(0), now)


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
    last_received_at_ms: int | None = None
    # IBKR's own model delta, when it has sent one. Kept beside the quote, never used for a pick.
    vendor_delta: float | None = None

    def ingest(self, ticker: Any, now: int) -> None:
        """Accept one callback: a side is received when a tick of its price or its size arrives."""
        self.last_received_at_ms = now
        if self.market_data_type != ticker.marketDataType:
            # A quote is of one kind of data. A switch of kind leaves nothing received.
            self.bid = self.ask = None
            self.market_data_type = ticker.marketDataType
        for tick in ticker.ticks:
            if tick.tickType in _BID_TICKS:
                self.bid = _side(ticker.bid, now)
            elif tick.tickType in _ASK_TICKS:
                self.ask = _side(ticker.ask, now)
        greeks = getattr(ticker, "modelGreeks", None)
        self.vendor_delta = None if greeks is None else _coerce_optional_float(greeks.delta)

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
class _Definition:
    """What one underlying lists, as read on one New York date."""

    read_on: date
    expiries: tuple[date, ...] | None = None
    contracts: dict[date, dict[str, ListedContract]] = field(default_factory=dict)


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
        self._requests, self._budget, self._reach, self._clock = requests, budget, strikes_each_side, clock
        self._wanted: tuple[WatchRequest, ...] = ()
        self._generation = 0
        self._owner: IbkrClient | None = None
        self._lines: dict[str, OptionLine] = {}
        self._definitions: dict[str, _Definition] = {}
        self._loading: dict[tuple[str, date | None], asyncio.Task[None]] = {}
        self._listings = asyncio.Semaphore(_MAX_PARALLEL_LISTINGS)
        self._ceiling: int | None = None
        self._ceiling_set_at_ms = 0
        self._without: Mapping[str, NoLine] = MappingProxyType({})
        self._refused: dict[str, str] = {}
        # Where each underlying's windows were last centred, kept for when it has no price to centre them on.
        self._centres: dict[str, Decimal] = {}
        self._attempts: dict[object, int] = {}
        self._retry_at: dict[object, int] = {}

    # ---- what the underlying's supervisor calls -------------------------------

    def underlyings(self) -> tuple[str, ...]:
        """Read what is wanted now, and return the tickers whose own quotes must be watched for it."""
        self._wanted = tuple(self._requests())
        return tuple(dict.fromkeys(request.underlying.upper() for request in self._wanted))

    def adopt(self, client: IbkrClient, now: int, *, maintained: bool) -> None:
        """A new connection epoch: nothing delivered before it is kept.

        ``maintained`` is IBKR's recovery with its requests kept alive: the
        lines stay open, and each must deliver again before it holds a quote.
        """
        self._generation += 1
        self._ceiling = None
        self._refused.clear()
        self._attempts.clear()
        self._retry_at.clear()
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
        for task in self._loading.values():
            task.cancel()
        self._loading.clear()
        for symbol in tuple(self._lines):
            self._drop(symbol)
        self._owner = None

    async def reconcile(self, client: IbkrClient, now: int, money_of: Callable[[str], Decimal | None]) -> None:
        """Bring the open lines to what is wanted now. ``money_of`` is the underlying's live mid, when it has one."""
        today = et_date_at_ms(now)
        wants: list[LineWant] = []
        listed: dict[str, ListedContract] = {}
        # The (root, expiry) pairs whose contracts have been read: only those can be asked for, or found unlisted.
        read: set[tuple[str, date]] = set()
        for request in self._wanted:
            underlying = request.underlying.upper()
            definition = self._definition(underlying, client, today, now)
            if definition.expiries is None:
                continue
            needed = {contract.expiry_date for contract in request.held}
            if request.plan is not None:
                needed |= expiries_wanted(request.plan, definition.expiries, today)
            for expiry in sorted(needed - definition.contracts.keys()):
                self._load((underlying, expiry), partial(self._read_contracts, client, underlying, expiry), now)
            for expiry, contracts in definition.contracts.items():
                listed.update(contracts)
                read.add((occ_root(underlying), expiry))
            wants.extend(LineWant(contract, LineClass.HELD) for contract in request.held)
            money = request.money if request.money is not None else money_of(underlying)
            if money is None:
                # No bar close and no usable quote just now. A window stays where it was: it is not given
                # up, nor moved by a quote no decision could use.
                money = self._centres.get(underlying)
            if request.plan is None or money is None:
                continue
            self._centres[underlying] = money
            window = watched_window(
                request.plan,
                chain=self._chain(underlying, definition, watched={}),
                money=money,
                today=today,
                strikes_each_side=self._reach,
            )
            wants.extend(LineWant(seen.contract, request.line_class, seen.distance) for seen in window)

        wanted = {want.contract.occ_symbol for want in wants}
        for kept in (self._refused, self._attempts, self._retry_at):
            for key in [key for key in kept if isinstance(key, str) and key not in wanted]:
                del kept[key]
        watched_for = {request.underlying.upper() for request in self._wanted}
        for underlying in [underlying for underlying in self._centres if underlying not in watched_for]:
            del self._centres[underlying]

        for symbol, line in tuple(self._lines.items()):
            # A line that never delivered is broken and is asked for again. One that delivered and went quiet
            # is not: an option can go a long time with nothing new to say, and its quote ages by its own
            # receipts. Replacing it would turn a stale quote, which a search walks past, into one not yet
            # arrived, which stops it.
            if line.last_received_at_ms is None and now - line.opened_at_ms > SUBSCRIPTION_TIMEOUT_MS:
                self._drop(symbol)
                self._retry_at[symbol] = now + self._retry_delay(symbol)
                logger.warning(
                    "Asking again for an option line that delivered nothing",
                    extra={"action": "option_line_silent", "contract": symbol},
                )

        plan = plan_lines(wants, budget=self._budget, ceiling=self._ceiling, open_lines=self._lines.keys())
        self._without = plan.without
        opens = [
            contract
            for contract in plan.opens
            if (contract.root, contract.expiry_date) in read
            and contract.occ_symbol not in self._refused
            and now >= self._retry_at.get(contract.occ_symbol, 0)
        ][:_MAX_OPENS_PER_PASS]
        if not plan.opens_first:
            for symbol in plan.closes:
                self._drop(symbol)
        for contract in opens:
            self._open(contract, listed.get(contract.occ_symbol), client, now)
        if plan.opens_first:
            for symbol in plan.closes:
                self._drop(symbol)

        waiting = NoLine.NO_CAPACITY in plan.without.values()
        if self._ceiling is not None and waiting and now - self._ceiling_set_at_ms >= CEILING_PROBE_MS:
            # Another session may have given lines back. Ask for one more and see.
            self._ceiling += 1
            self._ceiling_set_at_ms = now
        if self._ceiling is not None and self._ceiling >= self._budget:
            self._ceiling = None

    def on_error(self, req_id: int, code: int) -> None:
        """IBKR's answer to a request for a line."""
        line = next((line for line in self._lines.values() if line.request_id == req_id), None)
        if line is None or code not in _REFUSAL_CODES | {_CAPACITY_CODE}:
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
        self._retry_at[symbol] = now + self._retry_delay(symbol)
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
        definition = self._definitions.get(underlying.upper())
        if definition is None or definition.expiries is None:
            return None
        root = occ_root(underlying)
        watched = {
            symbol: WatchedContract(line.held(self._generation), line.vendor_delta)
            for symbol, line in self._lines.items()
            if line.contract.root == root
        }
        return self._chain(underlying.upper(), definition, watched)

    def line_use(self) -> LineUse:
        without: dict[str, str] = {symbol: str(reason) for symbol, reason in self._without.items()}
        without.update(self._refused)
        return LineUse(
            budget=self._budget, open=len(self._lines), ceiling=self._ceiling, without=MappingProxyType(without)
        )

    # ---- inside ---------------------------------------------------------------

    @staticmethod
    def _chain(underlying: str, definition: _Definition, watched: Mapping[str, WatchedContract]) -> Chain:
        assert definition.expiries is not None
        return Chain(
            listings=tuple(
                listed.listing for contracts in definition.contracts.values() for listed in contracts.values()
            ),
            watched=MappingProxyType(dict(watched)),
            expiries=definition.expiries,
        )

    def _definition(self, underlying: str, client: IbkrClient, today: date, now: int) -> _Definition:
        definition = self._definitions.get(underlying)
        if definition is None or definition.read_on != today:
            # A listing is read once for the day: expiries and strikes are added overnight.
            definition = self._definitions[underlying] = _Definition(read_on=today)
        if definition.expiries is None:
            self._load((underlying, None), partial(self._read_expiries, client, underlying), now)
        return definition

    def _load(self, key: tuple[str, date | None], read: Callable[[], Awaitable[None]], now: int) -> None:
        """Start reading a listing off the supervisor's pass, unless it is being read or waiting to be retried."""
        if key not in self._loading and now >= self._retry_at.get(key, 0):
            self._loading[key] = asyncio.create_task(self._read(key, read))

    async def _read(self, key: tuple[str, date | None], read: Callable[[], Awaitable[None]]) -> None:
        try:
            async with self._listings:
                await read()
            self._attempts.pop(key, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._retry_at[key] = self._clock() + self._retry_delay(key)
            logger.exception(
                "Could not read an option listing from IBKR",
                extra={"action": "option_listing_failed", "symbol": key[0], "expiry": str(key[1])},
            )
        finally:
            # A reconnect may already have started this listing's next read under the same key.
            if self._loading.get(key) is asyncio.current_task():
                del self._loading[key]

    async def _read_expiries(self, client: IbkrClient, underlying: str) -> None:
        definition = self._definitions[underlying]
        expiries = await listed_expiries(client, underlying)
        if self._definitions.get(underlying) is definition:
            definition.expiries = expiries

    async def _read_contracts(self, client: IbkrClient, underlying: str, expiry: date) -> None:
        definition = self._definitions[underlying]
        contracts = await listed_contracts(client, underlying, expiry)
        if self._definitions.get(underlying) is definition:
            definition.contracts[expiry] = contracts

    def _retry_delay(self, key: object) -> int:
        attempt = self._attempts.get(key, 0)
        self._attempts[key] = attempt + 1
        return min(1_000 * 2 ** min(attempt, 5), _MAX_RETRY_MS)

    def _open(self, contract: OptionContract, listed: ListedContract | None, client: IbkrClient, now: int) -> None:
        symbol = contract.occ_symbol
        if listed is None:
            self._refused[symbol] = "IBKR does not list this contract in the underlying's own class"
            return
        # ib_async keys a market-data request by the contract object itself, so each line asks under its own.
        line = OptionLine(contract, now, ticker=client.ib.reqMktData(copy(listed.ibkr_contract), "", False, False))
        self._lines[symbol] = line
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
            self._attempts.pop(symbol, None)

        line.callback = receive
        line.ticker.updateEvent += receive
        request_id = client.ib.wrapper.ticker2ReqId["mktData"].get(line.ticker)
        line.request_id = request_id if isinstance(request_id, int) else None

    def _drop(self, symbol: str) -> None:
        line = self._lines.pop(symbol)
        line.ticker.updateEvent -= line.callback
        if self._owner is not None and self._owner.is_connected():
            self._owner.ib.cancelMktData(line.ticker.contract)
