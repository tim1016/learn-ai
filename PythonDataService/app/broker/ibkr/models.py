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
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, computed_field

from app.broker.ibkr.recovery_state_machine import RecoveryState

OptionRight = Literal["C", "P"]


def _coerce_optional_float(value: float | None) -> float | None:
    """Treat IBKR's unset floating-point sentinels as ``None``.

    ``ib_async`` surfaces missing values as either ``nan`` or its
    ``UNSET_DOUBLE`` constant (``sys.float_info.max``). Funnel both into
    ``None`` so downstream consumers can rely on "value present ⇒
    trustworthy number."

    Deliberately does **not** strip ``-1.0`` for the fields routed
    through this helper: a real delta can be ``-1.0`` for a deep ITM
    put, theta is routinely negative, and quote fields can occasionally
    be zero or near-zero in legitimate ways. IV-specific stripping
    (``-1`` and any negative ⇒ ``None``) lives in ``_coerce_iv``.
    """
    if value is None:
        return None
    out = float(value)
    if not math.isfinite(out) or out == sys.float_info.max:
        return None
    return out


def _coerce_iv(value: float | None) -> float | None:
    """IV-specific coercion: ``-1.0`` is also a sentinel here."""
    out = _coerce_optional_float(value)
    if out is None:
        return None
    if out < 0.0:
        return None
    return out


def _coerce_quote(value: float | None) -> float | None:
    """Quote-specific coercion (bid / ask / last): NaN OR negative ⇒ ``None``.

    IBKR sends ``-1.0`` as the "no bid/ask available" sentinel for L1
    quote fields. A real bid can be ``$0.00`` (deep-OTM with no buyer)
    but never negative — there is no rational seller offering free
    options, no rational buyer paying negative dollars. Treating any
    negative value as missing is safe and stops sentinels from
    leaking into mid-price math (where ``(-1 + ask) / 2`` would
    produce a bogus reprice trigger and a "-$1.00" cell in the UI).

    Distinct from ``_coerce_optional_float`` because Greeks like
    ``delta`` legitimately go to ``-1.0`` for deep-ITM puts — that
    helper preserves the value, this one rejects it.
    """
    out = _coerce_optional_float(value)
    if out is None:
        return None
    if out < 0.0:
        return None
    return out


SecType = Literal["STK", "OPT", "FUT", "FOP", "CASH", "BOND", "CFD", "WAR", "IND", "BAG"]
IbkrApiRequestName = Literal[
    "accountSummaryAsync",
    "cancelOrder",
    "placeOrder",
    "cancelMktData",
    "qualifyContractsAsync",
    "reqAllOpenOrders",
    "reqCompletedOrdersAsync",
    "reqContractDetailsAsync",
    "reqCurrentTimeAsync",
    "reqExecutionsAsync",
    "reqHistoricalDataAsync",
    "reqMatchingSymbolsAsync",
    "reqMktData",
    "reqMarketDataType",
    "reqPnL",
    "reqPnLSingle",
    "reqPositionsAsync",
    "reqRealTimeBars",
    "reqSecDefOptParamsAsync",
    "whatIfOrderAsync",
]
IbkrApiCallbackName = Literal[
    "accountSummary",
    "contractDetails",
    "completedOrder",
    "currentTime",
    "error",
    "openOrder",
    "orderStatus",
    "execDetails",
    "historicalData",
    "marketDataType",
    "pnl",
    "pnlSingle",
    "position",
    "realTimeBar",
    "realTimeBarList",
    "securityDefinitionOptionParameter",
    "symbolSamples",
    "tickSnapshot",
    "whatIfOrder",
]


class IbkrObjectSnapshot(BaseModel):
    """JSON-safe snapshot of one ib_async object.

    ``object_type`` records the originating Python type while ``fields`` carries
    every public field we could observe. Datetime fields are converted at the
    broker boundary to ``int64 ms UTC`` to preserve the repo-wide timestamp
    contract.
    """

    model_config = ConfigDict(frozen=True)

    object_type: str
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    serializer_error: str | None = Field(default=None, exclude=True)


