"""Order and OrderEvent types."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from app.utils.timestamps import to_ms_utc


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


@dataclass(init=False)
class Order:
    order_id: int
    symbol: str
    quantity: int
    order_type: OrderType
    submitted_at_ms: int
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

    def __init__(
        self,
        *,
        order_id: int,
        symbol: str,
        quantity: int,
        order_type: OrderType,
        direction: Direction,
        submitted_at_ms: int | None = None,
        time: datetime | None = None,
        tag: str = "",
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
    ) -> None:
        """Create an order with the canonical numeric submission timestamp.

        ``time`` remains a temporary historical-fixture input alias. It is
        normalized on construction and never retained by the order object.
        """
        normalized_ms = _require_timestamp_ms(submitted_at_ms, time, "submitted_at_ms")
        self.order_id = order_id
        self.symbol = symbol
        self.quantity = quantity
        self.order_type = order_type
        self.submitted_at_ms = normalized_ms
        self.direction = direction
        self.tag = tag
        self.limit_price = limit_price
        self.stop_price = stop_price
        self.take_profit_price = take_profit_price
        self.stop_loss_price = stop_loss_price


@dataclass(init=False)
class OrderEvent:
    """Fired when an order is filled (or partially filled)."""

    order_id: int
    symbol: str
    filled_at_ms: int
    fill_price: Decimal
    fill_quantity: int
    direction: Direction
    fee: Decimal
    tag: str = ""
    # The commission the broker actually reported for this fill, or ``None``
    # when not yet reported. ``fee`` above is portfolio-facing (0 when unknown,
    # so cash math never sees NaN); ``recorded_fee`` preserves the unknown for
    # the execution artifact so a missing commission is never written as a
    # fabricated zero (PRD-B). Only the live IBKR fill path sets this; backtest
    # fills leave it ``None`` (they do not flow through the live receipt writer).
    recorded_fee: Decimal | None = None
    # Execution provenance for the live receipt (PRD-A schema, PRD-C shadow).
    # Real broker fills default to ``broker_fill``; the NoSubmitBrokerAdapter
    # stamps ``shadow_sim`` + the source bar it synthesised the fill from so
    # the receipt writer can never confuse simulated fills with real ones.
    execution_source: str = "broker_fill"
    fill_model: str = "NEXT_BAR_OPEN"
    source_bar_close_ms: int | None = None
    # Broker-primary-key identifiers carried through to the receipt. When set
    # (shadow fills carry the simulator's ``shadow:``-prefixed ids), the writer
    # uses them verbatim so the shadow invariant — simulated exec_ids can never
    # collide with a real IBKR execId — survives into executions.parquet. When
    # None (real broker fills today), the writer falls back to synthesised ids.
    exec_id: str | None = None
    client_order_id: str | None = None
    # VCR-P3-L — broker-reported execution time, distinct from ``filled_at_ms``
    # (which is the engine's wall-clock observation moment for live fills,
    # or the bar time for shadow / backtest fills). Set only on live broker
    # fills (sourced from ``IbkrOrderEvent.exec_time_ms``); ``None`` for
    # backtests and the shadow simulator. Surfaced into ``executions.parquet``
    # so latency analysis and reconciliation joins have the authoritative
    # broker time, not the receipt-side wall-clock.
    exec_time_ms: int | None = None

    def __init__(
        self,
        *,
        order_id: int,
        symbol: str,
        fill_price: Decimal,
        fill_quantity: int,
        direction: Direction,
        fee: Decimal,
        filled_at_ms: int | None = None,
        time: datetime | None = None,
        tag: str = "",
        recorded_fee: Decimal | None = None,
        execution_source: str = "broker_fill",
        fill_model: str = "NEXT_BAR_OPEN",
        source_bar_close_ms: int | None = None,
        exec_id: str | None = None,
        client_order_id: str | None = None,
        exec_time_ms: int | None = None,
    ) -> None:
        """Create a fill event with the canonical numeric fill timestamp."""
        self.order_id = order_id
        self.symbol = symbol
        self.filled_at_ms = _require_timestamp_ms(filled_at_ms, time, "filled_at_ms")
        self.fill_price = fill_price
        self.fill_quantity = fill_quantity
        self.direction = direction
        self.fee = fee
        self.tag = tag
        self.recorded_fee = recorded_fee
        self.execution_source = execution_source
        self.fill_model = fill_model
        self.source_bar_close_ms = source_bar_close_ms
        self.exec_id = exec_id
        self.client_order_id = client_order_id
        self.exec_time_ms = exec_time_ms


def _require_timestamp_ms(value: int | None, legacy: datetime | None, field_name: str) -> int:
    if value is None:
        if legacy is None:
            raise TypeError(f"{field_name} is required")
        value = to_ms_utc(legacy)
    elif legacy is not None:
        raise TypeError(f"received both {field_name} and legacy time")
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer Unix millisecond value")
    return value
