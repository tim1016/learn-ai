"""Contract schemas for the broker-v2 bot control panel (S1).

Backend-authored, broker-generic Pydantic models the Angular panel renders
strictly from (spec §4, §5, §7, §8, §11). Everything here is a projection over
the S0 evidence + durable lifecycle artifacts + the clerk's journal-derived
state; no schema derives display prose the frontend must invent.

Temporal fields are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
Every operator code (phase, verdict, station id/state, action id, ...) comes
from the closed vocabulary in ``app.broker.v2panel.vocabulary`` and carries
server-authored ``label`` / ``explanation`` copy so no raw enum reaches the UI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.v2panel.vocabulary import (
    ActionId,
    ChannelState,
    DesiredState,
    DutyOutcomeKind,
    HoldReason,
    Phase,
    ReconciliationVerdict,
    StationId,
    StationState,
)
from app.schemas.operator_blocker import OperatorBlocker, OperatorConfirmationCopy

# ── §4 Panel capability profile ──────────────────────────────────────────────


class StationApplicability(BaseModel):
    """One station's applicability for this broker/mode (§4, §7).

    ``applicable=False`` renders the station in the rail's fifth state
    (``not_applicable``); Angular never guesses which stations a broker has.
    """

    model_config = ConfigDict(frozen=True)

    station_id: StationId
    applicable: bool
    label: str
    explanation: str


class PanelProfile(BaseModel):
    """Closed descriptor extending BrokerCapabilities for the panel (§4).

    Angular renders strictly from this: an inapplicable station renders as
    ``not_applicable``; an unsupported action never renders at all. Snapshot-
    contract-tested per broker.
    """

    model_config = ConfigDict(frozen=True)

    broker: str
    # Fee-reporting fidelity for this broker's fills (§10). "none" → the panel
    # renders "Fees not reported", never $0.00.
    fee_fidelity: Literal["per_fill", "aggregate", "none"]
    # Whether Flatten & stop is available (§12). Alpaca paper: True.
    flatten_supported: bool
    # Whether the broker has a native live-bar strain for the LIVE chart pane
    # (§8). Alpaca phase-1: False — the LIVE pane uses the IBKR bridge +
    # Polygon fallback (ADR 0032 amendment).
    live_bars_supported: bool
    # Which stations apply to this broker/mode (§7).
    stations: list[StationApplicability]
    # The action ids this broker supports (§11). An action not listed here is
    # never rendered.
    supported_action_ids: list[ActionId]


# ── §5 Catalog view (bots list roster row) ───────────────────────────────────


class BotCatalogView(BaseModel):
    """One roster row: bot status + slice-0 rollups (§5).

    Assembled from the ``BotStatusView`` (lifecycle) + the S0 ``BotRollup``
    (incremental cache). ``needs_attention`` and ``status_label`` drive the
    attention-first sort and the closed status vocabulary. No journal scan per
    request.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    broker: str
    account_id: str
    symbol: str
    phase: Phase
    desired_state: DesiredState
    running: bool
    # Closed status label from the vocabulary: Working / Off duty / Retired.
    status_label: str
    # Rollups (S0 BotRollup) — never account-net; attributed to this bot.
    exposure: dict[str, float]
    fills_today: int
    realized_pnl_today: float
    open_pnl: float | None
    last_activity_at_ms: int | None
    needs_attention: bool


# ── §7 Panel view (single bot control panel) ─────────────────────────────────


class DutyOutcomeView(BaseModel):
    """Typed terminal duty fact shown on the bot-health card (§7.2)."""

    model_config = ConfigDict(frozen=True)

    kind: DutyOutcomeKind
    reason_code: str
    label: str
    explanation: str
    recorded_at_ms: int | None
    run_id: str | None


class BotHealthCard(BaseModel):
    """Bot-health card beside the rail (§7.2).

    ``desired_state`` is narrowed to ``RUNNING | STOPPED`` — the panel never
    emits ``PAUSED`` (decision #10; pinned by a contract test).
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    phase: Phase
    phase_label: str
    desired_state: DesiredState
    desired_state_label: str
    running: bool
    duty_outcome: DutyOutcomeView | None
    # Decision-receipt freshness (§9): the ts of the latest decision receipt,
    # and whether it is stale against the freshness threshold.
    last_decision_at_ms: int | None
    decision_stale: bool
    # Last bar seen (§7.2): the most recent bar this bot evaluated.
    last_bar_at_ms: int | None
    resume_eligible: bool
    resume_label: str
    resume_explanation: str
    carryover_checkpoint_exposure: dict[str, float]


class ChannelHealthView(BaseModel):
    """One market-data / execution channel's health (§7.3)."""

    model_config = ConfigDict(frozen=True)

    stream: Literal["market_data", "execution"]
    state: ChannelState
    label: str
    explanation: str
    reason: str
    observed_at_ms: int


