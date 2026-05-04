"""Live portfolio adapter for paper trading.

The strategy-facing methods intentionally mirror
``app.engine.execution.portfolio.Portfolio``: strategies can call
``set_holdings`` and ``liquidate`` synchronously inside bar handlers. The
live engine drains the resulting pending orders and submits them through the
existing IBKR paper-order boundary asynchronously.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.broker.ibkr.account import fetch_account_summary, fetch_positions
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec
from app.broker.ibkr.orders import (
    OrderNotFoundError,
    cancel_paper_order,
    list_open_orders,
    place_paper_order,
)
from app.engine.execution.order import Direction, Order, OrderEvent, OrderType
from app.engine.execution.portfolio import Position


class BrokerAdapter(Protocol):
    """Async broker surface LivePortfolio needs."""

    async def fetch_account_summary(self): ...

    async def fetch_positions(self): ...

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck: ...

    async def cancel_open_orders(self) -> list[int]:
        """Cancel every order the broker still considers open.

        Returns the list of cancelled ``order_id`` values. Used by the
        force-flat barrier so that any in-flight orders submitted on
        prior bars do not survive the session-close cutoff.
        """
        ...


class IbkrBrokerAdapter:
    """Production adapter over the existing broker module."""

    def __init__(self, client: IbkrClient) -> None:
        self._client = client

    async def fetch_account_summary(self):
        return await fetch_account_summary(self._client)

    async def fetch_positions(self):
        return await fetch_positions(self._client)

    async def place_order(self, spec: IbkrOrderSpec) -> IbkrOrderAck:
        return await place_paper_order(self._client, spec)

    async def cancel_open_orders(self) -> list[int]:
        open_orders = await list_open_orders(self._client)
        cancelled: list[int] = []
        for order in open_orders:
            try:
                await cancel_paper_order(self._client, order.order_id)
                cancelled.append(order.order_id)
            except OrderNotFoundError:
                # Filled or cancelled between the list call and ours; either
                # way it's no longer open, so the force-flat goal is satisfied.
                continue
        return cancelled


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
