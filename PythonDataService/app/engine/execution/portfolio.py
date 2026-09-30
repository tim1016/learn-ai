"""Portfolio: tracks cash, holdings, and applies fills.

The portfolio models a single-account state with per-symbol positions. It
supports LEAN-style ``set_holdings(symbol, fraction)`` which translates a
target portfolio weight into a market order for the right number of shares
at the symbol's current reference price.

Formula: equity = cash + sum(quantity * valuation_mark). Sizing uses the
  same cash-plus-holdings formula at decision prices, preserving its signal
  bar basis while reported equity uses the latest observed minute close.
Reference: repository backtest price-basis contract, issue #2449.
Canonical implementation: this file (Portfolio); BacktestEngine owns updates.
Validated against: tests/engine/test_portfolio.py and
  tests/engine/test_engine_fill_modes.py::test_equity_uses_current_minute_while_sizing_and_fills_keep_signal_price.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
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
    # Decision reference: the signal close during a strategy callback.
    reference_price: dict[str, Decimal] = field(default_factory=dict)
    # Latest input-minute close; consolidated signals cannot overwrite it.
    valuation_price: dict[str, Decimal] = field(default_factory=dict)
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

    def update_market_price(self, symbol: str, price: Decimal) -> None:
        """Observe a minute close before any consolidated decision fires."""
        self.valuation_price[symbol] = price
        self.update_reference_price(symbol, price)

    def get_position(self, symbol: str) -> Position:
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)
        return self.positions[symbol]

    def total_value(self) -> Decimal:
        """Mark holdings at the latest minute close for equity and drawdown."""
        return self._value_at(self.valuation_price)

    def _value_at(self, prices: dict[str, Decimal]) -> Decimal:
        """Value holdings on the supplied basis, using known prices as fallback."""
        value = self.cash
        for sym, pos in self.positions.items():
            price = prices.get(sym, self.reference_price.get(sym, pos.average_price))
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
        submitted_at_ms: int,
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
            submitted_at_ms=submitted_at_ms,
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
        submitted_at_ms: int,
        limit_price: Decimal,
        tag: str = "",
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
    ) -> Order:
        """Submit a resting limit order.

        The engine moves the order to its ``resting_limit_orders`` list
        at drain time and evaluates it against every subsequent minute
        bar until it fills (per the configured penetration rule) or the
        evaluation boundary clears the book.
        """
        if quantity == 0:
            raise ValueError("cannot submit a zero-quantity limit order")
        direction = Direction.LONG if quantity > 0 else Direction.SHORT
        order = Order(
            order_id=self._next_id(),
            symbol=symbol,
            quantity=quantity,
            order_type=OrderType.LIMIT,
            submitted_at_ms=submitted_at_ms,
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
        submitted_at_ms: int,
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
        # Sizing must retain its historical signal-price basis even when
        # the minute that emitted that signal has a newer valuation mark.
        portfolio_value = self._value_at(self.reference_price)
        target_quantity = self.sizing_model.target_quantity(
            portfolio_value=portfolio_value,
            price=price,
            target_fraction=target_fraction,
            order_fee=self.order_fee,
        )
        delta = target_quantity - current_pos.quantity
        if delta == 0:
            return None
        return self.submit_market_order(symbol, delta, submitted_at_ms, tag=tag or "SetHoldings")

    def liquidate(self, symbol: str, submitted_at_ms: int) -> Order | None:
        pos = self.get_position(symbol)
        if pos.quantity == 0:
            return None
        return self.submit_market_order(symbol, -pos.quantity, submitted_at_ms, tag="Liquidate")

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

    def rebase(self) -> None:
        """Return to the configured starting state, keeping only price marks.

        Cash goes back to ``initial_cash``; positions, accumulated fees, and
        queued orders are dropped. Both price maps survive because each is
        a fact about the market, not about this book, and the order-id
        counter keeps counting so ids stay unique across the reset. Used at
        the warmup → evaluation boundary of a primed backtest.
        """
        self.cash = self.initial_cash
        self.positions.clear()
        self.total_fees = Decimal(0)
        self.pending_orders.clear()

    def drain_pending(self) -> Iterable[Order]:
        orders = list(self.pending_orders)
        self.pending_orders.clear()
        return orders
