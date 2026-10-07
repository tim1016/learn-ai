"""What an option bot's runner does beside a stock bot's: its quote demand, its entry and its exit (ADR 0076).

A bot whose plan trades option contracts decides as every bot does, on its
Signal stream's bars. What differs is what an ENTER and an EXIT are handed to
the Clerk with. This module works that out; ``bot_trade_strategy`` calls it
from the Dry Run's loop, and a bot in shares never reaches it.

**Demand, from the start of the run.** Quotes are held only for contracts
somebody asked to have watched, and a window asked for at the decision has no
quotes yet. So the run asks at once, under its own key on the process's watch
list, for the window its plan's rules can pick from, and moves that window
with every closed bar of the underlying. The contract it comes to hold is not
its to ask for: that is the Clerk's own demand, read from its books
(``held_option_quotes``), so the line outlives this run.

**An ENTER** is resolved here and judged by the Clerk. After the runner's
own screens, in order:

1. the Clerk's IBKR supervisor is read on the event loop: its feed's state,
   the underlying's chain and the underlying's own quote;
2. the rate and the declared dividends are read off the loop;
3. the supervisor is read again and the contracts are searched for, in the
   entry lane's worker process (``option_search``), never in this one;
4. the picks' quotes are tested again at that moment (``recheck``), on a
   third read.

Each await is given no longer than the decision has left, and a decision
whose time ran out on the way is refused as late. The runner then judges
lateness once more, and hands the Clerk the facts: the contracts, their
listed multipliers and the recheck's quotes. It hands over no price.

**A refusal at any step is one answer**: a reason code and a sentence. The
runner writes it as the decision's one ``blocked`` receipt and discards the
staged decision. Nothing is kept for a later bar.

**An EXIT** closes what the Clerk says the bot holds. The plan's rules are
never asked again. The newest live quote the supervisor holds for that
contract goes with it; with none, the EXIT goes without, and the Clerk prices
it from the newest quote kept.

**The contract reference in a Dry Run** is IBKR's own listing: the multiplier
handed to the Clerk is the one the chain lists the contract with. A real
account asks its broker's reference before any order; a simulation has none.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from app.broker.alpaca.clerk.option_effects import (
    STANDARD_CONTRACT_MULTIPLIER,
    OptionEffectRefusal,
    OptionEntryEvidence,
    OptionExitEvidence,
    OptionPlanRefused,
)
from app.broker.ibkr.market_subscription import DECISION_DATA_MAX_AGE_MS
from app.broker.ibkr.option_quotes import WatchRequest
from app.marketdata.feed import ContinuityPolicy, MarketDataBar, MarketDataFeed
from app.options.deadlines import CloseTerms
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, DEFAULT_LEG_GAP_LIMIT_PERCENT
from app.options.quotes import HeldQuote, QuoteDataType, receipt_times
from app.options.resolver import (
    Chain,
    ContractsNotResolved,
    FeedState,
    ModelInputs,
    ResolverLimits,
    ResolverRefusal,
    UnderlyingObservation,
    recheck,
)
from app.options.watch import LineClass, expiries_wanted, lines_a_plan_can_need
from app.schemas.action_plan import ActionPlan, DeltaStrike, OptionEntryLeg, traded_instrument
from app.services.feed_continuity_policy import DECISION_LATE_REASON_CODE
from app.services.option_model_inputs import ModelInputsRead, off_the_loop, read_option_model_inputs
from app.services.option_search import OptionSearchLane, SearchNotRun, get_option_searches
from app.services.option_watch import OptionWatchList, get_option_watch_list
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import Clock, now_ms_utc

if TYPE_CHECKING:
    from app.broker.ibkr.market_liveness import IbkrMarketStatusSource
    from app.services.bot_runner import BrokerBotBinding

logger = logging.getLogger(__name__)

# Where a Dry Run's entry takes a contract's multiplier from: the chain IBKR lists.
IBKR_LISTING: Final = "ibkr_listing"

_NOT_QUEUED: Final = "No order was sent, and the entry is not kept for a later bar."


class OptionEntryRefusal(StrEnum):
    """Why the runner sent no option entry, where no resolver, search or Clerk code already says it."""

    # No Clerk with an IBKR option quote source runs in this process.
    NO_QUOTE_SOURCE = "OPTION_ENTRY_NO_QUOTE_SOURCE"
    # IBKR's list of the underlying's option contracts has not been read yet.
    LISTING_NOT_READ = "OPTION_ENTRY_LISTING_NOT_READ"
    # A leg is picked by delta, and the dividends the model needs are not known.
    DIVIDENDS_UNKNOWN = "OPTION_ENTRY_DIVIDENDS_UNKNOWN"
    # The rate and the dividends were not read while the decision was still on time.
    PRICING_INPUTS_LATE = "OPTION_ENTRY_PRICING_INPUTS_LATE"
    # The search had not answered while the decision was still on time.
    SEARCH_LATE = "OPTION_ENTRY_SEARCH_LATE"


@dataclass(frozen=True, slots=True)
class OptionEntryBlocked:
    """An option entry that is not sent: its reason code, and why in plain words."""

    reason_code: str
    why: str


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionOrderTerms:
    """What the owner fixes for an option bot at Deploy, for the bot's life."""

    # The widest bid-ask gap any one leg may have at an entry, as a percent of its mid.
    leg_gap_limit_percent: Decimal = DEFAULT_LEG_GAP_LIMIT_PERCENT
    # The most opening and closing at once may cost, as a percent of the entry commitment.
    cost_limit_percent: Decimal = DEFAULT_COST_LIMIT_PERCENT
    close: CloseTerms = field(default_factory=CloseTerms)


