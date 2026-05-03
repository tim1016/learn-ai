"""Pydantic v2 wire models for IBKR data.

Per ``docs/architecture/iv-ownership-research.md`` and the project
``numerical-rigor`` rules:

* All timestamps are ``int64`` ms since Unix epoch UTC. ib_async returns
  ``datetime`` objects; conversion to ms happens at this seam (the
  models module is the boundary where IBKR types become repo types).
* Greeks naming follows the existing engine convention: ``delta``,
  ``gamma``, ``theta`` (per-day, negative for long options), ``vega``
  (per-1-vol-point), and ``iv`` is annualised.
* IBKR can return ``-1`` or ``NaN`` as sentinel "no model" values for
  Greeks and IV. The wire model stores ``None`` in those cases — see
  ``_coerce_optional_float`` (NaN-only) and ``_coerce_iv`` (NaN + any
  negative) for the conversion split.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OptionRight = Literal["C", "P"]


def _coerce_optional_float(value: float | None) -> float | None:
    """Treat IBKR's ``NaN`` sentinel as ``None``.

    ``ib_async`` surfaces "no model could compute this" as ``nan``; we
    funnel that into ``None`` so downstream consumers can rely on
    "value present ⇒ trustworthy number."

    Deliberately does **not** strip ``-1.0`` for the fields routed
    through this helper: a real delta can be ``-1.0`` for a deep ITM
    put, theta is routinely negative, and quote fields can occasionally
    be zero or near-zero in legitimate ways. IV-specific stripping
    (``-1`` and any negative ⇒ ``None``) lives in ``_coerce_iv``.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return float(value)


def _coerce_iv(value: float | None) -> float | None:
    """IV-specific coercion: ``-1.0`` is also a sentinel here."""
    out = _coerce_optional_float(value)
    if out is None:
        return None
    if out < 0.0:
        return None
    return out


SecType = Literal["STK", "OPT", "FUT", "FOP", "CASH", "BOND", "CFD", "WAR", "IND", "BAG"]


class IbkrAccountSummary(BaseModel):
    """Snapshot of an IBKR account.

    The ``account_id`` is what the paper-vs-live sentinel runs against;
    paper account IDs begin with ``DU``.

    Margin and P&L fields are populated from the ``reqAccountSummary``
    tags listed in the Phase 2a doc. Any field IBKR doesn't return for
    the account type (cash accounts have no margin numbers, for example)
    is left ``None``.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    account_id: str
    is_paper: bool = Field(
        ...,
        description="True iff account_id starts with 'DU'.",
    )
    base_currency: str = "USD"
    cash_balance: float | None = None
    net_liquidation: float | None = None

    # ── Margin and buying power (Phase 2a additions) ──────────────────
    buying_power: float | None = None
    init_margin: float | None = None
    maint_margin: float | None = None
    excess_liquidity: float | None = None
    equity_with_loan_value: float | None = None
    available_funds: float | None = None

    # ── Account-level P&L (Phase 2a additions; pnl.py adds streaming) ─
    day_pnl: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None

    fetched_at_ms: int = Field(..., description="UTC milliseconds since epoch.")


class IbkrPosition(BaseModel):
    """One held position. Stocks and options share the same model.

    For options, ``expiry_ms``, ``strike``, and ``right`` are populated
    from the IBKR contract; for stocks they are ``None``. ``quantity``
    is signed — negative for short positions.

    ``avg_cost`` is per-unit *as IBKR reports it*: per share for stocks,
    per contract for options (i.e. already multiplied by 100 for an
    equity option). Consumers reconciling against the engine's
    ``FillModel`` should multiply by ``multiplier`` when comparing to
    a per-share cost basis.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    con_id: int
    symbol: str
    sec_type: SecType
    exchange: str | None = None
    currency: str = "USD"

    # Option-specific. None for non-option securities.
    expiry_ms: int | None = None
    strike: float | None = None
    right: OptionRight | None = None
    multiplier: int = 1

    # Quantity is signed (negative = short). avg_cost is the IBKR-reported
    # cost basis per unit (per share for stocks, per contract for options).
    quantity: float
    avg_cost: float

    # Live mark, populated when ``reqMktData`` has fired at least once
    # for the underlying contract. For positions-only fetches (no live
    # subscription), these stay None — the caller can join against the
    # option-chain stream if they need a live mark.
    market_price: float | None = None
    market_value: float | None = None

    fetched_at_ms: int


