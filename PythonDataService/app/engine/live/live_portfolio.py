"""Live portfolio adapter for paper trading.

The strategy-facing methods intentionally mirror
``app.engine.execution.portfolio.Portfolio``: strategies can call
``set_holdings`` and ``liquidate`` synchronously inside bar handlers. The
live engine drains the resulting pending orders and submits them through the
existing IBKR paper-order boundary asynchronously.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.broker.ibkr.account import fetch_account_summary, fetch_positions
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderEvent, IbkrOrderSpec
from app.broker.ibkr.orders import (
    OrderNotFoundError,
    cancel_paper_order,
    list_open_orders,
    place_paper_order,
    stream_order_events,
)
from app.engine.execution.order import Direction, Order, OrderEvent, OrderType
from app.engine.execution.portfolio import Position

logger = logging.getLogger(__name__)


class LiveBrokerEventStreamError(RuntimeError):
    """Raised when the IBKR order-event stream has terminated unexpectedly.

    Once the background stream task dies, fills stop arriving at the
    engine. Continuing to submit orders while the broker side is silent
    would silently desync the portfolio from broker reality. The engine
    surfaces this as a failed run rather than a degraded one.
    """


class BrokerAdapter(Protocol):
    """Async broker surface LivePortfolio needs."""

    async def fetch_account_summary(self): ...

    async def fetch_positions(self): ...

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck: ...

    async def cancel_open_orders(self) -> list[int]:
        """Cancel every order this runner still has open at the broker.

        Real adapters scope to the runner's own orders so that running
        the live engine never touches an unrelated open order on the
        same paper account. Returns the list of cancelled ``order_id``
        values.
        """
        ...


class IbkrBrokerAdapter:
    """Production adapter over the existing broker module.

    Tracks the set of order IDs this adapter has placed so that
    ``cancel_open_orders`` only touches the live runner's own orders.
    Any pre-existing or unrelated order on the paper account is left
    alone, even if it shares the connected client. Buffers IBKR order
    events so the live engine can drain real fills per bar.
    """

    def __init__(self, client: IbkrClient) -> None:
        self._client = client
        self._owned_order_ids: set[int] = set()
        self._event_buffer: list[IbkrOrderEvent] = []
        self._event_task: asyncio.Task[None] | None = None
        self._stream_failure: BaseException | None = None

    @property
    def owned_order_ids(self) -> set[int]:
        return set(self._owned_order_ids)

    @property
    def stream_failure(self) -> BaseException | None:
        """The exception that terminated the order-event stream, if any.

        ``None`` while the stream is healthy (or hasn't been started).
        Set once if the streaming task exits via an unhandled exception
        — the engine reads this each iteration and fails the run, since
        a dead stream means broker fills are no longer being ingested.
        """
        return self._stream_failure

    async def fetch_account_summary(self):
        return await fetch_account_summary(self._client)

    async def fetch_positions(self):
        return await fetch_positions(self._client)

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        ack = await place_paper_order(self._client, spec)
        self._owned_order_ids.add(int(ack.order_id))
        return ack

    async def cancel_open_orders(self) -> list[int]:
        open_orders = await list_open_orders(self._client)
        cancelled: list[int] = []
        for order in open_orders:
            if int(order.order_id) not in self._owned_order_ids:
                # Foreign order on this paper account — never the
                # runner's to cancel. Leaving it alone is the whole
                # point of the ownership filter.
                continue
            try:
                await cancel_paper_order(self._client, order.order_id)
                cancelled.append(order.order_id)
            except OrderNotFoundError:
                # Filled or cancelled between the list call and ours; either
                # way it's no longer open, so the force-flat goal is satisfied.
                continue
        return cancelled

    async def start_event_stream(self) -> None:
        """Begin draining IBKR order events into the local buffer."""
        if self._event_task is not None:
            return
        self._stream_failure = None
        self._event_task = asyncio.create_task(self._run_event_stream())

    async def stop_event_stream(self) -> None:
        if self._event_task is None:
            return
        self._event_task.cancel()
        try:
            await self._event_task
        except asyncio.CancelledError:
            pass
        finally:
            self._event_task = None

    async def _run_event_stream(self) -> None:
        try:
            async for event in stream_order_events(self._client):
                if int(event.order_id) not in self._owned_order_ids:
                    continue
                self._event_buffer.append(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Record the cause so the engine can fail the run on the
            # next iteration — silently retiring would leave the engine
            # submitting orders while no fills arrive, desyncing the
            # portfolio from broker reality. Logging here keeps the
            # original traceback in the operator log.
            logger.exception("IBKR order-event stream terminated unexpectedly")
            self._stream_failure = exc

    def drain_broker_events(self) -> list[IbkrOrderEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events


@dataclass
class LivePortfolio:
    """Portfolio-shaped live state with broker-backed account snapshots."""

    broker: BrokerAdapter
    cash: Decimal = Decimal(0)
    net_liquidation: Decimal = Decimal(0)
    positions: dict[str, Position] = field(default_factory=dict)
    pending_orders: list[Order] = field(default_factory=list)
    reference_price: dict[str, Decimal] = field(default_factory=dict)
    total_fees: Decimal = Decimal(0)
    _next_order_id: int = 0

    async def refresh_from_broker(self) -> None:
        """Refresh cash, net liquidation, and positions from the broker."""
        account = await self.broker.fetch_account_summary()
        self.cash = Decimal(str(account.cash_balance or 0))
        self.net_liquidation = Decimal(str(account.net_liquidation or account.cash_balance or 0))

        snapshot = await self.broker.fetch_positions()
        refreshed: dict[str, Position] = {}
        for pos in snapshot.positions:
            refreshed[pos.symbol.upper()] = Position(
                symbol=pos.symbol.upper(),
                quantity=int(pos.quantity),
                average_price=Decimal(str(pos.avg_cost)),
            )
        self.positions = refreshed

    def update_reference_price(self, symbol: str, price: Decimal) -> None:
        self.reference_price[symbol.upper()] = price

    def get_position(self, symbol: str) -> Position:
        sym = symbol.upper()
        if sym not in self.positions:
            self.positions[sym] = Position(symbol=sym)
        return self.positions[sym]

    def total_value(self) -> Decimal:
        has_open_positions = any(pos.quantity != 0 for pos in self.positions.values())
        if self.net_liquidation and (not has_open_positions or not self.reference_price):
            return self.net_liquidation
        value = self.cash
        for sym, pos in self.positions.items():
            price = self.reference_price.get(sym, pos.average_price)
            value += pos.market_value(price)
        return value

    def _next_id(self) -> int:
        self._next_order_id += 1
        return self._next_order_id

    def submit_market_order(self, symbol: str, quantity: int, time: datetime, tag: str = "") -> Order:
        if quantity == 0:
            raise ValueError("cannot submit a zero-quantity market order")
        order = Order(
            order_id=self._next_id(),
            symbol=symbol.upper(),
            quantity=quantity,
            order_type=OrderType.MARKET,
            time=time,
            direction=Direction.LONG if quantity > 0 else Direction.SHORT,
            tag=tag,
        )
        self.pending_orders.append(order)
        return order

    def set_holdings(
        self,
        symbol: str,
        target_fraction: Decimal | float,
        time: datetime,
        tag: str = "",
    ) -> Order | None:
        """Mirror simulated Portfolio.set_holdings integer sizing."""
        sym = symbol.upper()
        target_fraction = Decimal(str(target_fraction))
        price = self.reference_price.get(sym)
        if price is None:
            raise RuntimeError(f"Cannot set_holdings on {sym}: no reference price.")
        current_pos = self.get_position(sym)
        target_value = self.total_value() * target_fraction
        target_quantity = int(target_value / price)
        delta = target_quantity - current_pos.quantity
        if delta == 0:
            return None
        return self.submit_market_order(sym, delta, time, tag=tag or "SetHoldings")

    def liquidate(self, symbol: str, time: datetime) -> Order | None:
        pos = self.get_position(symbol)
        if pos.quantity == 0:
            return None
        return self.submit_market_order(symbol, -pos.quantity, time, tag="Liquidate")

    def drain_pending(self) -> Iterable[Order]:
        orders = list(self.pending_orders)
        self.pending_orders.clear()
        return orders

    def record_broker_fill(self, event: OrderEvent) -> None:
        """Update the local cache from a broker-reported fill event."""
        pos = self.get_position(event.symbol)
        new_qty = pos.quantity + event.fill_quantity
        if pos.quantity == 0 or (pos.quantity > 0) == (event.fill_quantity > 0):
            if new_qty != 0:
                pos.average_price = (
                    pos.average_price * Decimal(pos.quantity)
                    + event.fill_price * Decimal(event.fill_quantity)
                ) / Decimal(new_qty)
        elif new_qty != 0 and (pos.quantity > 0) != (new_qty > 0):
            pos.average_price = event.fill_price
        pos.quantity = new_qty
        if pos.quantity == 0:
            pos.average_price = Decimal(0)
        self.cash -= Decimal(event.fill_quantity) * event.fill_price
        self.cash -= event.fee
        self.net_liquidation = Decimal(0)
        self.total_fees += event.fee

    async def submit_pending_orders(self) -> list[IbkrOrderAck]:
        """Submit all locally queued orders through the paper-order boundary."""
        acks: list[IbkrOrderAck] = []
        for order in self.drain_pending():
            spec = IbkrOrderSpec(
                symbol=order.symbol,
                sec_type="STK",
                action="BUY" if order.quantity > 0 else "SELL",
                quantity=abs(order.quantity),
                order_type="MKT",
                time_in_force="DAY",
                confirm_paper=True,
                client_order_id=f"live-{order.order_id}",
            )
            acks.append(await self.broker.place_order(spec))
        return acks
