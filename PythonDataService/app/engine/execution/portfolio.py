"""Portfolio: tracks cash, holdings, and applies fills.

The portfolio models a single-account state with per-symbol positions. It
supports LEAN-style ``set_holdings(symbol, fraction)`` which translates a
target portfolio weight into a market order for the right number of shares
at the symbol's current reference price.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.engine.execution.order import (
    Direction,
    Order,
    OrderEvent,
    OrderType,
)
from app.engine.execution.sizing import SimpleFloorSizing, SizingModel


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    average_price: Decimal = Decimal(0)

    @property
    def direction(self) -> Direction:
        if self.quantity > 0:
            return Direction.LONG
        if self.quantity < 0:
            return Direction.SHORT
        return Direction.FLAT

    def market_value(self, current_price: Decimal) -> Decimal:
        return Decimal(self.quantity) * current_price


@dataclass
class Portfolio:
    initial_cash: Decimal
    cash: Decimal = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    total_fees: Decimal = Decimal(0)
    pending_orders: list[Order] = field(default_factory=list)
    _next_order_id: int = 0
    # Last known reference price per symbol (updated on every bar).
    reference_price: dict[str, Decimal] = field(default_factory=dict)
    # Position-sizing policy for set_holdings. Defaults to the historical
    # plain-floor behaviour; the engine swaps in LeanSetHoldingsSizing for
    # LEAN-pinned / cross-engine-parity runs.
    sizing_model: SizingModel = field(default_factory=SimpleFloorSizing)
    # Per-order fee the sizing model reserves (set from the fill model's
    # commission by the engine; 0 leaves simple_floor unchanged).
    order_fee: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        self.cash = self.initial_cash

    # ------------------------------------------------------------------
    # Price tracking
    # ------------------------------------------------------------------
    def update_reference_price(self, symbol: str, price: Decimal) -> None:
        self.reference_price[symbol] = price

    def get_position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def total_value(self) -> Decimal:
        value = self.cash
        for sym, pos in self.positions.items():
            price = self.reference_price.get(sym, pos.average_price)
            value += pos.market_value(price)
        return value

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------
    def _next_id(self) -> int:
        self._next_order_id += 1
        return self._next_order_id

    def submit_market_order(
        self,
        symbol: str,
        quantity: int,
        time: datetime,
        tag: str = "",
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
    ) -> Order:
        if quantity == 0:
            raise ValueError("cannot submit a zero-quantity market order")
        direction = Direction.LONG if quantity > 0 else Direction.SHORT
        order = Order(
            order_id=self._next_id(),
            symbol=symbol,
            quantity=quantity,
            order_type=OrderType.MARKET,
            time=time,
            direction=direction,
            tag=tag,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
        )
        self.pending_orders.append(order)
        return order

    def submit_limit_order(
        self,
        symbol: str,
        quantity: int,
        time: datetime,
        limit_price: Decimal,
        tag: str = "",
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
    ) -> Order:
        """Submit a resting limit order.

        The engine moves the order to its ``resting_limit_orders`` list
        at drain time and evaluates it against every subsequent minute
        bar until it fills (per the configured penetration rule) or is
        cancelled by force-flat.
        """
        if quantity == 0:
            raise ValueError("cannot submit a zero-quantity limit order")
        direction = Direction.LONG if quantity > 0 else Direction.SHORT
        order = Order(
            order_id=self._next_id(),
            symbol=symbol,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            time=time,
            direction=direction,
            tag=tag,
            limit_price=limit_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
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
        """Rebalance to a target portfolio fraction for ``symbol``.

        Mirrors LEAN's ``QCAlgorithm.SetHoldings``. The share count comes
        from ``self.sizing_model`` (see ``app.engine.execution.sizing``);
        ``LeanSetHoldingsSizing`` reproduces LEAN's buffered quantity,
        ``SimpleFloorSizing`` is the historical plain floor. Liquidates if
        the target is zero.
        """
        target_fraction = Decimal(str(target_fraction))
        price = self.reference_price.get(symbol)
        if price is None:
            raise RuntimeError(
                f"Cannot set_holdings on {symbol}: no reference price. Did the strategy receive a bar first?"
            )
        current_pos = self.get_position(symbol)
        portfolio_value = self.total_value()
        target_quantity = self.sizing_model.target_quantity(
            portfolio_value=portfolio_value,
            price=price,
            target_fraction=target_fraction,
            order_fee=self.order_fee,
        )
        delta = target_quantity - current_pos.quantity
        if delta == 0:
            return None
        return self.submit_market_order(symbol, delta, time, tag=tag or "SetHoldings")

    def liquidate(self, symbol: str, time: datetime) -> Order | None:
        pos = self.get_position(symbol)
        if pos.quantity == 0:
            return None
        return self.submit_market_order(symbol, -pos.quantity, time, tag="Liquidate")

    # ------------------------------------------------------------------
    # Fill application
    # ------------------------------------------------------------------
    def apply_fill(self, event: OrderEvent) -> None:
        pos = self.get_position(event.symbol)
        fill_qty = event.fill_quantity
        fill_price = event.fill_price

        if pos.quantity == 0 or (pos.quantity > 0) == (fill_qty > 0):
            # Opening or adding to an existing position
            new_qty = pos.quantity + fill_qty
            if new_qty != 0:
                pos.average_price = (
                    pos.average_price * Decimal(pos.quantity) + fill_price * Decimal(fill_qty)
                ) / Decimal(new_qty)
            pos.quantity = new_qty
        else:
            # Reducing or flipping the position
            new_qty = pos.quantity + fill_qty
            if (pos.quantity > 0) != (new_qty > 0) and new_qty != 0:
                # Flip through zero — reset average price to the fill price
                pos.average_price = fill_price
            pos.quantity = new_qty
            if pos.quantity == 0:
                pos.average_price = Decimal(0)

        # Cash accounting: buying decreases cash, selling increases cash.
        self.cash -= Decimal(fill_qty) * fill_price
        self.cash -= event.fee
        self.total_fees += event.fee

    def clear_pending(self) -> None:
        self.pending_orders.clear()

    def drain_pending(self) -> Iterable[Order]:
        orders = list(self.pending_orders)
        self.pending_orders.clear()
        return orders