class IbkrPositionsSnapshot(BaseModel):
    """All open positions for one account at a moment in time.

    The router returns this directly; the engine's reconciliation pass
    diffs ``positions`` against its own ``PortfolioService`` view.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    is_paper: bool
    positions: list[IbkrPosition]
    fetched_at_ms: int


class IbkrOptionQuote(BaseModel):
    """One option contract's tick snapshot.

    Greeks are sourced from IBKR's ``modelGreeks`` field by default; if
    the ``modelGreeks`` block is missing the producer falls back to
    ``bidGreeks`` / ``askGreeks`` and records that in ``greeks_source``.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiry_ms: int
    strike: float
    right: OptionRight

    # Quote
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None

    # IBKR-computed analytics. May be None when IBKR's model can't compute.
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    underlying_price: float | None = None
    greeks_source: Literal["model", "bid", "ask", "last", "none"] = "none"

    # Stamp of when this snapshot was assembled. Sourced from
    # ``Ticker.time`` if present, else process clock at conversion time.
    ts_ms: int


class IbkrChainSnapshot(BaseModel):
    """A point-in-time slice of one expiry's chain.

    Emitted by the option-chain stream once per debounce window (default
    a few hundred ms). Consumers diff successive snapshots to render an
    animated table.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiry_ms: int
    underlying_price: float | None = None
    quotes: list[IbkrOptionQuote]
    as_of_ms: int


OrderAction = Literal["BUY", "SELL"]
OrderType = Literal["MKT", "LMT"]
OrderTimeInForce = Literal["DAY", "GTC", "IOC", "OPG"]
OrderStatus = Literal[
    "PendingSubmit",
    "PendingCancel",
    "PreSubmitted",
    "Submitted",
    "ApiPending",
    "ApiCancelled",
    "Cancelled",
    "Filled",
    "Inactive",
    "Unknown",
]


class IbkrOrderSpec(BaseModel):
    """Inbound order request from the API.

    Phase 3a supports MKT and LMT only on stocks and US equity options.
    Brackets, OCO, trailing stops are Phase 3b. The
    ``confirm_paper`` field is a defense-in-depth gate: even when
    ``IBKR_MODE=paper`` and the connected account begins with ``DU``,
    the request body must explicitly set ``confirm_paper=true`` for the
    handler to dispatch ``placeOrder``. Phase 4 (live) will require
    ``confirm_live=true`` symmetrically.

    Option fields (``expiry_ms``, ``strike``, ``right``) are required
    when ``sec_type="OPT"`` and ignored when ``sec_type="STK"``.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    sec_type: SecType
    action: OrderAction
    quantity: float = Field(..., gt=0, description="Always positive; 'action' encodes side.")
    order_type: OrderType
    limit_price: float | None = Field(
        default=None,
        gt=0,
        description="Required when order_type='LMT'.",
    )
    time_in_force: OrderTimeInForce = "DAY"

    # Option-only fields
    expiry_ms: int | None = None
    strike: float | None = None
    right: OptionRight | None = None
    multiplier: int = 100

    confirm_paper: bool = Field(
        ...,
        description=(
            "Required True. Defense-in-depth on top of IBKR_MODE and the "
            "DU account-id sentinel."
        ),
    )

    client_order_id: str | None = Field(
        default=None,
        description=(
            "Optional caller-supplied UUID for idempotent retries. If a POST "
            "arrives with a client_order_id we've already seen, the original "
            "ack is returned and no second order is placed. Phase 3b feature; "
            "set None on Phase 3a callers."
        ),
        max_length=64,
    )