class IbkrApiRequestEvidence(BaseModel):
    """Typed envelope for one IBKR API request/call."""

    model_config = ConfigDict(frozen=True)

    call: IbkrApiRequestName
    params: dict[str, JsonValue] = Field(default_factory=dict)


class IbkrSerializerWarning(BaseModel):
    """Structured warning emitted when an IBKR object cannot be fully serialized."""

    model_config = ConfigDict(frozen=True)

    object_type: str
    serializer_error: str


class IbkrApiResponseEvidence(BaseModel):
    """Typed envelope for one IBKR callback/response."""

    model_config = ConfigDict(frozen=True)

    callback: IbkrApiCallbackName
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    serializer_warnings: list[IbkrSerializerWarning] = Field(default_factory=list)


DataPlaneReloadMode = Literal[
    "disabled",
    "watchfiles",
    "watchfiles-polling",
    "unknown",
]


class DataPlaneHealth(BaseModel):
    """Code-liveness metadata for the long-running FastAPI data plane.

    PRD #684 uses this as the operator's fast check for "fixed on disk"
    versus "actually live in the process". All timestamps are int64 ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    service: Literal["polygon-data-service"]
    code_revision: str
    process_start_ms: int = Field(gt=0)
    fetched_at_ms: int = Field(gt=0)
    reload: DataPlaneReloadMode


class IbkrTradeSnapshot(BaseModel):
    """Full ib_async Trade evidence grouped by its child objects."""

    model_config = ConfigDict(frozen=True)

    trade: IbkrObjectSnapshot | None = None
    contract: IbkrObjectSnapshot | None = None
    order: IbkrObjectSnapshot | None = None
    order_status: IbkrObjectSnapshot | None = None
    fills: list[IbkrObjectSnapshot] = Field(default_factory=list)
    log: list[IbkrObjectSnapshot] = Field(default_factory=list)
    advanced_error: str | None = None


class IbkrTradeEvidence(BaseModel):
    """Full IBKR request/response/object evidence for an order lifecycle row."""

    model_config = ConfigDict(frozen=True)

    request: IbkrApiRequestEvidence | None = None
    response: IbkrApiResponseEvidence | None = None
    contract: IbkrObjectSnapshot | None = None
    order: IbkrObjectSnapshot | None = None
    order_status: IbkrObjectSnapshot | None = None
    trade: IbkrTradeSnapshot | None = None
    fill: IbkrObjectSnapshot | None = None
    execution: IbkrObjectSnapshot | None = None
    commission_report: IbkrObjectSnapshot | None = None


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_exposure(self) -> bool:
        """Return the backend-authored exposure/flatness verdict.

        Formula: ``has_exposure = abs(quantity) >= POSITION_QTY_EPSILON``.
        Reference: ADR 0036.
        Canonical implementation:
          app/broker/alpaca/clerk/sqlite/folds.py::position_quantity_is_nonzero.
        Validated against: tests/broker/ibkr/test_account.py::
          test_fetch_positions_authors_canonical_exposure_verdict.
        """
        from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero

        return position_quantity_is_nonzero(self.quantity)


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
    used_cache_fallback: bool = False


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


class IbkrStrikeList(BaseModel):
    """Strikes IBKR has actually instantiated for one (symbol, expiry).

    Distinct from the union returned by ``reqSecDefOptParams``: that
    payload reports every strike listed on *any* expiry of the symbol,
    so a Monday weekly with $5 increments still surfaces every $1 strike
    that exists on a quarterly expiry. This model carries only strikes
    that ``qualifyContractsAsync`` could resolve into real contracts —
    the set the chain UI can safely subscribe to.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    expiry_ms: int
    strikes: list[float]
    fetched_at_ms: int


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