def option_order_terms(binding: BrokerBotBinding) -> OptionOrderTerms:
    """The order terms one option bot runs under: the one place its runner reads them.

    They are a fact of the deployment, never of a decision. Deploy does not
    store them on the binding yet, so every option bot runs under the terms a
    Deploy starts from; when it does, this reads them there and nothing else
    changes.
    """
    del binding
    return OptionOrderTerms()


# (underlying, the moment, the latest expiry the plan comes to) -> the rate and the dividends, with sources.
ModelInputsReader = Callable[[str, int, date], ModelInputsRead]


@dataclass(frozen=True, slots=True, kw_only=True)
class OptionMarket:
    """What an option bot's runner reads its market through, in the Clerk's own process."""

    # The Clerk's IBKR supervisor, when one runs here.
    quotes: Callable[[], IbkrMarketStatusSource | None]
    # The list that supervisor keeps its quote lines to.
    watch: OptionWatchList
    # Where an entry's search runs: a worker process, never this one.
    entry_searches: Callable[[], OptionSearchLane]
    model_inputs: ModelInputsReader
    # The Clerk's own allowance of option quote lines, and how far either side of its aim a leg's window reaches.
    line_budget: int
    strikes_each_side: int
    clock: Clock = now_ms_utc


def _read_model_inputs(underlying: str, at_ms: int, latest_expiry: date) -> ModelInputsRead:
    return read_option_model_inputs(underlying, at_ms=at_ms, latest_expiry=latest_expiry)


def this_process_option_market() -> OptionMarket:
    """The option market of the Clerk running in this process, as its settings and its supervisor have it."""
    from app.broker.alpaca.market_liveness import get_option_quotes
    from app.broker.ibkr.config import get_settings

    settings = get_settings()
    return OptionMarket(
        quotes=get_option_quotes,
        watch=get_option_watch_list(),
        entry_searches=lambda: get_option_searches().entry,
        model_inputs=_read_model_inputs,
        line_budget=settings.option_line_budget,
        strikes_each_side=settings.option_window_strikes_each_side,
    )


class _UnderlyingBars:
    """A feed exactly as it is, which also shows each bar of one symbol to a listener as it is delivered.

    Everything but ``stream_bars`` is the wrapped feed's own, whatever that
    feed happens to offer: the run's feeds are wrapped in one another, and
    each is asked for things the ``MarketDataFeed`` protocol does not name.
    """

    def __init__(self, source: MarketDataFeed, *, symbol: str, on_bar: Callable[[MarketDataBar], None]) -> None:
        self._source, self._symbol, self._on_bar = source, symbol.upper(), on_bar

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    async def stream_bars(
        self, symbol: str, *, use_rth: bool = True, continuity: ContinuityPolicy | None = None
    ) -> AsyncIterator[MarketDataBar]:
        async for bar in self._source.stream_bars(symbol, use_rth=use_rth, continuity=continuity):
            if bar.symbol.upper() == self._symbol:
                self._on_bar(bar)
            yield bar


