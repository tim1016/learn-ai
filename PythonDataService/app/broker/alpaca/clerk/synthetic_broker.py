"""Retained-bar-backed broker port for one isolated synthetic Clerk authority.

The ``sim:`` world's whole difference from the shadow world is its fill
model: a leg either transacts at the decision bar's close or is cancelled on
the spot (ruling R9). Everything durable — the order WAL, the decision-bar
binding, the position projection — is ``synthesized_orders.py``, shared with
``shadow_broker.py``; the position projection's provenance lives there.

A sale whose traded instrument had no bar for the decision's minute fills at
the close of the newest earlier one, stamped when it was decided and recorded
as ``older_bar_close`` (#2912).

A recovery EXIT (the operator's safe flatten of a stopped Dry Run) has no
decision bar: it fills at the live IBKR quote read when it is sent -- the bid
for a sale, the ask for a purchase -- retained first as its own evidence bar.
A run-end close (the simulation closing what an ended run left, owner decision
2026-09-29) fills at the last price the run saw instead, re-stated at the
instant it sold so the fill never predates its EXIT.

An option order of one leg is filled from a quote, never a bar (ADR 0076).
The Clerk binds the quote to the order before it sends: an opening order is
filled from the quote its limit was worked out from, a closing order from the
newest quote kept for its contract (``kept_option_quotes``). Both are filled
at once and in full at the natural price (``app.options.simulated_fill``),
and the order's record keeps the quote, the model and the quote's age. An
order with no quote bound is refused: the simulation invents no fill. An
option order of several legs is not taken.

The simulation holds no listing of contracts. What one contract multiplies
by comes with an opening order's quote, as the Clerk's books record it, and
is kept with the order. A position in a contract is stated by it: the
average entry price stays a share's, and the cost basis and market value are
dollars (``synthesized_positions``). A multiplier is never read off a symbol.

Three more things this simulation refuses of an option order, each before
it records a fill. A contract past its last session: nothing trades in it
any more, and no kept quote says what it is then worth. A closing order
for more than it holds, or for a contract it does not hold: that would
open the other side. A quote stamped later than its own clock: that is no
price the order could have been filled at.

An operator's Flatten of an option position is priced as every other
close of one is, from the newest quote kept. ``recovery_price_available``
answers for a contract from those quotes. The stock quote source is never
asked about a contract's symbol, so a contract never joins the shares the
IBKR supervisor watches.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Literal, NamedTuple

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.clerk.fill_models import immediate_fill_price
from app.broker.alpaca.clerk.kept_option_quotes import (
    KeptOptionQuote,
    KeptOptionQuotes,
    KeptOptionQuotesUnavailable,
)
from app.broker.alpaca.clerk.money import contract_multiplier, money_context, normalize_money
from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.alpaca.clerk.sqlite.contract_terms import is_option_contract_symbol
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
)
from app.broker.alpaca.clerk.synthesized_orders import (
    BoundBar,
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedOptionFill,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
    observed_in_send_session,
    project_positions,
)
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.errors import BrokerOptionsNotSupported, BrokerOrderRejected
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    OrderSide,
    PortfolioHistoryRange,
)
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionOrder,
    MultiLegOptionOrderRequest,
    OptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.marketdata.feed import MarketDataBar
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.options.quotes import HeldQuote
from app.options.simulated_fill import SimulatedFillModel, simulated_entry_fills, simulated_exit_fills
from app.options.structure import LegPosition, ResolvedLeg, ResolvedStructure
from app.schemas.market_liveness import TopOfBookQuote
from app.services.market_liveness import QUOTE_MAX_AGE_MS, prepared_top_of_book
from app.services.session_authority import order_session_state_at_ms, session_state_at_ms
from app.services.source_bar_ledger import (
    RECOVERY_QUOTE_PROVIDER,
    RUN_END_CLOSE_PROVIDER,
    RetainedSourceBar,
    SourceBarConflictError,
    SourceBarLedger,
)
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)
QuoteSource = Callable[[str, int], TopOfBookQuote | None]

SYNTHETIC_BROKER_ID = "synthetic"
SYNTHETIC_CAPABILITIES = BrokerCapabilities(
    broker=SYNTHETIC_BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    extended_hours_window=ALPACA_EXTENDED_HOURS_WINDOW,
    supported_order_types=("market", "limit"),
    # The simulation fills an option order of one leg from a quote handed to it. One of several
    # legs it does not take (#2913).
    supports_option_orders=True,
    supports_multi_leg_option_orders=False,
    data_feed="retained_source_bars",
    bars_may_gap=False,
    max_stream_symbols=0,
    max_concurrent_streams=0,
    rest_rate_limit_per_min=0,
)


class SimulatedPriceUnavailableError(RuntimeError):
    """No retained bar exists from which a synthetic fill may be derived."""


class _BoundOptionQuote(NamedTuple):
    """The quote one option order is to be filled from."""

    symbol: str
    quote: RecordedQuote
    # Where it is kept, when it was read back from the quotes kept.
    quote_ref: str | None
    # For a close: the generation its exit decision read quotes in, or None when it read none.
    usable_generation: int | None
    # For an opening order: the shares one contract's price stands for, as the Clerk's books record
    # it. None for a close, which opens nothing.
    multiplier: int | None


# The one leg of a single option order, as the fill model names it.
_THE_LEG = "leg"
# What the order's leg is in the structure it opens or closes: a closing sale closes a bought leg.
_LEG_POSITION: Mapping[tuple[OptionPositionIntent, OrderSide], LegPosition] = {
    (OptionPositionIntent.OPEN, OrderSide.BUY): "long",
    (OptionPositionIntent.OPEN, OrderSide.SELL): "short",
    (OptionPositionIntent.CLOSE, OrderSide.SELL): "long",
    (OptionPositionIntent.CLOSE, OrderSide.BUY): "short",
}


class OptionOrdersNotTaken:
    """The option methods of a trade port that takes no option order and holds none (#2913).

    Each refuses by type. "Absent" would be an answer about an order, and
    such a port never held one to answer about.
    """

    broker_id: str
    # How a refusal names the port: "The simulated broker".
    _named_in_option_refusals: str

    async def submit_option_order(
        self, order: OptionOrderRequest, *, client_order_id: str
    ) -> BrokerOptionOrder:
        del order, client_order_id
        raise self._option_orders_not_taken("takes no option orders")

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        del client_order_id
        raise self._option_orders_not_taken("holds no option orders")

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        del order_id
        raise self._option_orders_not_taken("holds no option orders")

    def _option_orders_not_taken(self, what: str) -> BrokerOptionsNotSupported:
        return BrokerOptionsNotSupported(
            f"{self._named_in_option_refusals} {what}.",
            broker=self.broker_id,
            detail="Its capabilities say so; nothing was sent and no order exists.",
        )


class SyntheticBroker(OptionOrdersNotTaken):
    """Durable immediate-fill port that derives prices only from retained bars.

    A market leg, or a limit leg marketable against the decision bar's close,
    fills immediately at that close. The sim world cannot rest an order: a
    non-marketable limit is canceled on the spot with zero fills (ruling R9)
    rather than waiting for a later bar to touch it.

    The Clerk remains the custody authority.  This adapter supplies the
    broker-shaped acknowledgement and fills it needs without contacting Alpaca;
    its only prices are retained bars: the decision bar, for an operator's
    recovery EXIT the live IBKR quote retained as one, and for a run-end close
    the last bar its run saw, re-stated at the instant it sells.
    """

    broker_id = SYNTHETIC_BROKER_ID
    _named_in_option_refusals = "The simulated broker"

    def __init__(
        self, *, account_id: str, source_bars: SourceBarLedger | None = None, clock: Clock = now_ms_utc,
        quote_source: QuoteSource = prepared_top_of_book,
    ) -> None:
        self._account_id = require_synthetic_account_id(account_id)
        self._clock = clock
        self._source_bars = source_bars
        self._quote_source = quote_source
        self._ledger: SynthesizedOrderLedger | None = (
            None
            if source_bars is None
            else SynthesizedOrderLedger.beside_source_bars(
                account_id=self._account_id, source_bars=source_bars, label="simulated_order"
            )
        )
        self._kept_quotes: KeptOptionQuotes | None = (
            None if source_bars is None else KeptOptionQuotes.beside(source_bars.path)
        )
        self._bound_option_quotes: dict[str, _BoundOptionQuote] = {}
        self._option_binding_lock = threading.Lock()

    def capabilities(self) -> BrokerCapabilities:
        return SYNTHETIC_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        observed_at_ms = self._clock()
        return BrokerAccountSnapshot(
            broker=self.broker_id,
            account_id=self._account_id,
            account_mode="paper",
            account_status="ACTIVE",
            currency="USD",
            cash=0.0,
            equity=0.0,
            buying_power=0.0,
            portfolio_value=0.0,
            long_market_value=0.0,
            short_market_value=0.0,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=observed_at_ms,
            observed_at_ms=observed_at_ms,
        )

    async def list_positions(self) -> list[BrokerPosition]:
        records = [] if self._ledger is None else list(self._ledger.latest_records().values())
        return synthesized_positions(
            self.broker_id,
            [record.order for record in records],
            observed_at_ms=self._clock(),
            contract_multipliers=_contract_multipliers(records),
        )

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
        symbols: tuple[str, ...] | None = None,
        until_ms: int | None = None,
    ) -> list[BrokerOrder]:
        return filter_synthesized_orders(
            self._latest_orders(), status=status, limit=limit, after_ms=after_ms,
            symbols=symbols, until_ms=until_ms,
        )

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[BrokerActivity]:
        del after_ms, limit, activity_type
        return []

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> BrokerOptionEventEvidence:
        """None, and that is all of them: the simulation takes no option order, so it holds no option to end."""
        del after_ms, resume
        return BrokerOptionEventEvidence(events=(), share_trades=(), history_complete=True)

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        del status, limit
        return []

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        del symbol
        return None

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        # "Not listed" would be a claim about a listing; the simulation has none to consult.
        del contract
        raise BrokerOptionsNotSupported(
            "The simulated broker has no option contract reference.",
            broker=self.broker_id,
            detail="It holds no listing of option contracts, so it cannot say whether this one is listed.",
        )

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        """The simulated account states no option buying power and no approval level."""
        return BrokerOptionAccountSnapshot(
            broker=self.broker_id,
            account_id=self._account_id,
            account_mode="paper",
            options_buying_power=None,
            options_approved_level=None,
            options_trading_level=None,
            observed_at_ms=self._clock(),
        )

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        observed_at_ms = self._clock()
        session = session_state_at_ms(now_ms=observed_at_ms)
        return BrokerClockEvidence(
            broker=self.broker_id,
            is_open=session.phase == "RTH",
            vendor_timestamp_ms=observed_at_ms,
            next_open_ms=session.next_transition_ms if session.phase != "RTH" else None,
            next_close_ms=session.next_transition_ms if session.phase == "RTH" else None,
            observed_at_ms=observed_at_ms,
        )

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        del history_range
        return BrokerPortfolioHistory(
            timestamps=[],
            equity=[],
            profit_loss=[],
            base_value=0.0,
            timeframe="synthetic",
        )

    @property
    def submission_response_is_authoritative_evidence(self) -> bool:
        """Every sim order executes entirely inside ``submit`` (ruling R9).

        So the returned order is the execution itself, folded as simulated
        evidence -- for a recovery EXIT as much as for a strategy decision.
        Without a retained-bar ledger nothing can execute at all.
        """
        return self._ledger is not None

    def synthesized_fill_anchors(self) -> Mapping[str, SynthesizedAnchor]:
        """How each order this world filled was priced, by client order id (#2912)."""
        return {} if self._ledger is None else self._ledger.fill_anchors()

    def synthesized_option_fills(self) -> Mapping[str, SynthesizedOptionFill]:
        """How each option order this world answered was priced, by client order id."""
        return {} if self._ledger is None else self._ledger.option_fills()

    def keep_option_quotes(self, quotes: Mapping[str, HeldQuote]) -> None:
        """Keep the newest quote the caller holds for each contract, by OCC symbol.

        A later close and the account's mark of an open contract are priced
        from what is kept here.
        """
        if self._kept_quotes is None:
            return
        self._kept_quotes.keep_all(quotes, kept_at_ms=self._clock())

    def bind_option_entry_quote(
        self, client_order_id: str, *, symbol: str, quote: HeldQuote, multiplier: int
    ) -> None:
        """Bind an opening order to the one quote its limit price was worked out from, and keep that quote.

        ``multiplier`` is the shares one contract's price stands for, as the
        Clerk's books record the contract. The simulation holds no listing
        to read it from, and never reads it off the symbol: it is kept with
        the order, and the position the order opens is counted in dollars
        by it (``synthesized_positions``).
        """
        if self._ledger is None or self._kept_quotes is None:
            raise SynthesizedBarBindingError("A simulated option fill requires the account's order ledger.")
        if not client_order_id:
            raise SynthesizedBarBindingError("A simulated option fill requires a client order id.")
        self._kept_quotes.keep(symbol, quote, kept_at_ms=self._clock())
        with self._option_binding_lock:
            self._bound_option_quotes[client_order_id] = _BoundOptionQuote(
                symbol, RecordedQuote.of(quote), None, quote.generation, multiplier
            )

    def bind_option_close_quotes(
        self,
        client_order_id: str,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        quotes: Sequence[HeldQuote],
        usable_generation: int | None,
    ) -> bool:
        """Bind a closing order to the newest quote kept for its contract.

        ``quotes`` are the ones the Clerk holds durably for this close: the
        exit decision's, and the entry's own. They are put back first if this
        account's kept quotes have lost them, so a close is priced from the
        newest of everything the account was ever handed for the contract. ``usable_generation`` is the generation the exit
        decision read its quotes in; None says it read none, and the close is
        then priced from an older quote. ``False`` leaves the close unsent:
        the newest quote kept by now has not the side this close fills at,
        or the contract is past its last session and nothing trades in it.
        """
        if self._ledger is None or self._kept_quotes is None:
            return False
        try:
            for quote in quotes:
                self._kept_quotes.restore(symbol, quote)
            newest = self._closing_quote(symbol, side=side)
        except KeptOptionQuotesUnavailable:
            self._log_kept_quotes_unreadable(symbol, client_order_id=client_order_id)
            return False
        if newest is None:
            return False
        with self._option_binding_lock:
            self._bound_option_quotes[client_order_id] = _BoundOptionQuote(
                symbol, newest.quote, newest.ref, usable_generation, None
            )
        return True

    def bind_evaluated_bar(
        self, client_order_id: str, retained_bar: RetainedSourceBar, *, fill_at_ms: int | None = None
    ) -> None:
        """Bind one minted Clerk order identity to the one exact retained bar that prices it."""
        if self._ledger is None:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding requires an authority-scoped retained-bar ledger."
            )
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar, fill_at_ms=fill_at_ms)

    def _closing_quote(self, symbol: str, *, side: Literal["buy", "sell"]) -> KeptOptionQuote | None:
        """The quote kept for a contract that a close of it made now is filled from, or None.

        None for a contract past its last session. Otherwise the quote whose
        sides were received last, when it has the side the close fills at,
        the bid for a sale: an older quote does not stand in for a newest
        one that lacks it (``KeptOptionQuotes.closing_quote``). A quote
        stamped later than this simulation's clock is left out: it is not a
        price the close could have met.
        """
        if self._kept_quotes is None:
            return None
        now_ms = self._clock()
        if past_last_session(OptionContract.from_occ_symbol(symbol).expiry_date, now_ms):
            return None
        return self._kept_quotes.closing_quote(symbol, side=side, at_ms=now_ms)

    def recovery_price_available(self, symbol: str, *, side: Literal["buy", "sell"] = "sell") -> bool:
        """Whether a recovery EXIT for ``symbol`` sent now has a price to fill at.

        Asked before any reduction is accepted, so a Dry Run with no price
        refuses its flatten instead of recording an EXIT that can never be
        sent. Reading it writes nothing.

        Shares are priced from the live IBKR quote. An option contract is
        priced from the newest quote kept for it, and only when that quote
        has something on the side the close fills at: a bid above zero for a
        sale. An older quote's bid does not count once a newer quote has
        none. The stock quote source is not asked about a contract.
        """
        contract_symbol = symbol.upper()
        if is_option_contract_symbol(contract_symbol):
            now_ms = self._clock()
            expiry = OptionContract.from_occ_symbol(contract_symbol).expiry_date
            if self._kept_quotes is None or past_last_session(expiry, now_ms):
                return False
            try:
                return self._kept_quotes.prices_a_close(contract_symbol, side=side, at_ms=now_ms)
            except KeptOptionQuotesUnavailable:
                self._log_kept_quotes_unreadable(contract_symbol, client_order_id=None)
                return False
        return self._recovery_quote(symbol) is not None

    def _log_kept_quotes_unreadable(self, symbol: str, *, client_order_id: str | None) -> None:
        """An unreadable file of kept quotes prices nothing: the close is left unsent, and this says why."""
        logger.error(
            "the option quotes kept for a Dry Run cannot be read; a close of the contract cannot be priced",
            exc_info=True,
            extra={"action": "kept_option_quotes_unreadable", "account_id": self._account_id,
                   "symbol": symbol, "client_order_id": client_order_id},
        )

    def bind_latest_recovery_bar(self, client_order_id: str, *, symbol: str, side: str) -> bool:
        """Bind a recovery EXIT to the live IBKR quote read now, retained as its evidence bar.

        A recovery EXIT -- the operator's safe flatten, a sweep re-drive --
        has no strategy decision bar. It fills at the quote's bid for a sale
        and its ask for a purchase, so a Dry Run stopped yesterday sells at
        today's price, never at a bar from an earlier session. ``False`` (no
        fresh quote in an open session) leaves the reduction unsent.
        """
        if is_option_contract_symbol(symbol.upper()):
            # A contract is closed from a quote kept for it (``bind_option_close_quotes``), never from the
            # stock quote source, which is not asked about it.
            return False
        quote = self._recovery_quote(symbol)
        if self._ledger is None or self._source_bars is None or quote is None:
            return False
        price = normalize_money(quote.bid if side == "sell" else quote.ask)
        observed_at_ms = quote.observed_at_ms
        session = order_session_state_at_ms(now_ms=observed_at_ms, extended_window=SYNTHETIC_CAPABILITIES.extended_hours_window)
        try:
            retained_bar = self._source_bars.retain_recovery_quote(MarketDataBar(
                symbol=symbol, start_ms=observed_at_ms - 1, end_ms=observed_at_ms,
                open=price, high=price, low=price, close=price, volume=0,
                fetched_at_ms=self._clock(), feed_id=RECOVERY_QUOTE_PROVIDER, session_phase=session.phase,
            ))
        except SourceBarConflictError:
            logger.warning(
                "simulated recovery quote could not be retained as fill evidence",
                exc_info=True,
                extra={"action": "simulated_recovery_quote_unretained", "account_id": self._account_id,
                       "symbol": symbol, "client_order_id": client_order_id},
            )
            return False
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar)
        return True

    def bind_run_end_close_bar(self, client_order_id: str, *, symbol: str) -> bool:
        """Bind a run-end close to the last price the run saw, retained at the instant it sells.

        That price is the newest bar the market delivered, not a live quote:
        the close must not wait on a feed that may be why the run ended, and
        it works overnight (owner decision 2026-09-29). Re-stating it at now
        keeps the fill after the EXIT that caused it. The order's record
        keeps when that price last traded, which the re-stated bar no longer
        shows, so the price's age is not lost (#2912); it keeps none when no
        trade this run retained made that price. ``False`` (no bar was ever
        delivered for ``symbol``) leaves the close unsent.
        """
        if self._ledger is None or self._source_bars is None:
            return False
        last_seen = self._source_bars.latest_for_symbol(symbol, market_only=True)
        if last_seen is None:
            return False
        # The feed delivers a bar for a minute nothing traded in, repeating
        # the last trade's price: that price is as old as the trade. When the
        # last trade this run kept made another price, or it kept none, when
        # this price traded is not known, and no time is recorded.
        last_traded = self._source_bars.latest_for_symbol(
            symbol, market_only=True, traded_only=True, at_or_before_ms=last_seen.end_ms
        )
        price_traded_at_ms = (
            last_traded.end_ms if last_traded is not None and last_traded.close == last_seen.close else None
        )
        now_ms = self._clock()
        session = order_session_state_at_ms(now_ms=now_ms, extended_window=SYNTHETIC_CAPABILITIES.extended_hours_window)
        price = last_seen.close
        try:
            retained_bar = self._source_bars.retain_run_end_close(MarketDataBar(
                symbol=symbol, start_ms=now_ms - 1, end_ms=now_ms,
                open=price, high=price, low=price, close=price, volume=0,
                fetched_at_ms=now_ms, feed_id=RUN_END_CLOSE_PROVIDER, session_phase=session.phase,
            ))
        except SourceBarConflictError:
            logger.warning(
                "simulated run-end close price could not be retained as fill evidence",
                exc_info=True,
                extra={"action": "simulated_run_end_close_unretained", "account_id": self._account_id,
                       "symbol": symbol, "client_order_id": client_order_id},
            )
            return False
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar, restated_bar_end_ms=price_traded_at_ms)
        logger.info(
            "bound a Dry Run's run-end close to the last price its run saw",
            extra={"action": "simulated_run_end_close_bound", "account_id": self._account_id,
                   "symbol": symbol, "client_order_id": client_order_id, "price": str(price),
                   "last_seen_bar_ref": last_seen.bar_ref, "last_seen_end_ms": last_seen.end_ms,
                   "price_traded_at_ms": price_traded_at_ms},
        )
        return True

    def _recovery_quote(self, symbol: str) -> TopOfBookQuote | None:
        if self._ledger is None or self._source_bars is None:
            return None
        now_ms = self._clock()
        quote = self._quote_source(symbol, now_ms)
        in_session = quote is not None and observed_in_send_session(
            start_ms=quote.observed_at_ms, end_ms=quote.observed_at_ms, now_ms=now_ms,
            extended_window=SYNTHETIC_CAPABILITIES.extended_hours_window,
        )
        return quote if in_session else None

    async def submit(
        self,
        leg: BrokerOrderLeg,
        *,
        client_order_id: str,
        retained_bar: RetainedSourceBar | None = None,
    ) -> BrokerOrder:
        if self._ledger is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        with self._ledger.transaction() as records:
            existing = self._ledger.find_order(records, client_order_id)
            if existing is not None:
                self._ledger.consume_bound_bar(client_order_id)
                return existing
            priced_by = self._submission_bar(
                client_order_id=client_order_id,
                symbol=leg.symbol,
                retained_bar=retained_bar,
            )
            bar = priced_by.bar
            order = self._resolved_order(leg, client_order_id=client_order_id, priced_by=priced_by)
            self._ledger.append_locked(
                records,
                order=order,
                leg=leg,
                anchor=SynthesizedAnchor(
                    fill_model=priced_by.fill_model,
                    evidence_account_id=bar.account_id,
                    provider=bar.provider,
                    bar_identity=bar.bar_identity,
                    bar_ref=bar.bar_ref,
                    decision_bar_start_ms=bar.start_ms,
                    decision_bar_end_ms=bar.end_ms,
                    fill_bar_ref=bar.bar_ref if order.status == "filled" else None,
                    restated_bar_end_ms=priced_by.restated_bar_end_ms,
                ),
            )
            return order

    async def cancel(self, order_id: str) -> None:
        if self._ledger is None:
            return
        with self._ledger.transaction() as records:
            record = next(
                (row for row in self._ledger.latest_records_from(records).values() if row.order.order_id == order_id),
                None,
            )
            if record is None or record.order.status == "filled":
                return
            now = self._clock()
            self._ledger.append_locked(
                records,
                order=record.order.model_copy(
                    update={"status": "canceled", "canceled_at_ms": now, "updated_at_ms": now}
                ),
                leg=record.leg,
                anchor=record.anchor,
                option=record.option,
            )

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return next(
            (order for order in self._latest_orders() if order.client_order_id == client_order_id),
            None,
        )

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        return next((order for order in self._latest_orders() if order.order_id == order_id), None)

    async def submit_option_order(
        self, order: OptionOrderRequest, *, client_order_id: str
    ) -> BrokerOptionOrder:
        """Fill an option order of one leg from the quote bound to it, or end it unfilled.

        The simulation takes an option order of one leg, so it answers this
        and the two lookups itself. What it still does not take, an order of
        several legs or any order when it has no ledger to record a fill in,
        it refuses in the words every simulated port refuses option orders
        in (``OptionOrdersNotTaken``).

        The order's record is one more in the same ledger. Its ``order`` is
        the fill as the Clerk folds every simulated fill, under the
        contract's own symbol, so the stock lookups, the open orders and the
        positions all show it.
        """
        if isinstance(order, MultiLegOptionOrderRequest):
            raise self._option_orders_not_taken("takes no option orders of several legs")
        if self._ledger is None:
            raise self._option_orders_not_taken("takes no option orders without its order ledger")
        symbol = order.contract.occ_symbol
        with self._ledger.transaction() as records:
            existing = self._ledger.find_record(records, client_order_id)
            bound = self._consume_option_binding(client_order_id)
            if existing is not None:
                if existing.option is None:
                    raise BrokerOrderRejected(
                        "That client order id already names a stock order in this simulation.", broker=self.broker_id
                    )
                return _option_order_view(existing, observed_at_ms=self._clock())
            at_ms = self._clock()
            if past_last_session(order.contract.expiry_date, at_ms):
                raise BrokerOrderRejected(
                    f"{symbol} expired on {order.contract.expiry_date.isoformat()} and its last session is over. "
                    "Nothing trades in it, and the simulation invents no fill.",
                    broker=self.broker_id,
                )
            if order.intent is OptionPositionIntent.CLOSE:
                self._refuse_a_close_of_more_than_is_held(order, self._ledger.latest_orders_from_records(records))
            if bound is None or bound.symbol != symbol:
                raise BrokerOrderRejected(
                    f"The simulation was handed no quote of {symbol} to fill this order from, and it invents no fill.",
                    broker=self.broker_id,
                )
            if order.intent is OptionPositionIntent.OPEN and bound.multiplier is None:
                # Its position could be counted only as shares: a hundred times too small.
                raise BrokerOrderRejected(
                    f"The simulation was not told how many shares one contract of {symbol} stands for, so it "
                    "could not count the position this order opens. It fills no such order.",
                    broker=self.broker_id,
                )
            fill = self._simulated_option_fill(order, bound, at_ms=at_ms)
            within_limit = order.limit_price is None or (
                fill.price <= order.limit_price if order.side is OrderSide.BUY else fill.price >= order.limit_price
            )
            record = self._ledger.append_locked(
                records,
                order=_option_order_evidence(
                    order, client_order_id=client_order_id, broker_id=self.broker_id, at_ms=at_ms,
                    fill_price=fill.price if within_limit else None,
                ),
                option=SynthesizedOptionFill(
                    request=order,
                    quote=bound.quote,
                    multiplier=bound.multiplier,
                    quote_ref=bound.quote_ref,
                    fill_model=fill.model if within_limit else None,
                    fill_price=fill.price if within_limit else None,
                    quote_age_ms=fill.quote_age_ms if within_limit else None,
                    filled_at_ms=at_ms if within_limit else None,
                ),
            )
            return _option_order_view(record, observed_at_ms=at_ms)

    def _refuse_a_close_of_more_than_is_held(self, order: SingleOptionOrderRequest, orders: list[BrokerOrder]) -> None:
        """Refuse a closing order this simulation's own position does not cover.

        A sale to close needs that many contracts held long, and a purchase
        to close that many held short. Filled beyond that, a closing order
        would open the other side, which no close may do.
        """
        symbol = order.contract.occ_symbol
        held, _notional = project_positions(orders).get(symbol, (0.0, 0.0))
        closable = held if order.side is OrderSide.SELL else -held
        if order.quantity > closable:
            raise BrokerOrderRejected(
                f"This closing order would {order.side.value} {order.quantity} of {symbol}, and the simulation "
                f"holds {held:g}. A closing order never opens a position.",
                broker=self.broker_id,
            )

    def _simulated_option_fill(
        self, order: SingleOptionOrderRequest, bound: _BoundOptionQuote, *, at_ms: int
    ) -> _PricedOptionFill:
        """The one leg's fill at the natural price of the bound quote, or a definitive refusal."""
        symbol = order.contract.occ_symbol
        structure = ResolvedStructure(
            legs=(
                ResolvedLeg(
                    leg_id=_THE_LEG, contract=order.contract, position=_LEG_POSITION[order.intent, order.side], ratio=1
                ),
            )
        )
        quotes = {_THE_LEG: bound.quote.held}
        try:
            if order.intent is OptionPositionIntent.OPEN:
                (fill,) = simulated_entry_fills(structure, quotes, units=order.quantity, at_ms=at_ms)
            else:
                (fill,) = simulated_exit_fills(
                    structure, quotes, units=order.quantity, at_ms=at_ms,
                    generation=bound.usable_generation, max_age_ms=QUOTE_MAX_AGE_MS,
                )
        except ValueError as exc:
            raise BrokerOrderRejected(
                f"The quote handed over for {symbol} cannot fill this order: {exc}.", broker=self.broker_id
            ) from exc
        if fill.price is None or fill.quote_age_ms is None:
            raise BrokerOrderRejected(
                f"Nothing kept for {symbol} gives a price to close it at, and the simulation invents no fill.",
                broker=self.broker_id,
            )
        if fill.price <= 0:
            # The books record no fill at a price of zero, so a contract nobody bids for stays open.
            raise BrokerOrderRejected(
                f"Nothing is bid for {symbol}. The simulation does not record a sale for nothing.",
                broker=self.broker_id,
            )
        return _PricedOptionFill(fill.price, fill.model, fill.quote_age_ms)

    def _consume_option_binding(self, client_order_id: str) -> _BoundOptionQuote | None:
        with self._option_binding_lock:
            return self._bound_option_quotes.pop(client_order_id, None)

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        return self._option_order_where(lambda record: record.order.client_order_id == client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        return self._option_order_where(lambda record: record.order.order_id == order_id)

    def _option_order_where(self, matches: Callable[[SynthesizedOrderRecord], bool]) -> BrokerOptionOrder | None:
        """The option order this simulation holds that ``matches``, or None: it holds no such option order."""
        if self._ledger is None:
            return None
        record = next(
            (row for row in self._ledger.latest_records().values() if row.option is not None and matches(row)), None
        )
        return None if record is None else _option_order_view(record, observed_at_ms=self._clock())

    def _latest_orders(self) -> list[BrokerOrder]:
        return [] if self._ledger is None else self._ledger.latest_orders()

    def _submission_bar(
        self,
        *,
        client_order_id: str,
        symbol: str,
        retained_bar: RetainedSourceBar | None,
    ) -> BoundBar:
        """The bar this order fills at, with the fill instant and re-stated price's time it was bound with."""
        if self._ledger is None or self._source_bars is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        bound = self._ledger.consume_binding(client_order_id)
        if retained_bar is not None:
            bound = BoundBar(retained_bar, None, None)
        if bound is not None:
            return bound._replace(bar=self._ledger.verified_retained_bar(bound.bar, symbol=symbol))
        # The last price the market delivered: a no-submit world's own fill
        # evidence (a recovery quote, a run-end close) is never a fill price.
        latest = self._source_bars.latest_for_symbol(symbol, market_only=True)
        if latest is None:
            raise SimulatedPriceUnavailableError(
                f"No retained source bar exists for {symbol!r}; refusing a synthetic fill."
            )
        return BoundBar(latest, None, None)

    def _resolved_order(self, leg: BrokerOrderLeg, *, client_order_id: str, priced_by: BoundBar) -> BrokerOrder:
        """The order this leg becomes against one bar: filled, or cancelled unfilled (ruling R9)."""
        return shape_immediate_order(
            leg,
            client_order_id=client_order_id,
            bar=priced_by.bar,
            broker_id=self.broker_id,
            id_prefix="sim",
            observed_at_ms=self._clock(),
            fill_at_ms=priced_by.older_fill_at_ms,
        )


class _PricedOptionFill(NamedTuple):
    """A simulated option fill that has a price above zero."""

    price: Decimal
    model: SimulatedFillModel
    quote_age_ms: int


def _option_order_evidence(
    order: SingleOptionOrderRequest,
    *,
    client_order_id: str,
    broker_id: str,
    at_ms: int,
    fill_price: Decimal | None,
) -> BrokerOrder:
    """A simulated option order as the Clerk folds every simulated order: filled at one price, or ended unfilled.

    The symbol is the contract's, the quantity is contracts and the price is
    dollars a share. ``fill_price`` is None for a limit the quote did not
    reach: the simulation cannot rest an order, so it ends on the spot with
    nothing filled (ruling R9).
    """
    filled = fill_price is not None
    contracts = float(order.quantity)
    return BrokerOrder(
        broker=broker_id,
        order_id=f"sim-order:{client_order_id}",
        client_order_id=client_order_id,
        symbol=order.contract.occ_symbol,
        asset_class="us_option",
        side=order.side.value,
        order_type=order.order_type.value,
        time_in_force="day",
        quantity=contracts,
        limit_price=None if order.limit_price is None else float(order.limit_price),
        stop_price=None,
        extended_hours=False,
        submitted_at_ms=at_ms,
        created_at_ms=at_ms,
        updated_at_ms=at_ms,
        expired_at_ms=None,
        observed_at_ms=at_ms,
        filled_quantity=contracts if filled else 0.0,
        filled_avg_price=float(fill_price) if fill_price is not None else None,
        status="filled" if filled else "canceled",
        filled_at_ms=at_ms if filled else None,
        canceled_at_ms=None if filled else at_ms,
        events=(
            [
                {
                    "event_type": "fill",
                    "occurred_at_ms": at_ms,
                    "price": float(fill_price),
                    "quantity": contracts,
                    "execution_id": f"sim-execution:{client_order_id}",
                }
            ]
            if fill_price is not None
            else []
        ),
    )


def _option_order_view(record: SynthesizedOrderRecord, *, observed_at_ms: int) -> BrokerOptionOrder:
    """One recorded option order as the option port answers about it."""
    option, order = record.option, record.order
    assert option is not None
    request = option.request
    return BrokerOptionOrder(
        broker=order.broker,
        order_id=order.order_id,
        client_order_id=order.client_order_id,
        multi_leg=False,
        contract=request.contract,
        side=request.side,
        intent=request.intent,
        order_type=request.order_type.value,
        time_in_force="day",
        quantity=request.quantity,
        filled_quantity=request.quantity if option.fill_price is not None else 0,
        limit_price=request.limit_price,
        filled_avg_price=option.fill_price,
        status=order.status,
        submitted_at_ms=order.submitted_at_ms,
        created_at_ms=order.created_at_ms,
        updated_at_ms=order.updated_at_ms,
        filled_at_ms=order.filled_at_ms,
        canceled_at_ms=order.canceled_at_ms,
        expired_at_ms=order.expired_at_ms,
        observed_at_ms=observed_at_ms,
    )


def shape_immediate_order(
    leg: BrokerOrderLeg,
    *,
    client_order_id: str,
    bar: RetainedSourceBar,
    broker_id: str,
    id_prefix: str,
    observed_at_ms: int,
    fill_at_ms: int | None = None,
) -> BrokerOrder:
    """One bar, one answer: filled at its close, or cancelled unfilled.

    The fill decision itself belongs to ``fill_models`` — a second copy of
    "would this have transacted?" living here is exactly how the sim world and
    the shadow port drift apart. This function only shapes the resulting
    ``BrokerOrder``; both no-submit worlds call it for a regular-session leg.

    The order is stamped at the bar's close, which is when it was decided.
    ``fill_at_ms`` stamps it later than that: the bar is an older one, priced
    because the decision's own minute had none (#2912, ``BoundBar``).
    """
    at_ms = bar.end_ms if fill_at_ms is None else fill_at_ms
    fill = immediate_fill_price(leg, bar.close)
    filled = fill is not None
    return BrokerOrder(
        broker=broker_id,
        order_id=f"{id_prefix}-order:{client_order_id}",
        client_order_id=client_order_id,
        symbol=leg.symbol,
        asset_class="us_equity",
        side=leg.side,
        order_type=str(leg.order_type),
        time_in_force=str(leg.time_in_force),
        quantity=leg.quantity,
        limit_price=leg.limit_price,
        stop_price=None,
        extended_hours=leg.extended_hours,
        submitted_at_ms=at_ms,
        created_at_ms=at_ms,
        updated_at_ms=at_ms,
        expired_at_ms=None,
        observed_at_ms=observed_at_ms,
        filled_quantity=leg.quantity if filled else 0.0,
        filled_avg_price=float(fill) if fill is not None else None,
        status="filled" if filled else "canceled",
        filled_at_ms=at_ms if filled else None,
        canceled_at_ms=None if filled else at_ms,
        events=(
            [
                {
                    "event_type": "fill",
                    "occurred_at_ms": at_ms,
                    "price": float(fill),
                    "quantity": leg.quantity,
                    "execution_id": f"{id_prefix}-execution:{client_order_id}",
                }
            ]
            if fill is not None
            else []
        ),
    )


def _contract_multipliers(records: Sequence[SynthesizedOrderRecord]) -> dict[str, int]:
    """The multiplier each option contract was opened with, by OCC symbol, from the orders' own records."""
    return {
        record.order.symbol: record.option.multiplier
        for record in records
        if record.option is not None and record.option.multiplier is not None
    }


def _position_dollars(per_share: float, multiplier: int | None) -> float:
    """A position's cost as the fold counts it, in dollars: itself for shares, times the multiplier for a contract."""
    if multiplier is None:
        return per_share
    with money_context():
        return float(normalize_money(per_share) * contract_multiplier(multiplier))


def synthesized_positions(
    broker_id: str,
    orders: list[BrokerOrder],
    *,
    observed_at_ms: int | None = None,
    contract_multipliers: Mapping[str, int] | None = None,
) -> list[BrokerPosition]:
    """Shape the canonical projection as broker positions (shared with the shadow read port).

    ``contract_multipliers`` names the option contracts among the symbols,
    each with the shares one contract's price stands for. A position in one
    keeps its average entry price a share, as a broker states it, and its
    cost basis and market value are that price times the contracts times the
    multiplier. Every other symbol is shares, counted as it always was.

    Formula: cost basis = market value = |signed notional| of the average-cost
      fold; for an option contract, |signed notional a share| x multiplier.
      A simulation states no current price, so a position is valued at cost.
    Reference: ADR 0076 decision 13 (an option's notional is contracts x
      premium a share x the contract's multiplier).
    Canonical implementation: ``synthesized_orders.project_positions`` (the
      fold) and ``clerk/money.py`` (the exact product).
    Validated against: tests/broker/alpaca/clerk/test_synthetic_broker_option_orders.py
      (the multiplier's product exact; what the fold's own floats carry into
      it to ``atol=1e-9``) and tests/services/test_source_bar_ledger.py (the fold).
    """
    quantities = project_positions(orders)
    observed_at_ms = now_ms_utc() if observed_at_ms is None else observed_at_ms
    multipliers = contract_multipliers or {}
    # A position is in what its orders were in: shares, or an option contract.
    asset_classes = {order.symbol: order.asset_class for order in orders if order.asset_class is not None}
    return [
        BrokerPosition(
            broker=broker_id,
            symbol=symbol,
            asset_id=None,
            asset_class=asset_classes.get(symbol, "us_equity"),
            quantity=quantity,
            side="long" if quantity > 0 else "short",
            average_entry_price=(abs(cost / quantity) if quantity else 0.0),
            market_value=_position_dollars(abs(cost), multipliers.get(symbol)),
            cost_basis=_position_dollars(abs(cost), multipliers.get(symbol)),
            current_price=None,
            unrealized_pl=0.0,
            unrealized_plpc=None,
            observed_at_ms=observed_at_ms,
        )
        for symbol, (quantity, cost) in quantities.items()
        if position_quantity_is_nonzero(quantity)
    ]


def filter_synthesized_orders(
    orders: list[BrokerOrder],
    *,
    status: str | None,
    limit: int | None,
    after_ms: int | None,
    symbols: tuple[str, ...] | None = None,
    until_ms: int | None = None,
) -> list[BrokerOrder]:
    """The read port's order filter, newest first (shared with the shadow read port).

    ``status`` is Alpaca's **query category** — ``open`` / ``closed`` / ``all``
    — not an order status; the real port hands it straight to the vendor. The
    sim world never noticed the difference because it cannot rest an order, so
    every sim order is terminal and ``open`` was legitimately empty. The shadow
    world can rest one, and the reconciliation sweep reads exactly
    ``status="open"``: an exact-status match there would hide a working order
    from the fold and report position drift in its place.
    """
    if status is not None and status != "all":
        if status == "open":
            orders = [order for order in orders if not _is_terminal_order(order)]
        elif status == "closed":
            orders = [order for order in orders if _is_terminal_order(order)]
        else:
            raise ValueError(f"unknown order query category {status!r}")
    if after_ms is not None:
        orders = [order for order in orders if (order.updated_at_ms or 0) >= after_ms]
    if symbols is not None:
        wanted = {symbol.upper() for symbol in symbols}
        orders = [order for order in orders if order.symbol.upper() in wanted]
    if until_ms is not None:
        orders = [order for order in orders if (order.submitted_at_ms or 0) < until_ms]
    return list(reversed(orders))[:limit]


def _is_terminal_order(order: BrokerOrder) -> bool:
    """Whether an order has reached a terminal status, in the sweep's convention."""
    return order.status.lower() in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES


__all__ = [
    "SYNTHETIC_BROKER_ID",
    "SYNTHETIC_CAPABILITIES",
    "SimulatedPriceUnavailableError",
    "SyntheticBroker",
    "filter_synthesized_orders",
    "shape_immediate_order",
    "synthesized_positions",
]