class IbkrSurfaceExpiry(BaseModel):
    """One expiry's slice of an option surface.

    Emitted as part of :class:`IbkrSurfaceSnapshot` — wraps the same
    per-contract quotes as :class:`IbkrChainSnapshot` but groups them by
    expiry so the surface UI can index ``(expiry, strike, right)`` in
    one pass.
    """

    model_config = ConfigDict(frozen=True)

    expiry_ms: int
    quotes: list[IbkrOptionQuote]


class IbkrSurfaceSnapshot(BaseModel):
    """Point-in-time slice of a multi-expiry option surface.

    Emitted by the option-surface stream once per debounce window. The
    surface is a fan-out across N expiries × M strikes × 2 sides; this
    snapshot carries every contract's quote in one envelope so the 3D
    visualizer can re-render without coalescing.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    underlying_price: float | None = None
    expiries: list[IbkrSurfaceExpiry]
    line_count: int = Field(
        ...,
        description=(
            "Number of streaming market-data lines this surface holds open "
            "(underlying + every option contract). Surfaced for the client "
            "so it can warn when nearing IBKR's shared user allocation "
            "(100 lines by default across TWS and all API connections)."
        ),
    )
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


OrderEventType = Literal["status", "fill", "cancel", "error"]


class IbkrOrderEvent(BaseModel):
    """One transition on an order's lifecycle.

    Emitted by the read-only order-event SSE stream. The fill case
    carries non-null ``fill_quantity`` and ``avg_fill_price``; the
    error case carries non-null ``error_code`` / ``error_message``.

    ``exec_id`` and ``client_id`` are populated on fill events for durable
    evidence attribution (``execId`` is IBKR's globally unique execution
    identifier; ``clientId`` distinguishes the client that originated the
    order, including a manual TWS click). Both are ``None`` for non-fill events.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    order_id: int
    req_id: int | None = Field(
        default=None,
        description=(
            "IBKR errorEvent reqId when the event originated from an error "
            "callback. For order-scoped errors IBKR uses the orderId here; "
            "the bot-event stream joins this back to order_ref."
        ),
    )
    perm_id: int | None = None
    con_id: int | None = None
    event_type: OrderEventType
    status: OrderStatus | None = None

    # ADR 0008 / Phase 5A — broker-echoed deterministic
    # ``{bot_order_namespace}:{intent_id}`` token, captured here so the
    # reconciliation publisher can join each callback (status or fill) back to
    # the originating engine intent unambiguously. Set on every event whose
    # underlying ib_async object carries a non-empty ``orderRef``; ``None`` for
    # status events on orders placed before this field shipped, fills with no
    # echoed orderRef (a foreign exec under our account, by definition), and
    # error events without an associated trade.
    order_ref: str | None = None

    # ADR 0014 — operator-facing fields the broker_activity reconciler
    # consumes verbatim. Sourced from the underlying ``Trade.contract``
    # (``symbol``) and ``Trade.order`` (``action`` → ``BUY``/``SELL``;
    # ``orderType``). ``None`` only when the underlying ib_async object is
    # degenerate (no contract, missing action) — defensive optionals so
    # the model stays constructible from old fixtures.
    symbol: str | None = None
    side: Literal["BUY", "SELL"] | None = None
    order_type: str | None = None

    # Fill payload (event_type == "fill")
    exec_id: str | None = None
    client_id: int | None = None
    fill_quantity: float | None = None
    avg_fill_price: float | None = None
    cumulative_filled: float | None = None
    remaining: float | None = None
    last_fill_price: float | None = None
    # Broker execution time (``int64 ms UTC``) read from the underlying
    # ib_async ``Execution.time`` — distinct from ``ts_ms`` (wall-clock
    # observation time). The § 7 outside-mutation check uses this to floor
    # at session start: IBKR replays the day's prior executions at connect,
    # and a foreign fill whose execution time predates this run's session is
    # pre-existing account history, not concurrent contamination. ``None``
    # for non-fill events or when the broker omits the time.
    exec_time_ms: int | None = None
    # Commission for this fill, read from the polled ``Fill.commissionReport``
    # (PRD-B). ``None`` when IBKR has not yet reported the commission for this
    # execId — never a fabricated zero, so a missing fee stays distinguishable
    # from a genuine zero downstream (COMMISSION_MISSING vs COMMISSION_DRIFT).
    fee: float | None = None

    # Error payload (event_type == "error")
    error_code: int | None = None
    error_message: str | None = None

    ibkr_evidence: IbkrTradeEvidence | None = None

    ts_ms: int


