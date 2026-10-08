"""The option order of one leg that a no-submit port fills itself (ADR 0076).

Two worlds fill an order without sending it: a Dry Run's simulation
(``synthetic_broker``) and a Live Shadow's order book (``shadow_broker``).
Each fills an option order of one leg the same way, and that way lives here
once, over the order ledger both worlds share (``synthesized_orders``) and
the quotes kept for the account (``kept_option_quotes``).

An option order is filled from a quote, never a bar. The Clerk binds the
quote to the order before it sends: an opening order is filled from the
quote its limit was worked out from, a closing order from the newest quote
kept for its contract. Both are filled at once and in full at the natural
price (``app.options.simulated_fill``), and the order's record keeps the
quote, the model and the quote's age. An order with no quote bound is
refused: nothing here invents a fill. A limit the quote does not reach ends
the order on the spot with nothing filled, because neither world can rest an
order.

Neither world holds a listing of contracts. What one contract multiplies by
comes with an opening order's quote, as the Clerk's books record it, and is
kept with the order (``contract_multipliers``). A multiplier is never read
off a symbol.

Three more things are refused of an option order, each before a fill is
recorded. A contract past its last session: nothing trades in it any more,
and no kept quote says what it is then worth. A closing order for more than
is held, or for a contract not held: that would open the other side. A quote
stamped later than the port's own clock: that is no price the order could
have been filled at.

The record's ``order`` is the fill as the Clerk folds every simulated fill,
under the contract's own symbol and the world's own execution identity
(``sim-execution:`` / ``shadow-execution:``), so the stock lookups, the open
orders and the positions of the port all show it.

The Dry Run also fills an opening order of several legs through the
canonical multi-leg fill model. Every leg commits together. Live Shadow
refuses that order before it reaches this module (ADR 0076).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Literal, NamedTuple

from app.broker.alpaca.clerk.kept_option_quotes import (
    KeptOptionQuote,
    KeptOptionQuotes,
    KeptOptionQuotesUnavailable,
)
from app.broker.alpaca.clerk.option_effects import RecordedQuote
from app.broker.alpaca.clerk.simulated_multi_leg_orders import (
    BoundLegQuote,
    filled_multi_leg_order,
    legs_recorded,
    multi_leg_order_view,
)
from app.broker.alpaca.clerk.synthesized_orders import (
    SynthesizedBarBindingError,
    SynthesizedOptionFill,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
    project_positions,
)
from app.broker.contract.errors import BrokerEvidenceUnavailable, BrokerOrderRejected
from app.broker.contract.models import BrokerOrder, OrderSide, OrderType
from app.broker.contract.options import (
    BrokerOptionOrder,
    MultiLegOptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract
from app.options.deadlines import past_last_session
from app.options.quotes import HeldQuote
from app.options.simulated_fill import SimulatedFillModel, simulated_entry_fills, simulated_exit_fills
from app.options.structure import LegPosition, ResolvedLeg, ResolvedStructure
from app.services.market_liveness import QUOTE_MAX_AGE_MS
from app.utils.timestamps import Clock

logger = logging.getLogger(__name__)


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


class _PricedOptionFill(NamedTuple):
    """A simulated option fill that has a price above zero."""

    price: Decimal
    model: SimulatedFillModel
    quote_age_ms: int


# The one leg of a single option order, as the fill model names it.
_THE_LEG = "leg"
# What the order's leg is in the structure it opens or closes: a closing sale closes a bought leg.
_LEG_POSITION: Mapping[tuple[OptionPositionIntent, OrderSide], LegPosition] = {
    (OptionPositionIntent.OPEN, OrderSide.BUY): "long",
    (OptionPositionIntent.OPEN, OrderSide.SELL): "short",
    (OptionPositionIntent.CLOSE, OrderSide.SELL): "long",
    (OptionPositionIntent.CLOSE, OrderSide.BUY): "short",
}


class SimulatedOptionOrders:
    """One no-submit port's option orders of one leg: bound to a quote, filled from it, and looked up.

    ``id_prefix`` is the world's own: ``sim`` for a Dry Run and ``shadow``
    for a Live Shadow. An order is ``<prefix>-order:<client order id>`` and
    its fill ``<prefix>-execution:<client order id>``, as that world's
    orders in shares are.
    """

    def __init__(
        self,
        *,
        account_id: str,
        ledger: SynthesizedOrderLedger,
        kept: KeptOptionQuotes,
        clock: Clock,
        broker_id: str,
        id_prefix: str,
    ) -> None:
        self._account_id = account_id
        self._ledger = ledger
        self._kept_quotes = kept
        self._clock = clock
        self._broker_id = broker_id
        self._id_prefix = id_prefix
        self._bound_option_quotes: dict[str, _BoundOptionQuote] = {}
        self._bound_leg_quotes: dict[str, dict[str, _BoundOptionQuote]] = {}
        self._option_binding_lock = threading.Lock()

    def fills(self) -> Mapping[str, SynthesizedOptionFill]:
        """How each option order this world answered was priced, by client order id."""
        return self._ledger.option_fills()

    def bind_entry_quote(self, client_order_id: str, *, symbol: str, quote: HeldQuote, multiplier: int) -> None:
        """Bind an opening order to the one quote its limit price was worked out from, and keep that quote.

        ``multiplier`` is the shares one contract's price stands for, as the
        Clerk's books record the contract. No listing is held to read it
        from, and it is never read off the symbol: it is kept with the
        order, and the position the order opens is counted in dollars by it.
        """
        if not client_order_id:
            raise SynthesizedBarBindingError("A simulated option fill requires a client order id.")
        self._kept_quotes.keep(symbol, quote, kept_at_ms=self._clock())
        with self._option_binding_lock:
            self._bound_option_quotes[client_order_id] = _BoundOptionQuote(
                symbol, RecordedQuote.of(quote), None, quote.generation, multiplier
            )
            self._bound_leg_quotes.setdefault(client_order_id, {})[symbol] = self._bound_option_quotes[client_order_id]

    def bind_close_quotes(
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
            self._bound_leg_quotes.setdefault(client_order_id, {})[symbol] = self._bound_option_quotes[client_order_id]
        return True

    def _closing_quote(self, symbol: str, *, side: Literal["buy", "sell"]) -> KeptOptionQuote | None:
        """The quote kept for a contract that a close of it made now is filled from, or None.

        None for a contract past its last session. Otherwise the quote whose
        sides were received last, when it has the side the close fills at,
        the bid for a sale: an older quote does not stand in for a newest
        one that lacks it (``KeptOptionQuotes.closing_quote``). A quote
        stamped later than this port's clock is left out: it is not a price
        the close could have met.
        """
        now_ms = self._clock()
        if past_last_session(OptionContract.from_occ_symbol(symbol).expiry_date, now_ms):
            return None
        return self._kept_quotes.closing_quote(symbol, side=side, at_ms=now_ms)

    def prices_a_close(self, contract_symbol: str, *, side: Literal["buy", "sell"]) -> bool:
        """Whether a close of ``contract_symbol`` sent now has a price to fill at. Reading it writes nothing.

        Only when the newest quote kept for the contract has something on
        the side the close fills at: a bid above zero for a sale. An older
        quote's bid does not count once a newer quote has none. Never for a
        contract past its last session.
        """
        now_ms = self._clock()
        if past_last_session(OptionContract.from_occ_symbol(contract_symbol).expiry_date, now_ms):
            return False
        try:
            return self._kept_quotes.prices_a_close(contract_symbol, side=side, at_ms=now_ms)
        except KeptOptionQuotesUnavailable:
            self._log_kept_quotes_unreadable(contract_symbol, client_order_id=None)
            return False

    def _log_kept_quotes_unreadable(self, symbol: str, *, client_order_id: str | None) -> None:
        """An unreadable file of kept quotes prices nothing: the close is left unsent, and this says why."""
        logger.error(
            "the option quotes kept for a simulated account cannot be read; a close of the contract cannot be priced",
            exc_info=True,
            extra={"action": "kept_option_quotes_unreadable", "account_id": self._account_id,
                   "symbol": symbol, "client_order_id": client_order_id},
        )

    def submit(self, order: SingleOptionOrderRequest, *, client_order_id: str) -> BrokerOptionOrder:
        """Fill an option order of one leg from the quote bound to it, or end it unfilled.

        The order's record is one more in the port's own ledger. Asked
        again under the same client order id, the order already recorded is
        the answer, and a quote bound for the second ask is dropped unused.
        """
        symbol = order.contract.occ_symbol
        with self._ledger.transaction() as records:
            existing = self._ledger.find_record(records, client_order_id)
            bound = self._consume_option_binding(client_order_id)
            if existing is not None:
                if existing.option is None:
                    raise BrokerOrderRejected(
                        "That client order id already names a stock order in this simulation.", broker=self._broker_id
                    )
                return option_order_view(existing, observed_at_ms=self._clock())
            at_ms = self._clock()
            if past_last_session(order.contract.expiry_date, at_ms):
                raise BrokerOrderRejected(
                    f"{symbol} expired on {order.contract.expiry_date.isoformat()} and its last session is over. "
                    "Nothing trades in it, and the simulation invents no fill.",
                    broker=self._broker_id,
                )
            if order.intent is OptionPositionIntent.CLOSE:
                self._refuse_a_close_of_more_than_is_held(order, self._ledger.latest_orders_from_records(records))
            if bound is None or bound.symbol != symbol:
                raise BrokerOrderRejected(
                    f"The simulation was handed no quote of {symbol} to fill this order from, and it invents no fill.",
                    broker=self._broker_id,
                )
            if order.intent is OptionPositionIntent.OPEN and bound.multiplier is None:
                # Its position could be counted only as shares: a hundred times too small.
                raise BrokerOrderRejected(
                    f"The simulation was not told how many shares one contract of {symbol} stands for, so it "
                    "could not count the position this order opens. It fills no such order.",
                    broker=self._broker_id,
                )
            fill = self._simulated_option_fill(order, bound, at_ms=at_ms)
            within_limit = order.limit_price is None or (
                fill.price <= order.limit_price if order.side is OrderSide.BUY else fill.price >= order.limit_price
            )
            record = self._ledger.append_locked(
                records,
                order=_option_order_evidence(
                    order, client_order_id=client_order_id, broker_id=self._broker_id, id_prefix=self._id_prefix,
                    at_ms=at_ms, fill_price=fill.price if within_limit else None,
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
            return option_order_view(record, observed_at_ms=at_ms)

    def _refuse_a_close_of_more_than_is_held(self, order: SingleOptionOrderRequest, orders: list[BrokerOrder]) -> None:
        """Refuse a closing order this world's own position does not cover.

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
                broker=self._broker_id,
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
                f"The quote handed over for {symbol} cannot fill this order: {exc}.", broker=self._broker_id
            ) from exc
        if fill.price is None or fill.quote_age_ms is None:
            raise BrokerOrderRejected(
                f"Nothing kept for {symbol} gives a price to close it at, and the simulation invents no fill.",
                broker=self._broker_id,
            )
        if fill.price <= 0:
            # The books record no fill at a price of zero, so a contract nobody bids for stays open.
            raise BrokerOrderRejected(
                f"Nothing is bid for {symbol}. The simulation does not record a sale for nothing.",
                broker=self._broker_id,
            )
        return _PricedOptionFill(fill.price, fill.model, fill.quote_age_ms)

    def _consume_option_binding(self, client_order_id: str) -> _BoundOptionQuote | None:
        with self._option_binding_lock:
            self._bound_leg_quotes.pop(client_order_id, None)
            return self._bound_option_quotes.pop(client_order_id, None)

    def submit_multi(self, order: MultiLegOptionOrderRequest, *, client_order_id: str) -> BrokerOptionOrder:
        """Fill every opening leg together, or none, retaining one record per leg."""
        with self._ledger.transaction() as records:
            with self._option_binding_lock:
                self._bound_option_quotes.pop(client_order_id, None)
                bound = self._bound_leg_quotes.pop(client_order_id, {})
            if self._ledger.find_record(records, client_order_id) is not None:
                raise BrokerOrderRejected(
                    "That client order id already names an order of one leg in this simulation.", broker=self._broker_id
                )
            at_ms = self._clock()
            already = legs_recorded(self._ledger.latest_records_from(records), client_order_id)
            if already:
                return self._multi_leg_view(already, client_order_id, observed_at_ms=at_ms)
            # Validate every reduction before the transaction writes any leg.
            previous = self._ledger.latest_records_from(records)
            multipliers = contract_multipliers(tuple(previous.values()))
            for leg in order.legs:
                if leg.intent is OptionPositionIntent.CLOSE:
                    self._refuse_a_close_of_more_than_is_held(
                        SingleOptionOrderRequest(
                            contract=leg.contract, side=leg.side, intent=leg.intent,
                            quantity=leg.ratio * order.quantity, order_type=OrderType.MARKET,
                        ), [record.order for record in previous.values()],
                    )
            legs = filled_multi_leg_order(
                order, client_order_id=client_order_id,
                bound={
                    symbol: BoundLegQuote(
                        quoted.quote, quoted.quote_ref, quoted.multiplier or multipliers.get(symbol), quoted.usable_generation,
                    )
                    for symbol, quoted in bound.items()
                },
                at_ms=at_ms, broker_id=self._broker_id,
                shape_leg_order=lambda request, leg_client_order_id, fill_price, order_type: _option_order_evidence(
                    request, client_order_id=leg_client_order_id, broker_id=self._broker_id,
                    id_prefix=self._id_prefix, at_ms=at_ms, fill_price=fill_price,
                ).model_copy(update={"order_type": order_type}),
            )
            appended = self._ledger.append_option_legs_locked(records, legs)
            return self._multi_leg_view(appended, client_order_id, observed_at_ms=at_ms)

    def _multi_leg_view(
        self, legs: Sequence[SynthesizedOrderRecord], client_order_id: str, *, observed_at_ms: int
    ) -> BrokerOptionOrder:
        if len(legs) < 2:
            raise BrokerEvidenceUnavailable(
                "The simulation's ledger holds a part of an option order of several legs.", broker=self._broker_id,
                detail=f"Order {client_order_id!r} has {len(legs)} of its legs on record.",
            )
        return multi_leg_order_view(
            legs, parent_client_order_id=client_order_id,
            parent_order_id=f"{self._id_prefix}-order:{client_order_id}",
            broker_id=self._broker_id, observed_at_ms=observed_at_ms,
        )

    def multi_leg_sent_as(self, client_order_id: str) -> BrokerOptionOrder | None:
        """Read the parent's answer from its retained leg records."""
        legs = legs_recorded(self._ledger.latest_records(), client_order_id)
        return self._multi_leg_view(legs, client_order_id, observed_at_ms=self._clock()) if legs else None

    def order_where(self, matches: Callable[[SynthesizedOrderRecord], bool]) -> BrokerOptionOrder | None:
        """The option order this world holds that ``matches``, or None: it holds no such option order."""
        record = next(
            (row for row in self._ledger.latest_records().values() if row.option is not None and matches(row)), None
        )
        return None if record is None else option_order_view(record, observed_at_ms=self._clock())


