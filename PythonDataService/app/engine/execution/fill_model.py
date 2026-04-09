"""Fill models for market orders.

Two modes are supported:

* ``SIGNAL_BAR_CLOSE`` — the order fills at ``bar.close`` of the consolidated
  bar that triggered it. This reproduces the bookkeeping inside LEAN's
  ``SpyEmaCrossoverAlgorithm.OnFifteenMinuteBar``, where ``_entryPrice`` is
  set to ``bar.Close`` on the signal bar. Use this for exact replication of
  the LEAN trade log.

* ``NEXT_BAR_OPEN`` — the order fills at the open of the bar *after* the
  signal bar. This is closer to LEAN's actual ``EquityFillModel.MarketFill``
  behavior for backtests without tick data, where
  ``GetBestEffortTradeBar`` returns the next available bar whose ``EndTime``
  is strictly after the order time.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.order import (
    Direction,
    FillMode,
    Order,
    OrderEvent,
    OrderType,
)


@dataclass
class FillModel:
    """Simple fill model configurable between the two supported modes.

    Args:
        mode: One of the ``FillMode`` values.
        commission_per_order: Flat fee charged per filled order. Defaults to
            $1.00 which matches the ``InteractiveBrokersFeeModel`` used by
            LEAN for SPY in the reference backtest (total fees ~$126 for 126
            order events).
        slippage_per_share: Applied to the fill price against the trade
            direction. Zero by default (matches LEAN's default
            ``ConstantSlippageModel(0)`` for equities).
    """

    mode: FillMode = FillMode.SIGNAL_BAR_CLOSE
    commission_per_order: Decimal = Decimal("1.00")
    slippage_per_share: Decimal = Decimal(0)

    def fill_market_order(
        self,
        order: Order,
        signal_bar: TradeBar,
        next_bar: Optional[TradeBar] = None,
    ) -> Optional[OrderEvent]:
        """Attempt to fill a market order.

        Args:
            order: The pending market order.
            signal_bar: The bar at whose timestamp the order was placed.
            next_bar: The bar immediately following ``signal_bar``, required
                for ``NEXT_BAR_OPEN`` mode. If None in that mode, the fill is
                deferred (returns None).

        Returns:
            OrderEvent describing the fill, or None if the fill could not be
            produced (e.g., NEXT_BAR_OPEN awaiting the following bar).
        """
        if order.order_type != OrderType.MARKET:
            raise NotImplementedError(
                f"fill_model only supports MARKET orders, got {order.order_type}"
            )

        if self.mode == FillMode.SIGNAL_BAR_CLOSE:
            fill_price = signal_bar.close
            fill_time = signal_bar.end_time
        elif self.mode == FillMode.NEXT_BAR_OPEN:
            if next_bar is None:
                return None
            fill_price = next_bar.open
            fill_time = next_bar.time
        else:
            raise ValueError(f"unknown fill mode: {self.mode}")

        # Apply slippage in the direction of the trade.
        if order.direction == Direction.LONG:
            fill_price = fill_price + self.slippage_per_share
        elif order.direction == Direction.SHORT:
            fill_price = fill_price - self.slippage_per_share

        return OrderEvent(
            order_id=order.order_id,
            symbol=order.symbol,
            time=fill_time,
            fill_price=fill_price,
            fill_quantity=order.quantity,
            direction=order.direction,
            fee=self.commission_per_order,
            tag=order.tag,
        )