class IbkrOrderAck(BaseModel):
    """Historical acknowledgement schema retained for durable journal rows."""

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
    order_ref: str | None = None
    ibkr_evidence: IbkrTradeEvidence | None = None
    placed_at_ms: int


ClientConnectionState = Literal[
    "connected",
    "soft_lost",
    "subscriptions_stale",
    "degraded_data_farm",
    "disconnected",
]
"""Subset of states an ``IbkrClient`` can observe for itself. The monitor's
``reconnecting`` overlay and the env-driven ``disabled`` state are layered on
by ``build_broker_health``."""


BrokerConnectionState = Literal[
    "connected",
    "soft_lost",
    "subscriptions_stale",
    "degraded_data_farm",
    "reconnecting",
    "recovering",
    "hard_down",
    "disconnected",
    "disabled",
]
"""Wire-level state surfaced to the cockpit. Strict superset of
``ClientConnectionState`` with the monitor and env-driven values."""


BrokerHealthConditionCode = Literal[
    "DATA_PLANE_BROKER_CONNECTED",
    "DATA_PLANE_BROKER_DISCONNECTED",
    "DATA_PLANE_BROKER_SOFT_LOST",
    "DATA_PLANE_BROKER_SUBSCRIPTIONS_STALE",
    "DATA_PLANE_BROKER_DATA_FARM_DEGRADED",
    "DATA_PLANE_BROKER_RECONNECTING",
    "DATA_PLANE_BROKER_RECOVERING",
    "DATA_PLANE_BROKER_HARD_DOWN",
    "DATA_PLANE_BROKER_DISABLED",
]


class BrokerHealthCondition(BaseModel):
    """Backend-authored operator copy for the data-plane broker session.

    This is intentionally the data-plane altitude, not a per-bot runtime
    verdict. Per-bot broker proof belongs to the canonical broker projection.
    """

    model_config = ConfigDict(frozen=True)

    code: BrokerHealthConditionCode
    severity: Literal["ok", "info", "warning", "critical"]
    title: str
    summary: str
    remediation: str | None = None


