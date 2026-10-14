"""Retained-bar-backed broker port for one isolated synthetic Clerk authority.

The ``sim:`` world's whole difference from the shadow world is its fill
model: a leg either transacts at the decision bar's close or is cancelled on
the spot (ruling R9). Everything durable — the order WAL, the decision-bar
binding, the position projection — is ``synthesized_orders.py``, shared with
``shadow_broker.py``; the position projection's provenance lives there.

A sale whose traded instrument had no bar for the decision's minute fills at
the close of the newest earlier one, stamped when it was decided and recorded
as ``older_bar_close`` (#2912). An exit re-driven with no bar bound fills the
same way at the last price the market delivered, stamped when it is sent
(#2926).

A recovery EXIT (the operator's safe flatten of a stopped Dry Run) has no
decision bar: it fills at the live IBKR quote read when it is sent -- the bid
for a sale, the ask for a purchase -- retained first as its own evidence bar.
A run-end close (the simulation closing what an ended run left, owner decision
2026-09-29) fills at the last price the run saw instead, re-stated at the
instant it sold so the fill never predates its EXIT.

An option order of one leg is filled from a quote, never a bar (ADR 0076).
How it is filled lives in ``simulated_option_orders``, which the shadow world's port shares.
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

A contract this simulation still holds after its last session is settled by
the simulation itself (ADR 0076 decision 22): no broker exists to do it, and
nothing trades in the contract any more. That is one explicit step,
``settle_expired_options``, which the Clerk's poll of option events calls.
The rule, the price it reads and the record it keeps are
``simulated_option_settlement``'s: the contract is sold, in cash, at its
exercise value at the close of its last session, rounded down to the cent,
or expires at nothing. It is never exercised into shares. The step writes
the settlement to its own order ledger first, so its positions show it, and
hands it back with its record. While the closing bar may still arrive it
waits. With no price at the close it settles nothing and says why. Its read
of option events settles nothing and reports none.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.account_authority import require_synthetic_account_id
from app.broker.alpaca.clerk.fill_models import immediate_fill_price
from app.broker.alpaca.clerk.kept_option_quotes import KeptOptionQuotes
from app.broker.alpaca.clerk.money import contract_multiplier, money_context, normalize_money
from app.broker.alpaca.clerk.simulated_option_expiry import SimulatedOptionExpiry
from app.broker.alpaca.clerk.simulated_option_orders import SimulatedOptionOrders, contract_multipliers
from app.broker.alpaca.clerk.simulated_option_settlement import (
    OwnersWord,
    SimulatedOptionSettlements,
)
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
    observed_in_send_session,
    project_positions,
)
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.errors import BrokerOptionsNotSupported
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.broker.contract.option_events import (
    BrokerOptionEventEvidence,
    BrokerOptionEventResume,
)
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionOrder,
    MultiLegOptionOrderRequest,
    OptionOrderRequest,
)
from app.marketdata.feed import MarketDataBar
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote
from app.schemas.market_liveness import TopOfBookQuote
from app.services.market_liveness import prepared_top_of_book
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
    supports_multi_leg_option_orders=True,
    data_feed="retained_source_bars",
    bars_may_gap=False,
    max_stream_symbols=0,
    max_concurrent_streams=0,
    rest_rate_limit_per_min=0,
)


class SimulatedPriceUnavailableError(RuntimeError):
    """No retained bar exists from which a synthetic fill may be derived."""


class SyntheticBroker:
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
        # The option orders of one leg this simulation fills, from the quotes kept beside its order
        # ledger. None with no ledger to record a fill in.
        self._options: SimulatedOptionOrders | None = (
            None
            if source_bars is None or self._ledger is None
            else SimulatedOptionOrders(
                account_id=self._account_id,
                ledger=self._ledger,
                kept=KeptOptionQuotes.beside(source_bars.path),
                clock=clock,
                broker_id=self.broker_id,
                id_prefix="sim",
            )
        )

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
            contract_multipliers=contract_multipliers(records),
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
        """No broker reports this account's option events: none, and that is the whole of it.

        Reading settles nothing and writes nothing. What this simulation
        settled itself is handed over by ``settle_expired_options``, with
        its record, and by nothing else.
        """
        del after_ms, resume
        return BrokerOptionEventEvidence(events=(), share_trades=(), history_complete=True)

    def settle_expired_options(
        self, *, after_ms: int, at_the_owners_word: Mapping[str, OwnersWord] | None = None
    ) -> SimulatedOptionSettlements:
        return SimulatedOptionExpiry(
            account_id=self._account_id, broker_id=self.broker_id, ledger=self._ledger,
            source_bars=self._source_bars, clock=self._clock,
        ).settle_expired_options(after_ms=after_ms, at_the_owners_word=at_the_owners_word)

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
        return {} if self._options is None else self._options.fills()

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
        if self._options is None:
            raise SynthesizedBarBindingError("A simulated option fill requires the account's order ledger.")
        self._options.bind_entry_quote(client_order_id, symbol=symbol, quote=quote, multiplier=multiplier)

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
        if self._options is None:
            return False
        return self._options.bind_close_quotes(
            client_order_id, symbol=symbol, side=side, quotes=quotes, usable_generation=usable_generation
        )

    def bind_evaluated_bar(
        self, client_order_id: str, retained_bar: RetainedSourceBar, *, fill_at_ms: int | None = None
    ) -> None:
        """Bind one minted Clerk order identity to the one exact retained bar that prices it."""
        if self._ledger is None:
            raise SynthesizedBarBindingError(
                "Synthetic bar binding requires an authority-scoped retained-bar ledger."
            )
        self._ledger.bind_evaluated_bar(client_order_id, retained_bar, fill_at_ms=fill_at_ms)

    def bind_decision_bar(
        self, client_order_id: str, *, bar_identity: str, decided_at_ms: int, symbol: str
    ) -> bool:
        """Rebind an order to the exact bar its decision priced it at, from durable evidence (#2919).

        The first send binds the bar in memory
        (``_PricingBarBoundTradePort``); a process death between the send
        request and the simulated fill leaves the pass that re-drives the
        order with none. The acceptance's recorded evidence -- the pricing
        bar's identity and the decision's close -- rebuilds the same binding,
        looked up from the authority's own retained bars so nothing is
        trusted from the record. ``False`` (the bar is no longer retained, or
        is not the order's symbol) leaves the order unsent: a decision is
        never re-priced from another bar.
        """
        if self._ledger is None or self._source_bars is None:
            return False
        retained = self._source_bars.by_identity(bar_identity)
        if retained is None or retained.symbol != symbol:
            return False
        self._ledger.bind_evaluated_bar(client_order_id, retained, fill_at_ms=decided_at_ms)
        return True

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
            return self._options is not None and self._options.prices_a_close(contract_symbol, side=side)
        return self._recovery_quote(symbol) is not None

    def bind_latest_recovery_bar(
        self, client_order_id: str, *, symbol: str, side: str, extended_hours: bool = False
    ) -> bool:
        """Bind a recovery EXIT to the live IBKR quote read now, retained as its evidence bar.

        A recovery EXIT -- the operator's safe flatten, a sweep re-drive --
        has no strategy decision bar. It fills at the quote's bid for a sale
        and its ask for a purchase, so a Dry Run stopped yesterday sells at
        today's price, never at a bar from an earlier session. ``False`` (no
        fresh quote in an open session) leaves the reduction unsent. The
        fill is the quote's own instant, and this world rests no limit, so
        ``extended_hours`` names nothing it decides.
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
            if record is None or _is_terminal_order(record.order):
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
        """Fill an option order from its bound quotes, or end it unfilled.

        The simulation takes one-leg orders and multi-leg opening orders,
        and answers both lookups itself. An order with no ledger to record a fill in
        is refused.

        The order's record is one more in the same ledger. Its ``order`` is
        the fill as the Clerk folds every simulated fill, under the
        contract's own symbol, so the stock lookups, the open orders and the
        positions all show it.
        """
        if self._options is None:
            raise BrokerOptionsNotSupported(
                "The simulated broker takes no option orders without its order ledger.",
                broker=self.broker_id,
                detail="Its capabilities say so; nothing was sent and no order exists.",
            )
        if isinstance(order, MultiLegOptionOrderRequest):
            return self._options.submit_multi(order, client_order_id=client_order_id)
        return self._options.submit(order, client_order_id=client_order_id)

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        if self._options is None:
            return None
        return self._options.order_where(
            lambda record: record.order.client_order_id == client_order_id
        ) or self._options.multi_leg_sent_as(client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        if self._options is None:
            return None
        one_leg = self._options.order_where(lambda record: record.order.order_id == order_id)
        if one_leg is not None or not order_id.startswith("sim-order:"):
            return one_leg
        return self._options.multi_leg_sent_as(order_id.removeprefix("sim-order:"))

    def _latest_orders(self) -> list[BrokerOrder]:
        return [] if self._ledger is None else self._ledger.latest_orders()

    def _submission_bar(
        self,
        *,
        client_order_id: str,
        symbol: str,
    ) -> BoundBar:
        """The bar this order fills at, with the fill instant and re-stated price's time it was bound with."""
        if self._ledger is None or self._source_bars is None:
            raise SimulatedPriceUnavailableError(
                "Synthetic execution requires an authority-scoped retained-bar ledger."
            )
        bound = self._ledger.consume_binding(client_order_id)
        if bound is not None:
            return bound._replace(bar=self._ledger.verified_retained_bar(bound.bar, symbol=symbol))
        # The last price the market delivered: a no-submit world's own fill
        # evidence (a recovery quote, a run-end close) is never a fill price.
        latest = self._source_bars.latest_for_symbol(symbol, market_only=True)
        if latest is None:
            raise SimulatedPriceUnavailableError(
                f"No retained source bar exists for {symbol!r}; refusing a synthetic fill."
            )
        # Stamped when it is sent, never at the older bar's own close (#2926).
        # A clock behind the feed keeps the bar's close: a fill is never
        # stamped before the close of the bar that prices it.
        now_ms = self._clock()
        return BoundBar(latest, now_ms if now_ms > latest.end_ms else None, None)

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
