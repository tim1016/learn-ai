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
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.safety_verdict import BrokerSafetyVerdict

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
            "so it can warn when nearing IBKR's ~100-line per-client cap."
        ),
    )
    as_of_ms: int


class IbkrMinuteBar(BaseModel):
    """One closed 1-minute TRADES bar from IBKR real-time bars.

    IBKR delivers 5-second bars via ``reqRealTimeBars``. The broker
    boundary aggregates those into closed 1-minute bars and stores all
    boundary timestamps as ``int64`` ms UTC.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    start_ms: int = Field(..., description="UTC milliseconds since epoch, inclusive.")
    end_ms: int = Field(..., description="UTC milliseconds since epoch, exclusive.")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    fetched_at_ms: int


class IbkrBarsSnapshot(BaseModel):
    """A snapshot of the live 1-min OHLCV ring buffer for one symbol.

    ``status`` reports the aggregator's subscription health so the UI can
    show "Subscribing…" / "Streaming" / "Error: …" instead of an
    inscrutable empty chart.
    """

    symbol: str
    status: Literal["idle", "subscribing", "streaming", "errored", "resubscribing"]
    last_error: str | None = None
    last_bar_ms: int | None = None
    bars: list[IbkrMinuteBar] = Field(default_factory=list)


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
        description=("Required True. Defense-in-depth on top of IBKR_MODE and the DU account-id sentinel."),
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

    # ADR 0008 / Phase 5A — deterministic ``{namespace}:{intent_id}`` token
    # the broker echoes back on every order callback. Lets the WAL and the
    # IBKR audit be joined unambiguously even after a restart. ``None`` for
    # legacy callers (replay / explicit-surface tests) so the surface stays
    # backwards-compatible while the production submit path is rewired.
    order_ref: str | None = Field(
        default=None,
        description=(
            "ADR 0008 / Phase 5A. Deterministic ``{bot_order_namespace}:"
            "{intent_id}`` stamped on every managed broker order. The IBKR "
            "Gateway echoes it back on order callbacks; the runtime joins "
            "fills / cancels by it. ``None`` only on legacy / pre-Phase-5A "
            "callers; future durable-submit activation refuses requests "
            "without it."
        ),
        max_length=120,
    )


OrderEventType = Literal["status", "fill", "cancel", "error"]


class IbkrOrderEvent(BaseModel):
    """One transition on an order's lifecycle.

    Emitted by the order event SSE stream (Phase 3b). The fill case
    carries non-null ``fill_quantity`` and ``avg_fill_price``; the
    error case carries non-null ``error_code`` / ``error_message``.

    ``exec_id`` and ``client_id`` are populated on fill events so the
    live runtime's § 7 fatal-halt check can index by broker primary
    keys (``execId`` is IBKR's globally unique execution identifier,
    ``clientId`` lets the runtime see when a fill was placed by some
    other client — including a manual TWS click — under the same DU
    account). Both are ``None`` for non-fill events.
    """

    model_config = ConfigDict(frozen=True)

    account_id: str
    order_id: int
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


DiagnosticStatus = Literal["pass", "warn", "fail", "skip"]


class DiagnosticCheck(BaseModel):
    """One step in the broker connection self-test.

    The ``status`` is the operator-facing verdict; ``detail`` reports what
    we observed; ``fix`` carries a remediation hint when the check is not
    passing. ``fix`` is ``None`` for ``pass`` (nothing to do) and may be
    ``None`` for ``warn`` when the warning is informational only.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Stable identifier for the check (e.g. 'tcp_reachable').")
    label: str = Field(..., description="Human-readable check name shown to operators.")
    status: DiagnosticStatus
    detail: str
    fix: str | None = None


class DiagnosticReportActive(BaseModel):
    """Active broker diagnostic report (broker connection is enabled)."""

    model_config = ConfigDict(frozen=True)

    disabled: Literal[False] = False
    overall_status: Literal["pass", "warn", "fail"]
    checks: list[DiagnosticCheck]
    fetched_at_ms: int


class DiagnosticReportDisabled(BaseModel):
    """Broker disabled diagnostic report (IBKR_BROKER_ENABLED=false)."""

    model_config = ConfigDict(frozen=True)

    disabled: Literal[True]
    reason: str
    since_ms: int


DiagnosticReport = Annotated[
    DiagnosticReportActive | DiagnosticReportDisabled,
    Field(discriminator="disabled"),
]


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
    "disconnected",
    "disabled",
]
"""Wire-level state surfaced to the cockpit. Strict superset of
``ClientConnectionState`` with the monitor and env-driven values."""


class IbkrConnectionHealth(BaseModel):
    """Diagnostic snapshot used by ``GET /api/broker/health``.

    The router never raises on disconnect; it returns this with
    ``connected=False`` so the UI can render the disconnected state and
    surface a reconnect button.

    Phase 7A / VCR-0010 / ADR 0011 — ``safety_verdict`` is the structured
    paper-mode safety verdict the cockpit hero binds to. Derivation is
    fail-closed: the hero never claims ``paper-only`` unless every gate
    positively confirms it.
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
    safety_verdict: BrokerSafetyVerdict | None = None
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
    "ClientConnectionState",
    "DiagnosticCheck",
    "DiagnosticReport",
    "DiagnosticReportActive",
    "DiagnosticReportDisabled",
    "DiagnosticStatus",
    "IbkrAccountSummary",
    "IbkrChainSnapshot",
    "IbkrConnectionHealth",
    "IbkrMinuteBar",
    "IbkrOpenOrder",
    "IbkrOptionQuote",
    "IbkrOrderAck",
    "IbkrOrderEvent",
    "IbkrOrderSpec",
    "IbkrPnLTick",
    "IbkrPosition",
    "IbkrPositionsSnapshot",
    "IbkrStrikeList",
    "IbkrSurfaceExpiry",
    "IbkrSurfaceSnapshot",
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