class IbkrConnectionHealth(BaseModel):
    """Diagnostic snapshot used by ``GET /api/broker/health``.

    The router never raises on disconnect; it returns this with
    ``connected=False`` so the UI can render the disconnected state and
    surface a reconnect button.

    The Phase 7A / VCR-0010 / ADR 0011 ``safety_verdict`` field (a
    paper-mode safety verdict tied to the retired account-order-actuation
    cockpit hero) was dropped from this response — PR-B of #1813,
    2026-08-27 — along with ``app/broker/safety_verdict.py``, its sole
    producer. No live consumer read it (verified against Frontend before
    removal).
    """

    model_config = ConfigDict(frozen=True)

    mode: Literal["paper", "live"]
    host: str
    port: int
    client_id: int
    connected: bool
    disabled: bool = False
    reason: str | None = None
    account_id: str | None = None
    is_paper: bool | None = None
    server_version: int | None = None
    fetched_at_ms: int
    condition: BrokerHealthCondition | None = None
    """Backend-authored operator copy for this data-plane broker session.
    Frontend surfaces should render this text rather than inventing their
    own explanation from ``connection_state``."""
    # ── Connection-state machine fields (auto-reconnect, VCR-broker-stability) ──
    # Required: the cockpit binds the link strip to ``connection_state`` and
    # ``last_transition_ms`` directly; every constructor in the codebase sets
    # them, so the typed contract is non-optional. ``connected`` stays bool for
    # back-compat with downstream code that already keys off it.
    connection_state: BrokerConnectionState
    """Cockpit-facing connection state. The single field the link strip binds
    to; richer than ``connected`` (which is still surfaced for back-compat).
    Cockpit derives banner colour and detail string from this."""
    last_transition_ms: int
    """Wall-clock when ``connection_state`` last changed (int64 ms UTC).
    Composed by ``build_broker_health`` as the max of the client's own
    event timestamp and the monitor's last attempt-boundary timestamp."""
    recovery_state: RecoveryState | None = None
    """ADR 0018 recovery state. Monitor-owned when the auto-reconnect
    monitor is installed; otherwise projected from ``connection_state`` for
    compatibility with broker-disabled and unit-test constructors."""
    connection_lost: bool = False
    """Whether IBKR Error 1100 / 504 has fired and not yet been restored.
    The socket may still report ``connected=True`` in this window — the data
    feed is dead."""
    connectivity_lost_count: int = 0
    """Cumulative observable count of connectivity-lost events since the
    process started."""
    reconnect_attempt: int | None = None
    """Current AutoReconnectMonitor attempt number while ``connection_state ==
    "reconnecting"``, ``None`` otherwise. The cockpit renders it as
    "Reconnecting (attempt N)" so the operator sees progress, not silence."""
    successful_reconnect_count: int = 0
    """Cumulative observable count of monitor-driven recoveries this process —
    surfaces in the broker diagnostics for an operator who wants to know
    "how flaky has the bridge been"."""
    last_ibkr_code: int | None = None
    """Most recent IBKR/TWS connectivity or data-farm code observed by the
    client. Used by the UI to distinguish a daily reset from a stale
    subscription or data-farm degradation."""
    last_ibkr_message: str | None = None
    """Message paired with ``last_ibkr_code``."""
    subscriptions_stale: bool = False
    """True after IBKR code 1101 ("data lost") until recovery callbacks have
    resubscribed active streams."""
    data_farm_degraded: bool = False
    """True while market-data or historical-data farm connectivity is
    degraded (e.g. 2103 / 2105 without its matching OK code yet)."""
    last_probe_ms: int | None = None
    """Wall-clock timestamp of the most recent successful app-level broker
    probe."""
    last_probe_error: str | None = None
    """Most recent watchdog probe failure, cleared on probe success."""
    last_recovery_ms: int | None = None
    """Wall-clock timestamp when post-reconnect recovery last completed."""
    recovery_error: str | None = None
    """Most recent post-reconnect recovery failure, cleared on recovery
    success."""


__all__ = [
    "BrokerConnectionState",
    "BrokerHealthCondition",
    "BrokerHealthConditionCode",
    "ClientConnectionState",
    "DataPlaneHealth",
    "DataPlaneReloadMode",
    "IbkrApiCallbackName",
    "IbkrApiRequestEvidence",
    "IbkrApiRequestName",
    "IbkrApiResponseEvidence",
    "IbkrChainSnapshot",
    "IbkrConnectionHealth",
    "IbkrObjectSnapshot",
    "IbkrOptionQuote",
    "IbkrOrderAck",
    "IbkrOrderEvent",
    "IbkrPosition",
    "IbkrPositionsSnapshot",
    "IbkrSerializerWarning",
    "IbkrStrikeList",
    "IbkrSurfaceExpiry",
    "IbkrSurfaceSnapshot",
    "IbkrTradeEvidence",
    "IbkrTradeSnapshot",
    "OptionRight",
    "OrderAction",
    "OrderEventType",
    "OrderStatus",
    "OrderTimeInForce",
    "OrderType",
    "SecType",
    "_coerce_iv",
    "_coerce_optional_float",
    "_coerce_quote",
]