class ClerkCard(BaseModel):
    """Account/clerk card beside the rail (§7.3)."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    hold_active: bool
    hold_reason: HoldReason
    hold_reason_label: str
    hold_reason_explanation: str
    hold_since_ms: int | None
    freeze_active: bool
    freeze_category: Literal[
        "ACCOUNT_STATE_UNATTRIBUTABLE",
        "ACCOUNT_STATE_UNPROVABLE",
    ] | None
    freeze_label: str
    freeze_explanation: str
    freeze_next_step: str | None
    freeze_observed_at_ms: int | None
    reconciliation_verdict: ReconciliationVerdict | None
    reconciliation_verdict_label: str | None
    last_sweep_at_ms: int | None
    outstanding_intents: int
    channels: list[ChannelHealthView]


class StationView(BaseModel):
    """One of the six transaction-rail stations (§7.1).

    ``state`` is one of the five states; ``blocker`` is populated only when
    ``state == "blocked"`` (carries the reused OperatorBlocker contract).
    ``evidence_at_ms`` is the ts of the evidence backing this station, if any.
    """

    model_config = ConfigDict(frozen=True)

    station_id: StationId
    label: str
    state: StationState
    state_label: str
    receipt: str
    evidence_at_ms: int | None
    blocker: OperatorBlocker | None


class TransactionRail(BaseModel):
    """The six-station rail rendering one selected transaction (§7.1)."""

    model_config = ConfigDict(frozen=True)

    # The intent/order this rail renders (most recent by default; a journal-tail
    # row can select another). ``None`` when the bot has no transaction yet.
    transaction_ref: str | None
    stations: list[StationView]


class PanelAction(BaseModel):
    """One backend-presented action (§11).

    Angular executes only the closed set of known action ids and renders
    exactly what it is given. ``revision`` binds the action to the panel-state
    revision it was presented against; a stale POST is a 409.
    """

    model_config = ConfigDict(frozen=True)

    action_id: ActionId
    label: str
    explanation: str
    enabled: bool
    blockers: list[OperatorBlocker]
    confirmation: OperatorConfirmationCopy | None
    revision: int
    # An action-scoped optimistic-concurrency token.  This deliberately is
    # narrower than the display revision: a new chart point or journal receipt
    # must not make a presented STOP falsely stale.
    concurrency_token: str


class BotPanelView(BaseModel):
    """The full 5s-poll panel projection for one bot (§7).

    Everything except chart data: bot health, clerk/account state, the six-
    station rail, a journal-tail reference, the presented actions, and the
    panel-state ``revision`` those actions bind to.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    broker: str
    account_id: str
    symbol: str
    mode: Literal["log_only", "trade"]
    revision: int
    health: BotHealthCard
    clerk: ClerkCard
    rail: TransactionRail
    # Journal-tail reference (§7.4): the bounded read endpoint + the newest seq
    # the panel observed, so the tail component knows where to page from.
    journal_tail_ref: str
    journal_tail_seq: int | None
    actions: list[PanelAction]
    # S0 rollup summary — backend-computed FIFO P&L (§10).  Frontend renders,
    # never recomputes.  "Fees not reported" renders when fee_fidelity="none".
    fills_today: int
    realized_pnl_today: float
    open_pnl: float | None


# ── §11 Presented-actions execution request/response ─────────────────────────


class PanelActionRequest(BaseModel):
    """Execute one presented action (§11).

    Identity is NEVER a request field — it derives from the authenticated
    control channel (§14). The form carries only the reason.
    """

    model_config = ConfigDict(extra="forbid")

    action_id: ActionId
    revision: int = Field(ge=0)
    concurrency_token: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=512)


class PanelActionResult(BaseModel):
    """The outcome of an executed action (§11).

    On success the caller re-polls the panel; ``applied`` distinguishes a fresh
    application from an idempotent replay (``applied=False`` — the key was seen
    before, the action is a no-op).
    """

    model_config = ConfigDict(frozen=True)

    action_id: ActionId
    applied: bool
    revision: int
    concurrency_token: str
    message: str


# ── §8 Chart shapes ──────────────────────────────────────────────────────────

ChartSource = Literal["ibkr", "polygon", "mixed"]
ChartHistoryPreset = Literal["1D", "5D", "1M", "3M", "1Y", "All"]
ChartAggregation = Literal["1m", "5m", "30m", "1h", "1d"]


class ChartBar(BaseModel):
    """One source-tagged OHLCV bar for a chart pane (§8).

    Broker-generic — decoupled from the IBKR-specific ``IbkrMinuteBar``. Prices
    are strings to preserve exact decimal representation over the wire.
    ``source`` is truthfully tagged ``ibkr`` / ``polygon`` / ``mixed`` (§8).
    """

    model_config = ConfigDict(frozen=True)

    start_ms: int
    end_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: int
    source: ChartSource


class ChartFillMarker(BaseModel):
    """One fill marker for a chart pane (§8, §10).

    Projected from the clerk journal's fill events filtered by the bot's
    namespace. ``side`` and ``price`` render the buy/sell marker.
    """

    model_config = ConfigDict(frozen=True)

    filled_at_ms: int
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    order_ref: str


class ChartOverlayNoticeView(BaseModel):
    """An honest chip explaining a fallback overlay (§8)."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    source: Literal["polygon"] = "polygon"


class ChartLiveResponse(BaseModel):
    """Today's merged, source-tagged LIVE-pane bars + today's fill markers (§8).

    ``as_of_ms`` and the two session boundaries derive from the canonical NY
    calendar — "today" is the NY trading date, never browser-local midnight.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    symbol: str
    trading_date_open_ms: int
    trading_date_close_ms: int
    resolution: Literal["5s", "1m"]
    bars: list[ChartBar]
    fill_markers: list[ChartFillMarker]
    overlay_notices: list[ChartOverlayNoticeView]
    as_of_ms: int


class ChartHistoryResponse(BaseModel):
    """Bounded HISTORY-pane response for a fixed preset (§8).

    The preset → aggregation ladder and the response-size bound are enforced
    server-side; the existing 7-day live resolver is not widened.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    symbol: str
    preset: ChartHistoryPreset
    aggregation: ChartAggregation
    from_ms: int
    to_ms: int
    bars: list[ChartBar]
    fill_markers: list[ChartFillMarker]
    truncated: bool
    as_of_ms: int
