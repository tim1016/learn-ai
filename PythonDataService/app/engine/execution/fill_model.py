"""Fill models for market orders.

Three modes are supported:

* ``SIGNAL_BAR_CLOSE`` — the order fills at ``bar.close`` of the consolidated
  bar that triggered it. This reproduces the bookkeeping inside LEAN's
  ``SpyEmaCrossoverAlgorithm.OnFifteenMinuteBar``, where ``_entryPrice`` is
  set to ``bar.Close`` on the signal bar. Use this for exact replication of
  the LEAN trade log. Matrix parity runs may additionally enable
  ``fill_stale_signal_at_current_open`` to match LEAN's equity market-order
  behavior when a consolidated signal bar is emitted only after a
  session/data gap.

* ``NEXT_BAR_OPEN`` — the order fills at the open of the bar *after* the
  signal bar. This is closer to LEAN's actual ``EquityFillModel.MarketFill``
  behavior for backtests without tick data, where
  ``GetBestEffortTradeBar`` returns the next available bar whose ``EndTime``
  is strictly after the order time.

* ``NEXT_SESSION_OPEN`` — the QC precomputed-predictions parity case: signal
  at end of day T-1 (daily-consolidated bar's close), fill at the first
  minute bar of day T. The fill is deferred (returns None) for any candidate
  bar whose NY-local trading date is not strictly after the signal bar's
  trading date. The first eligible candidate bar's ``open`` is used as the
  fill price.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.engine.data.trade_bar import TradeBar
from app.engine.execution.commission import IbkrEquityCommissionModel
from app.engine.execution.order import (
    Direction,
    FillMode,
    Order,
    OrderEvent,
    OrderType,
)

# Set of FillModes whose fill_market_order may return None waiting for a
# subsequent candidate bar. The engine's main loop uses this set to gate
# both the pending-fills retry loop (Step 3) and the order-drain branch
# (Step 5). Single source of truth — keep in lockstep with the FillMode
# enum (see test_deferred_fill_modes_membership_invariant).
DEFERRED_FILL_MODES: frozenset[FillMode] = frozenset({FillMode.NEXT_BAR_OPEN, FillMode.NEXT_SESSION_OPEN})


@dataclass
class FillModel:
    """Simple fill model configurable between the three supported modes.

    Args:
        mode: One of the ``FillMode`` values.
        commission_per_order: Legacy flat fee. Used when ``fee_model`` is
            None — pre-matrix SPY parity fixtures still rely on it. New
            fixtures pin a fee_model and ignore this field.
        slippage_per_share: Applied against the trade direction.
        fee_model: Optional per-fill fee model
            (:class:`IbkrEquityCommissionModel`). When set,
            ``compute_fee(quantity, fill_price)`` returns
            ``fee_model.fee(quantity, fill_price)`` and ``commission_per_order``
            is ignored. This is the single seam through which the matrix
            cells charge IBKR equity-tier commission.
        fill_stale_signal_at_current_open: Optional LEAN equity-market-order
            compatibility path. When ``SIGNAL_BAR_CLOSE`` is selected and the
            current minute bar starts after the signal bar's ``end_time``
            (e.g. Friday's 15:45-16:00 consolidated bar emits on Monday's
            first minute), fill at the current minute's open and timestamp
            the event at that minute's ``end_time``. Disabled by default so
            existing research/backtest behavior stays byte-identical.
    """

    mode: FillMode = FillMode.SIGNAL_BAR_CLOSE
    commission_per_order: Decimal = Decimal("1.00")
    slippage_per_share: Decimal = Decimal(0)
    fee_model: IbkrEquityCommissionModel | None = None
    fill_stale_signal_at_current_open: bool = False

    def compute_fee(self, *, quantity: int, fill_price: Decimal) -> Decimal:
        """Return the fee for a single fill. Always quantized to cents."""
        if self.fee_model is not None:
            return self.fee_model.fee(quantity=int(quantity), fill_price=fill_price)
        return self.commission_per_order

    def fill_market_order(
        self,
        order: Order,
        signal_bar: TradeBar,
        next_bar: TradeBar | None = None,
        current_bar: TradeBar | None = None,
    ) -> OrderEvent | None:
        """Attempt to fill a market order.

        Args:
            order: The pending market order.
            signal_bar: The bar at whose timestamp the order was placed.
            next_bar: The bar immediately following ``signal_bar``, required
                for ``NEXT_BAR_OPEN`` mode. If None in that mode, the fill is
                deferred (returns None).
            current_bar: The engine's current minute bar. Used only by the
                opt-in LEAN stale-signal path for ``SIGNAL_BAR_CLOSE``.

        Returns:
            OrderEvent describing the fill, or None if the fill could not be
            produced (e.g., NEXT_BAR_OPEN awaiting the following bar).
        """
        if order.order_type != OrderType.MARKET:
            raise NotImplementedError(f"fill_model only supports MARKET orders, got {order.order_type}")

        if self.mode == FillMode.SIGNAL_BAR_CLOSE:
            if (
                self.fill_stale_signal_at_current_open
                and current_bar is not None
                and signal_bar.end_time < current_bar.time
            ):
                fill_price = current_bar.open
                fill_time = current_bar.end_time
            else:
                fill_price = signal_bar.close
                fill_time = signal_bar.end_time
        elif self.mode == FillMode.NEXT_BAR_OPEN:
            if next_bar is None:
                return None
            fill_price = next_bar.open
            fill_time = next_bar.time
        elif self.mode == FillMode.NEXT_SESSION_OPEN:
            if next_bar is None:
                return None
            # Eligibility: candidate bar must belong to a trading date STRICTLY
            # AFTER the signal bar's trading date (NY-local). Minimal
            # implementation for regular-hours-only fixtures. A future
            # EligibilityPolicy would replace this date comparison without
            # changing the contract: "first eligible minute bar after the
            # signal bar's trading date." Both .end_time and .time are
            # tz-aware (set by FixtureDataReader and LeanMinuteDataReader);
            # .date() returns the NY-local calendar date.
            if next_bar.time.date() <= signal_bar.end_time.date():
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
            fee=self.compute_fee(quantity=int(order.quantity), fill_price=fill_price),
            tag=order.tag,
        )