def _option_order_evidence(
    order: SingleOptionOrderRequest,
    *,
    client_order_id: str,
    broker_id: str,
    id_prefix: str,
    at_ms: int,
    fill_price: Decimal | None,
) -> BrokerOrder:
    """A simulated option order as the Clerk folds every simulated order: filled at one price, or ended unfilled.

    The symbol is the contract's, the quantity is contracts and the price is
    dollars a share. ``fill_price`` is None for a limit the quote did not
    reach: a no-submit world cannot rest an order, so it ends on the spot
    with nothing filled (ruling R9).
    """
    filled = fill_price is not None
    contracts = float(order.quantity)
    return BrokerOrder(
        broker=broker_id,
        order_id=f"{id_prefix}-order:{client_order_id}",
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
                    "execution_id": f"{id_prefix}-execution:{client_order_id}",
                }
            ]
            if fill_price is not None
            else []
        ),
    )


def option_order_view(record: SynthesizedOrderRecord, *, observed_at_ms: int) -> BrokerOptionOrder:
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


def contract_multipliers(records: Sequence[SynthesizedOrderRecord]) -> dict[str, int]:
    """The multiplier each option contract was opened with, by OCC symbol, from the orders' own records."""
    return {
        record.order.symbol: record.option.multiplier
        for record in records
        if record.option is not None and record.option.multiplier is not None
    }


__all__ = [
    "SimulatedOptionOrders",
    "contract_multipliers",
    "option_order_view",
]