@dataclass(frozen=True, slots=True)
class _Reading:
    """What the supervisor held at one moment, once it is enough to resolve against."""

    feed: FeedState
    chain: Chain
    underlying_quote: HeldQuote | None


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] + ("" if text.endswith((".", "?", "!")) else ".")


def _blocked(reason: object, why: str) -> OptionEntryBlocked:
    return OptionEntryBlocked(str(reason), f"{_sentence(why)} {_NOT_QUEUED}")


class OptionRun:
    """One run of one option bot: what it asks to have watched, and what its decisions are handed over with."""

    def __init__(
        self, binding: BrokerBotBinding, market: OptionMarket, *, held_contracts: Callable[[], tuple[str, ...]]
    ) -> None:
        self._market = market
        self._plan: ActionPlan = binding.action_plan
        self._underlying = traded_instrument(binding.action_plan).underlying.upper()
        self._strategy_instance_id, self._run_id = binding.strategy_instance_id, binding.run_id
        self._key = f"bot:{binding.strategy_instance_id}"
        self._terms = option_order_terms(binding)
        # The Clerk's answer to which contracts this bot holds now, by OCC symbol.
        self._held_contracts = held_contracts
        self._picks_by_delta = any(
            isinstance(leg, OptionEntryLeg) and isinstance(leg.strike, DeltaStrike) for leg in self._plan.on_enter
        )
        self._warming: asyncio.Future[ModelInputsRead] | None = None

    @property
    def underlying(self) -> str:
        return self._underlying

    @property
    def terms(self) -> OptionOrderTerms:
        return self._terms

    async def __aenter__(self) -> OptionRun:
        self._require_the_window_fits()
        self._watch(money=None)
        # The search's worker loads while the first bars arrive.
        self._market.entry_searches().warm()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._market.watch.unwatch(self._key)

    def _require_the_window_fits(self) -> None:
        """Refuse a bot whose watched window can need more quote lines than the Clerk may open: every entry
        of such a bot would be refused as a search that could not be finished."""
        needed = lines_a_plan_can_need(self._plan, strikes_each_side=self._market.strikes_each_side)
        if needed > self._market.line_budget:
            raise OptionPlanRefused(
                OptionEffectRefusal.WINDOW_OVER_LINE_BUDGET,
                f"This bot's option plan can need {needed} IBKR quote lines, and this Clerk's budget of option "
                f"quote lines is {self._market.line_budget}. Its contracts could not all be quoted, so every "
                "entry would be refused. Raise this Clerk's IBKR_OPTION_LINE_BUDGET, or deploy a narrower "
                "structure.",
            )

    # ---- what is watched --------------------------------------------------------

    def _watch(self, *, money: Decimal | None) -> None:
        self._market.watch.watch(
            self._key,
            WatchRequest(underlying=self._underlying, line_class=LineClass.CANDIDATE, plan=self._plan, money=money),
        )

    def tapping(self, feed: MarketDataFeed) -> MarketDataFeed:
        """``feed``, with each closed bar of the underlying it delivers moving this run's watched window."""
        return _UnderlyingBars(feed, symbol=self._underlying, on_bar=self._bar_closed)  # type: ignore[return-value]

    def _bar_closed(self, bar: MarketDataBar) -> None:
        if bar.volume > 0:
            # A minute with no trade has no price to centre a window on: it stays where it was.
            self._watch(money=bar.close)
        self._keep_the_model_inputs_read(bar.end_ms)

    def _keep_the_model_inputs_read(self, at_ms: int) -> None:
        """Read the rate and the dividends again in the background, so a decision finds them kept and does
        not spend its own time on a vendor. What is kept is answered at once; nothing waits on this."""
        if self._warming is not None and not self._warming.done():
            return
        source = self._market.quotes()
        chain = None if source is None else source.option_chain(self._underlying)
        if chain is None:
            return
        latest_expiry = self._latest_expiry(chain, at_ms)
        self._warming = off_the_loop(partial(self._market.model_inputs, self._underlying, at_ms, latest_expiry))
        self._warming.add_done_callback(self._warmed)

    def _warmed(self, read: asyncio.Future[ModelInputsRead]) -> None:
        if read.cancelled():
            return
        failed = read.exception()
        if failed is not None:
            logger.warning(
                "An option bot's rate and dividends could not be read ahead of its next decision",
                extra={"action": "option_model_inputs_not_warmed", "strategy_instance_id": self._strategy_instance_id},
                exc_info=failed,
            )

    def _latest_expiry(self, chain: Chain, at_ms: int) -> date:
        """The latest expiry the plan's legs come to on the day of ``at_ms``: how far the rate and the
        dividends must reach."""
        today = et_date_at_ms(at_ms)
        named = chain.expiry_dates
        listed = (
            named
            if named is not None
            else tuple(sorted({listing.contract.expiry_date for listing in chain.listings if listing.is_standard}))
        )
        return max(expiries_wanted(self._plan, listed, today), default=today)

    # ---- an ENTER ---------------------------------------------------------------

    def _read(self) -> _Reading | OptionEntryBlocked:
        """Read the supervisor once, on the event loop where its callbacks write."""
        source = self._market.quotes()
        feed = None if source is None else source.option_feed()
        if source is None or feed is None:
            return _blocked(
                OptionEntryRefusal.NO_QUOTE_SOURCE, "This process has no IBKR quote source for option contracts"
            )
        if feed.unhealthy is not None:
            return _blocked(ResolverRefusal.FEED_UNHEALTHY, f"The option quote feed is not healthy: {feed.unhealthy}")
        chain = source.option_chain(self._underlying)
        if chain is None:
            return _blocked(
                OptionEntryRefusal.LISTING_NOT_READ,
                f"IBKR's list of {self._underlying} option contracts has not been read yet",
            )
        return _Reading(feed, chain, source.underlying_quote(self._underlying))

    async def _model(
        self, reading: _Reading, *, decision_bar_close_ms: int, time_left_ms: Callable[[], int]
    ) -> ModelInputs | OptionEntryBlocked:
        """The pricing model's inputs for this decision, read off the event loop and waited for no longer than
        the decision has left. A read that outlives the wait goes on, and is kept for the next decision."""
        latest_expiry = self._latest_expiry(reading.chain, decision_bar_close_ms)
        reading_them = off_the_loop(
            partial(self._market.model_inputs, self._underlying, self._market.clock(), latest_expiry)
        )
        try:
            inputs = await asyncio.wait_for(reading_them, timeout=time_left_ms() / 1000)
        except TimeoutError:
            return _blocked(
                OptionEntryRefusal.PRICING_INPUTS_LATE,
                f"The rate and the dividends for {self._underlying} had not been read while this decision was "
                "still on time",
            )
        model = inputs.model
        if model is not None:
            return model
        if self._picks_by_delta:
            return _blocked(
                OptionEntryRefusal.DIVIDENDS_UNKNOWN,
                f"The dividends {self._underlying} pays before expiry are not known, and a leg of this plan is "
                f"picked by delta, which needs them. Nothing is picked on a guess. {inputs.dividends_source}",
            )
        # No rule here picks by delta, so no pick depends on the model.
        return inputs.unverified_model()

    async def entry_evidence(
        self, *, money: Decimal | None, decision_bar_close_ms: int, time_left_ms: Callable[[], int]
    ) -> OptionEntryEvidence | OptionEntryBlocked:
        """What this run hands the Clerk with an ENTER decided on the bar that closed at
        ``decision_bar_close_ms``, or why no entry is sent.

        ``money`` is the close of the underlying's own bar for that minute;
        None when the underlying did not trade in it. ``time_left_ms`` says
        how much longer the decision stays on time, asked at each wait.
        """
        first = self._read()
        if isinstance(first, OptionEntryBlocked):
            return first
        model = await self._model(first, decision_bar_close_ms=decision_bar_close_ms, time_left_ms=time_left_ms)
        if isinstance(model, OptionEntryBlocked):
            return model
        if time_left_ms() <= 0:
            # Said as what it is. A search given no time would only be told it was late.
            return _blocked(
                DECISION_LATE_REASON_CODE,
                f"This decision's time ran out while the rate and the dividends for {self._underlying} were read",
            )
        # The read of the rate and dividends took time: a search is handed quotes as they stand now.
        selected = self._read()
        if isinstance(selected, OptionEntryBlocked):
            return selected
        searching = self._market.entry_searches().resolve(
            self._plan,
            chain=selected.chain,
            underlying=UnderlyingObservation(pricing_bar_close=money, quote=selected.underlying_quote),
            feed=selected.feed,
            model=model,
            limits=ResolverLimits(
                max_quote_age_ms=DECISION_DATA_MAX_AGE_MS,
                entry_cutoff_lead_ms=self._terms.close.entry_cutoff_lead_ms,
            ),
            decision_bar_close_ms=decision_bar_close_ms,
            # A feed's bar can close a moment ahead of this machine's clock. A selection is never
            # stamped before the bar it was decided on: its quotes are then judged a little older.
            selection_ms=max(self._market.clock(), decision_bar_close_ms),
        )
        try:
            # The search runs to its answer whoever stops waiting for it; nothing is done with a late one.
            found = await asyncio.wait_for(searching, timeout=time_left_ms() / 1000)
        except TimeoutError:
            return _blocked(
                OptionEntryRefusal.SEARCH_LATE,
                "The search for this entry's contracts had not answered while the decision was still on time",
            )
        except ContractsNotResolved as refused:
            return _blocked(refused.reason, str(refused))
        if isinstance(found, SearchNotRun):
            return _blocked(found.reason, f"The search for this entry's contracts could not be run: {found.detail}")
        for leg_id, multiplier in found.multipliers.items():
            if multiplier != STANDARD_CONTRACT_MULTIPLIER:
                return _blocked(
                    OptionEffectRefusal.CONTRACT_NOT_STANDARD,
                    f"IBKR lists {found.picked[leg_id].occ_symbol} (leg {leg_id!r}) with a multiplier of "
                    f"{multiplier}. A bot trades standard contracts only, which multiply by "
                    f"{STANDARD_CONTRACT_MULTIPLIER}",
                )
        # The search took time too. The picks' quotes are tested as they stand at this moment.
        now = self._read()
        if isinstance(now, OptionEntryBlocked):
            return now
        try:
            rechecked = recheck(
                found,
                watched=now.chain.watched,
                underlying=UnderlyingObservation(pricing_bar_close=money, quote=now.underlying_quote),
                feed=now.feed,
                recheck_ms=max(self._market.clock(), found.evidence.selection_ms),
            )
        except ContractsNotResolved as refused:
            return _blocked(refused.reason, str(refused))
        quotes = {leg_id: quote for leg_id, quote in rechecked.quotes.items() if quote is not None}
        logger.info(
            "An option bot's entry resolved to its contracts",
            extra={
                "action": "option_entry_resolved",
                "strategy_instance_id": self._strategy_instance_id,
                "run_id": self._run_id,
                "underlying": self._underlying,
                "money": None if money is None else str(money),
                "picked": {leg_id: contract.occ_symbol for leg_id, contract in found.picked.items()},
                "contract_reference": IBKR_LISTING,
                "decision_bar_close_ms": decision_bar_close_ms,
                "selection_ms": found.evidence.selection_ms,
                "recheck_ms": rechecked.recheck_ms,
                "generation": rechecked.generation,
            },
        )
        return OptionEntryEvidence(
            picked=dict(found.picked),
            multipliers=dict(found.multipliers),
            quotes=quotes,
            generation=rechecked.generation,
            rechecked_at_ms=rechecked.recheck_ms,
            leg_gap_limit_percent=self._terms.leg_gap_limit_percent,
            cost_limit_percent=self._terms.cost_limit_percent,
        )

    # ---- an EXIT ----------------------------------------------------------------

    def exit_evidence(self) -> OptionExitEvidence | None:
        """The newest live quote the supervisor holds for each contract the Clerk says this bot holds, or
        None when there is none to hand over. An EXIT is sent either way.

        A quote is handed over whatever its age: the Clerk judges whether a
        decision could have used it, and prices the close from an older
        quote, saying so, when it could not. Delayed or frozen data is not
        handed over: it is no price of this market.
        """
        held = self._held_contracts()
        source = self._market.quotes()
        feed = None if source is None else source.option_feed()
        if not held or source is None or feed is None:
            return None
        chain = source.option_chain(self._underlying)
        quotes: dict[str, HeldQuote] = {}
        for symbol in held:
            watched = None if chain is None else chain.watched.get(symbol)
            quote = None if watched is None else watched.quote
            if quote is not None and quote.data_type is QuoteDataType.LIVE and receipt_times(quote):
                quotes[symbol] = quote
        if not quotes:
            return None
        return OptionExitEvidence(quotes=quotes, generation=feed.generation, read_at_ms=self._market.clock())


__all__ = [
    "IBKR_LISTING",
    "ModelInputsReader",
    "OptionEntryBlocked",
    "OptionEntryRefusal",
    "OptionMarket",
    "OptionOrderTerms",
    "OptionRun",
    "option_order_terms",
    "this_process_option_market",
]
