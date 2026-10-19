"""IBKR observations for the canonical Options session rules.

Formula: rules read a completed TRADES minute's close; strike and expiry picks
    are ``options_backtest.picking``'s over admitted IBKR listing facts.
Reference: IBKR historical bars, https://interactivebrokers.github.io/tws-api/historical_bars.html,
    and ADR 0079. Quotes price execution only and never replace a trade close.
Canonical implementation: ``app/options_backtest/picking.py`` for selection;
    ``app/broker/ibkr/minute_assembler.py`` for vendor bar admission.
Validated against: tests/broker/ibkr/test_options_session_market.py.

Uses the existing read-only Gateway client and its supervisor's shared quote
lines. An explicit historical-data error, timeout or malformed bar is missing
observation evidence, never a minute with no trade. Only a successful completed
request can supply an empty minute. No Alpaca market data is requested.

Every bot on a lane shares one Gateway client, so they share its completed-minute
reads: IBKR answers historical requests one at a time and holds a burst for about
five seconds, so separate reads of one minute delay every bot that triggers on it.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence
from app.broker.ibkr.client import IbkrClient, get_client
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.market_subscription import DECISION_DATA_MAX_AGE_MS
from app.broker.ibkr.minute_assembler import _bar_time_ms, _validated_contribution
from app.broker.ibkr.option_chain import ListedContract, listed_contracts, listed_expiries
from app.broker.ibkr.option_quotes import WatchRequest
from app.options.contract import OptionContract
from app.options.expiry import option_expiry_anchor_ms, option_expiry_date
from app.options.quotes import HeldQuote, quote_fault
from app.options.resolver import FeedState
from app.options.watch import LineClass
from app.options_backtest.market import MinuteBar, Picked, PickRefused
from app.options_backtest.picking import ListedContract as ResearchListedContract
from app.options_backtest.picking import Listing, expiry_for, pick_contract
from app.options_backtest.setup import MS_PER_MINUTE, LegSetup, OptionsBacktestSetup
from app.schemas.action_plan import (
    AbsoluteExpiry,
    AbsoluteStrike,
    ActionPlan,
    CloseLegExit,
    OptionEntryLeg,
    OptionInstrument,
)
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services.option_watch import OptionWatchList, get_option_watch_list
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, datetime_at_ms, now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
    from app.options_bots.session import OpenLeg

_HISTORY_TIMEOUT_S = 12
_MAX_PARALLEL_HISTORY = 3


class OptionsMarketUnavailable(RuntimeError):
    """The requested minute cannot be established from available IBKR evidence."""

    code = "OPTIONS_MINUTE_UNAVAILABLE"


async def _history(client: IbkrClient, contract: Any, at_ms: int) -> MinuteBar | None:
    """Read exactly [t-60s,t), correlating errors and cancellation to this request.

    ib_async's default async convenience method folds request errors and its
    own timeout into an empty result. Use its existing request machinery with
    an explicit id, so a denied feed cannot masquerade as a no-trade minute.
    """
    from ib_async import BarDataList

    client.require_connected()
    ib = client.ib
    request_id = ib.client.getReqId()
    bars = BarDataList()
    bars.reqId, bars.contract = request_id, contract
    future = ib.wrapper.startReq(request_id, contract, container=bars)
    errors: list[tuple[int, str]] = []

    def failed(req_id: int, code: int, message: str, _contract: Any) -> None:
        if req_id == request_id:
            errors.append((code, message))

    ib.errorEvent += failed
    sent = False
    completed = False
    try:
        # This UTC string exists only at the provider's ingestion interface.
        end = datetime_at_ms(at_ms).strftime("%Y%m%d-%H:%M:%S")
        ib.client.reqHistoricalData(request_id, contract, end, "120 S", "1 min", "TRADES", True, 2, False, [])
        sent = True
        await asyncio.wait_for(future, timeout=_HISTORY_TIMEOUT_S)
        completed = True
        if errors:
            code, message = errors[0]
            raise OptionsMarketUnavailable(f"IBKR historical trade data was refused ({code}): {message}")
        found: MinuteBar | None = None
        previous: int | None = None
        for raw in bars:
            start = _bar_time_ms(raw)
            if start % MS_PER_MINUTE or (previous is not None and start <= previous):
                raise OptionsMarketUnavailable("IBKR returned unordered or unaligned option trade minutes")
            previous = start
            if start != at_ms - MS_PER_MINUTE:
                continue
            admitted = _validated_contribution(raw, symbol=str(contract.symbol), source_ms=start)
            if admitted.volume == 0:
                continue
            found = MinuteBar(at_ms, admitted.open, admitted.high, admitted.low, admitted.close)
        client.require_connected()
        return found
    except TimeoutError as exc:
        raise OptionsMarketUnavailable("IBKR did not finish the completed-minute TRADES request") from exc
    finally:
        ib.errorEvent -= failed
        if sent and not completed:
            ib.client.cancelHistoricalData(request_id)
        # Forget a cancelled waiter immediately; the wrapper safely ignores a
        # late historicalDataEnd after the explicit cancellation.
        if not completed:
            ib.wrapper._endReq(request_id)


_ReadKey = tuple[int, int, int, int]


def _observed(task: asyncio.Task[Any]) -> None:
    # Every waiting bot receives the read's exception; this only marks it seen
    # when all of them stopped waiting first.
    if not task.cancelled():
        task.exception()


class _LaneMinuteReads:
    """One Gateway client's completed-minute reads, shared by every bot on its lane.

    A read in flight is joined and a completed one reused, keyed by connection,
    data-loss epoch, contract and minute. A failure reaches every waiting bot but
    is never kept, so the next preparation asks IBKR again. A bot that stops
    waiting never cancels a read another bot still needs. At most three reads
    are open per lane; IBKR answers them one at a time anyway.
    """

    def __init__(self) -> None:
        self._limit = asyncio.Semaphore(_MAX_PARALLEL_HISTORY)
        self._completed: dict[_ReadKey, MinuteBar | None] = {}
        self._in_flight: dict[_ReadKey, asyncio.Task[MinuteBar | None]] = {}

    async def read(self, client: IbkrClient, contract: Any, at_ms: int) -> MinuteBar | None:
        key = (client.connection_generation, client.data_loss_epoch, int(contract.conId), at_ms)
        if key in self._completed:
            return self._completed[key]
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(client, contract, key))
            task.add_done_callback(_observed)
            self._in_flight[key] = task
        return await asyncio.shield(task)

    async def _fetch(self, client: IbkrClient, contract: Any, key: _ReadKey) -> MinuteBar | None:
        try:
            async with self._limit:
                result = await _history(client, contract, key[3])
        finally:
            del self._in_flight[key]
        oldest = key[3] - MS_PER_MINUTE
        self._completed = {cached: value for cached, value in self._completed.items() if cached[3] >= oldest}
        self._completed[key] = result
        return result


_LANE_READS: weakref.WeakKeyDictionary[Any, _LaneMinuteReads] = weakref.WeakKeyDictionary()


def _lane_reads(client: IbkrClient) -> _LaneMinuteReads:
    reads = _LANE_READS.get(client)
    if reads is None:
        reads = _LANE_READS[client] = _LaneMinuteReads()
    return reads


class IbkrOptionsSessionMarket:
    """Prefetch observations, then expose the existing synchronous SessionMarket.

    ``prepare`` publishes a complete frozen minute only after all its reads
    succeed. ``contracts`` are retained momentum picks; ``held_contracts`` are
    real custody or working orders and receive held-line priority. A later
    preparation never supplies an earlier or carried-forward price.
    """

    def __init__(
        self,
        setup: OptionsBacktestSetup,
        *,
        session_open_ms: int,
        session_close_ms: int,
        client: IbkrClient | None = None,
        watch: OptionWatchList | None = None,
        quotes: Callable[[], IbkrMarketStatusSource | None] | None = None,
        clock: Clock = now_ms_utc,
        watch_key: str | None = None,
    ) -> None:
        self.setup = setup
        self.session_open_ms, self.session_close_ms = session_open_ms, session_close_ms
        self._client = client
        self._watch = watch or get_option_watch_list()
        if quotes is None:
            from app.broker.alpaca.market_liveness import get_option_quotes

            quotes = get_option_quotes
        self._quotes = quotes
        self._clock = clock
        self._key = watch_key or f"options-session:{uuid4().hex}"
        self._watch_keys: set[str] = set()
        self._listed: dict[str, ListedContract] = {}
        self._listing_loaded = False
        self._stock: Any = None
        self._at_ms: int | None = None
        self._underlying: Decimal | None = None
        self._picks: dict[str, Picked | PickRefused] = {}
        self._bars: dict[str, MinuteBar | None] = {}

    @property
    def client(self) -> IbkrClient:
        if self._client is None:
            self._client = get_client()
        return self._client

    async def __aenter__(self) -> IbkrOptionsSessionMarket:
        self.client.require_connected()
        await self._load_listing()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        for key in self._watch_keys:
            self._watch.unwatch(key)
        self._watch_keys.clear()

    async def _load_listing(self) -> None:
        if self._listing_loaded:
            return
        self._stock = await asyncio.wait_for(qualify_underlying(self.client, self.setup.underlying), _HISTORY_TIMEOUT_S)
        dates = await listed_expiries(self.client, self.setup.underlying)
        day = et_date_at_ms(self.session_open_ms)
        loaded: set[int] = set()
        contracts: dict[str, ListedContract] = {}
        for leg in self.setup.legs:
            remaining = list(dates)
            while (expiry := expiry_for(leg.expiry, listed=remaining, trading_date=day)) is not None:
                anchor = option_expiry_anchor_ms(expiry)
                if anchor not in loaded:
                    contracts.update(await listed_contracts(self.client, self.setup.underlying, expiry))
                    loaded.add(anchor)
                if any(
                    item.listing.contract.expiry_ms == anchor
                    and item.listing.contract.right == leg.right
                    and item.listing.is_standard
                    for item in contracts.values()
                ):
                    break
                remaining.remove(expiry)
        self._listed, self._listing_loaded = contracts, True

    async def _read(self, contract: Any, at_ms: int) -> MinuteBar | None:
        return await _lane_reads(self.client).read(self.client, contract, at_ms)

    async def prepare(
        self,
        at_ms: int,
        *,
        contracts: Sequence[OptionContract] = (),
        held_contracts: Sequence[OptionContract] = (),
        include_candidates: bool = True,
    ) -> str | None:
        """Publish required contract bars; an optional candidate failure holds entries only."""
        if at_ms % MS_PER_MINUTE or not self.session_open_ms < at_ms <= self.session_close_ms or at_ms > self._clock():
            raise ValueError("prepare needs a completed minute of this calendar session")
        self.client.require_connected()
        generation = (self.client.connection_generation, self.client.data_loss_epoch)
        # Minute reads by instrument symbol: the underlying's, then OCC option symbols.
        reads: dict[str, asyncio.Task[MinuteBar | None]] = {}
        try:
            await self._load_listing()
            # Ask for the underlying first; contracts already known need no pick,
            # so their reads travel with it instead of after it.
            reads[self.setup.underlying] = asyncio.create_task(self._read(self._stock, at_ms))
            for contract in (*(contracts if include_candidates else ()), *held_contracts):
                listed = self._listed.get(contract.occ_symbol)
                if listed is not None and contract.occ_symbol not in reads:
                    reads[contract.occ_symbol] = asyncio.create_task(self._read(listed.ibkr_contract, at_ms))
            underlying_bar = await reads[self.setup.underlying]
            underlying = None if underlying_bar is None else underlying_bar.close
            day = et_date_at_ms(self.session_open_ms)
            listing = Listing(
                self.setup.underlying,
                day,
                tuple(
                    ResearchListedContract(item.listing.contract, item.listing.multiplier, item.listing.is_standard)
                    for item in self._listed.values()
                ),
                complete=True,
            )
            picks = (
                {
                    leg.leg_id: pick_contract(leg, listing=listing, trading_date=day, underlying_price=underlying)
                    for leg in self.setup.legs
                }
                if underlying is not None and include_candidates
                else {}
            )
            wanted = {
                contract.occ_symbol: contract
                for contract in (
                    *(contracts if include_candidates else ()),
                    *held_contracts,
                    *(pick.contract for pick in picks.values() if isinstance(pick, Picked)),
                )
            }
            missing = [contract for symbol, contract in wanted.items() if symbol not in self._listed]
            for contract in missing:
                self._listed.update(
                    await listed_contracts(self.client, self.setup.underlying, option_expiry_date(contract.expiry_ms))
                )
                if contract.occ_symbol not in self._listed:
                    raise OptionsMarketUnavailable(f"IBKR no longer lists selected contract {contract.occ_symbol}")
            self._ask_for_quotes(tuple(wanted.values()), held_contracts)
            required = {contract.occ_symbol for contract in (held_contracts or contracts)}
            for symbol in wanted:
                if symbol not in reads:
                    reads[symbol] = asyncio.create_task(self._read(self._listed[symbol].ibkr_contract, at_ms))
            values = await asyncio.gather(*(reads[symbol] for symbol in wanted), return_exceptions=True)
            bars: dict[str, MinuteBar | None] = {}
            candidate_hold = None
            for symbol, value in zip(wanted, values, strict=True):
                if isinstance(value, BaseException):
                    if not isinstance(value, Exception) or symbol in required or not held_contracts:
                        raise value
                    candidate_hold = f"New entries are held because candidate {symbol}'s trade minute is unavailable: {value}"
                else:
                    bars[symbol] = value
            if generation != (self.client.connection_generation, self.client.data_loss_epoch):
                raise OptionsMarketUnavailable("IBKR reconnected while this decision minute was being read")
            self._at_ms, self._underlying, self._picks, self._bars = (
                at_ms,
                underlying,
                picks,
                bars,
            )
            return candidate_hold
        except OptionsMarketUnavailable:
            raise
        except Exception as exc:
            raise OptionsMarketUnavailable(f"IBKR could not establish the completed options minute: {exc}") from exc
        finally:
            # Stop only this bot's waits; a lane read another bot needs continues.
            for task in reads.values():
                task.cancel()
            await asyncio.gather(*reads.values(), return_exceptions=True)

    def _ask_for_quotes(self, candidates: Sequence[OptionContract], held: Sequence[OptionContract]) -> None:
        keys: set[str] = set()
        for contract in candidates:
            key = f"{self._key}:{contract.occ_symbol}"
            keys.add(key)
            # A fixed single-contract plan asks the existing supervisor for
            # exactly this candidate at normal candidate priority.
            plan = ActionPlan(
                on_enter=[
                    OptionEntryLeg(
                        leg_id="quote",
                        instrument=OptionInstrument(kind="option", underlying=self.setup.underlying),
                        position="long",
                        qty_ratio=1,
                        right=contract.right,
                        strike=AbsoluteStrike(selector="absolute", strike=float(contract.strike)),
                        expiry=AbsoluteExpiry(selector="absolute", expiration_ms=contract.expiry_ms),
                    )
                ],
                on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="quote")],
            )
            self._watch.watch(key, WatchRequest(self.setup.underlying, LineClass.CANDIDATE, plan=plan))
        if held:
            key = f"{self._key}:held"
            keys.add(key)
            self._watch.watch(key, WatchRequest(self.setup.underlying, LineClass.CANDIDATE, held=tuple(held)))
        for key in self._watch_keys - keys:
            self._watch.unwatch(key)
        self._watch_keys = keys

    def watch_contracts(
        self, contracts: Sequence[OptionContract], *, held_contracts: Sequence[OptionContract] = ()
    ) -> None:
        """Keep actual custody quoted even while historical trade reads are unavailable."""
        self._ask_for_quotes(contracts, held_contracts)

    def _require_minute(self, at_ms: int) -> None:
        if self._at_ms != at_ms:
            raise OptionsMarketUnavailable("This decision minute has not been prepared from IBKR observations")

    def underlying_close(self, close_ms: int) -> Decimal | None:
        self._require_minute(close_ms)
        return self._underlying

    def pick(self, leg: LegSetup, *, at_ms: int, underlying_price: Decimal) -> Picked | PickRefused:
        self._require_minute(at_ms)
        if underlying_price != self._underlying or leg not in self.setup.legs:
            raise ValueError("a pick must use this minute's exact setup leg and underlying trade close")
        return self._picks[leg.leg_id]

    def option_bar(self, contract: OptionContract, close_ms: int) -> MinuteBar | None:
        self._require_minute(close_ms)
        if contract.occ_symbol not in self._bars:
            raise OptionsMarketUnavailable("The selected contract's trade minute was not prefetched")
        return self._bars[contract.occ_symbol]

    def quote_feed(self) -> FeedState | None:
        source = self._quotes()
        return None if source is None else source.option_feed()

    def quote(self, contract: OptionContract) -> HeldQuote | None:
        source = self._quotes()
        chain = None if source is None else source.option_chain(self.setup.underlying)
        watched = None if chain is None else chain.watched.get(contract.occ_symbol)
        return None if watched is None else watched.quote

    async def entry_evidence(
        self, decision: OpenLeg, terms: SealedOptionOrderTerms, *, proven_by_ms: int
    ) -> OptionEntryEvidence:
        remaining_s = min(3.0, (proven_by_ms - self._clock()) / 1_000)
        if remaining_s <= 0:
            raise OptionsMarketUnavailable("This entry's execution-evidence deadline has passed")
        deadline = asyncio.get_running_loop().time() + remaining_s
        while True:
            feed, quote = self.quote_feed(), self.quote(decision.contract)
            if (
                feed is not None
                and feed.unhealthy is None
                and quote is not None
                and quote_fault(
                    quote,
                    at_ms=self._clock(),
                    generation=feed.generation,
                    max_age_ms=DECISION_DATA_MAX_AGE_MS,
                    needs_a_bid=decision.side == "sell",
                )
                is None
            ):
                return OptionEntryEvidence(
                    picked={decision.leg_id: decision.contract},
                    multipliers={decision.leg_id: decision.multiplier},
                    quotes={decision.leg_id: quote},
                    generation=feed.generation,
                    rechecked_at_ms=self._clock(),
                    leg_gap_limit_percent=terms.gap_limit_percent,
                    cost_limit_percent=terms.cost_limit_percent,
                    proven_by_ms=proven_by_ms,
                    terms=terms.close,
                )
            if self._clock() >= proven_by_ms or asyncio.get_running_loop().time() >= deadline:
                raise OptionsMarketUnavailable("A fresh live execution quote has not arrived for the selected contract")
            await asyncio.sleep(0.1)