OrderEventType = Literal["status", "fill", "cancel", "error"]


class IbkrOrderEvent(BaseModel):
    """One transition on an order's lifecycle.

    Emitted by the order event SSE stream (Phase 3b). The fill case
    carries non-null ``fill_quantity`` and ``avg_fill_price``; the
    error case carries non-null ``error_code`` / ``error_message``.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    order_id: int
    perm_id: int | None = None
    con_id: int | None = None
    event_type: OrderEventType
    status: OrderStatus | None = None

    # Fill payload (event_type == "fill")
    fill_quantity: float | None = None
    avg_fill_price: float | None = None
    cumulative_filled: float | None = None
    remaining: float | None = None
    last_fill_price: float | None = None

    # Error payload (event_type == "error")
    error_code: int | None = None
    error_message: str | None = None

    ts_ms: int


class IbkrOpenOrder(BaseModel):
    """One open order as IBKR currently sees it.

    Returned by ``GET /api/broker/orders/open``; mirrors the in-flight
    state of a previously-placed order (status, partial fills, remaining
    quantity).
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    order_id: int
    perm_id: int | None = None
    client_id: int
    con_id: int
    symbol: str
    sec_type: SecType
    action: OrderAction
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    time_in_force: OrderTimeInForce
    status: OrderStatus
    cumulative_filled: float = 0.0
    remaining: float = 0.0
    avg_fill_price: float | None = None
    fetched_at_ms: int


class IbkrOrderAck(BaseModel):
    """Synchronous acknowledgement of a placed order.

    The handler returns this immediately after ``IB.placeOrder`` returns
    a Trade. Status updates after this point arrive on Phase 3b's order
    event stream.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    is_paper: bool
    order_id: int
    perm_id: int | None = None
    client_id: int
    con_id: int
    symbol: str
    action: OrderAction
    quantity: float
    order_type: OrderType
    limit_price: float | None = None
    status: OrderStatus
    placed_at_ms: int


class IbkrPnLTick(BaseModel):
    """One P&L update from IBKR (account-level or per-position).

    Account-level ticks have ``con_id=None`` and ``position=None``. Per-
    position ticks carry the contract id and signed quantity. ``daily_pnl``
    is the day-rolled change; ``unrealized_pnl`` and ``realized_pnl`` are
    cumulative since position open.

    All P&L numbers are in the account's base currency. Phase 2 is USD-
    only; multi-currency is a separate ticket.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    con_id: int | None = Field(
        default=None,
        description="None for account-level ticks; contract id for per-position.",
    )
    daily_pnl: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    market_value: float | None = None
    position: float | None = None
    ts_ms: int


class IbkrConnectionHealth(BaseModel):
    """Diagnostic snapshot used by ``GET /api/broker/health``.

    The router never raises on disconnect; it returns this with
    ``connected=False`` so the UI can render the disconnected state and
    surface a reconnect button.
    """

    model_config = ConfigDict(frozen=True)

    mode: Literal["paper", "live"]
    host: str
    port: int
    client_id: int
    connected: bool
    account_id: str | None = None
    is_paper: bool | None = None
    server_version: int | None = None
    fetched_at_ms: int


__all__ = [
    "IbkrAccountSummary",
    "IbkrChainSnapshot",
    "IbkrConnectionHealth",
    "IbkrOpenOrder",
    "IbkrOptionQuote",
    "IbkrOrderAck",
    "IbkrOrderEvent",
    "IbkrOrderSpec",
    "IbkrPnLTick",
    "IbkrPosition",
    "IbkrPositionsSnapshot",
    "OptionRight",
    "OrderAction",
    "OrderEventType",
    "OrderStatus",
    "OrderTimeInForce",
    "OrderType",
    "SecType",
    "_coerce_iv",
    "_coerce_optional_float",
]
