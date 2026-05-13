"""Order and OrderEvent types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class Direction(Enum):
    LONG = 1
    FLAT = 0
    SHORT = -1


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"


class FillMode(Enum):
    """Controls where market orders fill.

    SIGNAL_BAR_CLOSE: Fill at the close of the bar that triggered the order.
        This matches the bookkeeping recorded in LEAN's algorithm trade log
        (``_entryPrice = bar.Close`` inside ``OnFifteenMinuteBar``).

    NEXT_BAR_OPEN: Fill at the open of the bar *after* the signal bar.
        Closer to LEAN's actual fill model for equity market orders when no
        tick data is available. Used for realistic backtesting.

    NEXT_SESSION_OPEN: Fill at the open of the first eligible minute bar
        whose trading date is strictly after the signal bar's trading date
        (NY-local). Designed for the daily-consolidator-over-minute-stream
        pattern (e.g. QC precomputed-predictions parity): the strategy
        triggers at end of day T-1's consolidated bar; the order fills at
        the first minute of day T. "Eligible" today means any regular-hours
        bar; a future EligibilityPolicy may add pre/post-market handling.
    """

    SIGNAL_BAR_CLOSE = "signal_bar_close"
    NEXT_BAR_OPEN = "next_bar_open"
    NEXT_SESSION_OPEN = "next_session_open"


@dataclass
class Order:
    order_id: int
    symbol: str
    quantity: int
    order_type: OrderType
    time: datetime
    direction: Direction
    tag: str = ""
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    # Optional bracket attached to an entry order. When either is set, the
    # engine registers a post-fill watcher that evaluates the bracket
    # against every subsequent fired bar via the pessimistic intrabar
    # resolver (app.engine.execution.intrabar_resolver). Brackets on exit
    # orders are ignored — brackets only make sense on entries.
    take_profit_price: Decimal | None = None
    stop_loss_price: Decimal | None = None


@dataclass
class OrderEvent:
    """Fired when an order is filled (or partially filled)."""

    order_id: int
    symbol: str
    time: datetime
    fill_price: Decimal
    fill_quantity: int
    direction: Direction
    fee: Decimal
    tag: str = ""
