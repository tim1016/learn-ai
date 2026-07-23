"""Broker-neutral contract models (Broker System v2, Layer 3).

These are the **only** broker types allowed to cross the router boundary — no
vendor SDK object escapes ``app/broker/<vendor>/``. Every model is Pydantic v2,
snake_case (the .NET/Angular consumers expect snake_case), and constructed by a
vendor adapter at the ingestion boundary.

Two conventions are load-bearing:

- **Time is ``int64`` ms UTC.** Every temporal field is an integer count of
  milliseconds since the Unix epoch, per ``.claude/rules/temporal-rigor.md``.
  The adapter is the single conversion boundary: vendor RFC-3339 strings become
  ``int64`` ms there, exactly once. Fields carry the ``_ms`` suffix.
- **Money and quantity are ``float``.** These are broker-reported figures for a
  read-only display surface (phase 1), not ported math or backtest PnL, so the
  numerical-rigor Decimal discipline does not apply; ``float`` matches the IBKR
  model precedent and serializes cleanly to the JSON consumers. The verbatim
  decimal strings are preserved losslessly in the capture journal regardless.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _ContractModel(BaseModel):
    """Base for contract models: reject unknown fields to catch adapter typos."""

    model_config = ConfigDict(extra="forbid")


class BrokerAccountSnapshot(_ContractModel):
    """Account-level state for the account card (equity/cash/buying power)."""

    broker: str
    account_id: str
    account_status: str
    currency: str
    cash: float
    equity: float
    buying_power: float
    portfolio_value: float
    long_market_value: float
    short_market_value: float
    pattern_day_trader: bool
    trading_blocked: bool
    account_blocked: bool
    created_at_ms: int | None
    observed_at_ms: int


class BrokerPosition(_ContractModel):
    """A single open position (symbol, quantity, entry, value, unrealized PnL)."""

    broker: str
    symbol: str
    asset_id: str | None
    asset_class: str | None
    quantity: float
    side: str
    average_entry_price: float
    market_value: float
    cost_basis: float
    current_price: float | None
    unrealized_pl: float
    unrealized_plpc: float | None
    observed_at_ms: int


class BrokerOrderEvent(_ContractModel):
    """A lifecycle event on an order (fill/partial-fill/cancel/...).

    Phase-1 REST orders carry only their own lifecycle timestamps, from which
    the adapter synthesizes a fill event when the order reports a fill. The
    richer per-event stream arrives with the phase-2 ``trade_updates`` consumer.
    """

    event_type: str
    occurred_at_ms: int
    price: float | None
    quantity: float | None


class BrokerOrder(_ContractModel):
    """An order and its status, for the recent-orders table."""

    broker: str
    order_id: str
    client_order_id: str | None
    symbol: str
    asset_class: str | None
    side: str
    order_type: str
    time_in_force: str
    quantity: float | None
    filled_quantity: float
    limit_price: float | None
    stop_price: float | None
    filled_avg_price: float | None
    status: str
    submitted_at_ms: int | None
    created_at_ms: int | None
    updated_at_ms: int | None
    filled_at_ms: int | None
    canceled_at_ms: int | None
    expired_at_ms: int | None
    events: list[BrokerOrderEvent] = Field(default_factory=list)
    observed_at_ms: int


class BrokerActivity(_ContractModel):
    """An account activity row (trade fills and non-trade events)."""

    broker: str
    activity_id: str
    activity_type: str
    category: str | None
    symbol: str | None
    side: str | None
    quantity: float | None
    price: float | None
    net_amount: float | None
    occurred_at_ms: int | None
    observed_at_ms: int


class BrokerAsset(_ContractModel):
    """A tradable (or listed) instrument descriptor."""

    broker: str
    asset_id: str
    symbol: str
    name: str | None
    asset_class: str
    exchange: str | None
    status: str
    tradable: bool
    fractionable: bool
    shortable: bool | None
    marginable: bool | None


class BrokerClockEvidence(_ContractModel):
    """Vendor clock/calendar reading — **evidence only, never authority**.

    The canonical calendar module (``.claude/rules/temporal-rigor.md``) remains
    the sole source of scheduled session structure. This model records what the
    broker *claims* about market state so it can be displayed and, later,
    compared against the calendar in a parity diagnostic. Nothing in session or
    calendar logic may read these fields as authoritative.
    """

    broker: str
    is_open: bool
    vendor_timestamp_ms: int
    next_open_ms: int | None
    next_close_ms: int | None
    observed_at_ms: int
