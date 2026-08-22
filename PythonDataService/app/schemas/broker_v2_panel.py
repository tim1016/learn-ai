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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.v2panel.vocabulary import (
    TRADER_LIFECYCLE_ACTION_IDS,
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
from app.schemas.run_admission import ProgramBuildAdmissionFact, RunAdmissionDecision
from app.schemas.signal_program_seal import SealedBotProgram


def _validate_simulated_authority_metadata(
    *,
    simulated: bool,
    authority_account_id: str | None,
    authority_kind: Literal["real_paper", "synthetic"] | None,
) -> None:
    """Require simulated panel evidence to name its isolated synthetic authority."""
    if simulated and (
        not authority_account_id
        or not authority_account_id.startswith("sim:")
        or authority_kind != "synthetic"
    ):
        raise ValueError(
            "simulated panel rows require nonempty synthetic authority metadata"
        )


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
    # An action-scoped optimistic-concurrency token. This deliberately is
    # narrower than the display revision: a new chart point or journal receipt
    # must not make a presented STOP falsely stale.
    concurrency_token: str
    # Opaque immutable evidence identities for view actions. The client may
    # route these into Clerk timeline filters but never derives a recovery
    # capability from them.
    evidence_refs: list[str] = Field(default_factory=list)


class BotCatalogView(BaseModel):
    """One roster row: bot status + slice-0 rollups (§5).

    Assembled from the ``BotStatusView`` (lifecycle) + the S0 ``BotRollup``
    (incremental cache). ``needs_attention`` and ``status_label`` drive the
    attention-first sort and the closed status vocabulary. No journal scan per
    request.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    strategy_key: str
    strategy_label: str
    broker: str
    account_id: str
    symbol: str
    mode: Literal["log_only", "dry_run", "trade"]
    phase: Phase
    desired_state: DesiredState
    running: bool
    # Closed status label from the vocabulary: Working / Off duty / Retired.
    status_label: str
    # Backend-authored, trader-facing explanation for the row's current state.
    status_explanation: str
    # Rollups (S0 BotRollup) — never account-net; attributed to this bot.
    exposure: dict[str, float]
    fills_today: int | None
    realized_pnl_today: float | None
    open_pnl: float | None
    last_activity_at_ms: int | None
    needs_attention: bool
    # The one routine lifecycle command appropriate for this row. It carries
    # the same server-authored guard/token contract as panel actions so the
    # roster never performs a full-panel preflight before a mutation.
    row_action: PanelAction | None = None


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

    ``PAUSED`` means the current process/run remains live while bar delivery
    is held. Continue retains that run identity; Resume is not applicable.
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
    freeze_category: (
        Literal[
            "ACCOUNT_STATE_UNATTRIBUTABLE",
            "ACCOUNT_STATE_UNPROVABLE",
        ]
        | None
    )
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


class PrimaryActionByLens(BaseModel):
    """The one backend-selected banner action for each lens (issue #1665).

    ``trader`` is restricted to the closed
    ``app.broker.v2panel.vocabulary.TRADER_LIFECYCLE_ACTION_IDS`` set
    (``resume`` / ``continue`` / ``stop``); an Operator-only recovery
    capability can never reach it. ``operator`` also considers those same
    lifecycle actions, but a SQLite ``RecoveryCapability.primary`` recovery
    action takes precedence when one is available — the audience-aware
    precedence rule authored once by
    ``panel_projection_service.select_primary_action_by_lens`` (ADR 0027).
    Either reference is ``None``, never a guess, when nothing currently
    qualifies; the frontend renders no banner action in that case rather than
    deriving one from ``health``.
    """

    model_config = ConfigDict(frozen=True)

    trader: ActionId | None
    operator: ActionId | None


class MissionVerdictView(BaseModel):
    """Backend-authored answer to whether this bot can perform its mission now."""

    model_config = ConfigDict(frozen=True)

    state: Literal["ready", "working", "blocked", "off_duty", "retired"]
    label: str
    explanation: str
    next_action: str | None
    evaluated_at_ms: int


class ReadinessCheckView(BaseModel):
    """Current enforcement check for one operation, separate from transaction history."""

    model_config = ConfigDict(frozen=True)

    operation: ActionId
    label: str
    ready: bool
    scope: Literal["bot", "account", "broker"]
    authority: str
    explanation: str
    evidence: dict[str, str | int | float | bool | None]
    evaluated_at_ms: int
    cure: str | None


class WorkingOrderView(BaseModel):
    """Latest Clerk-owned non-terminal order state attributed to this bot."""

    model_config = ConfigDict(frozen=True)

    order_ref: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: float | None
    filled_quantity: float | None
    status: str
    observed_at_ms: int


class RecentDecisionView(BaseModel):
    """Bounded backend-authored decision receipt for the Trader lens."""

    model_config = ConfigDict(frozen=True)

    seq: int
    recorded_at_ms: int
    outcome: Literal[
        "enter_intent",
        "exit_intent",
        "entered",
        "exited",
        "no_action",
        "blocked",
    ]
    reason_code: str
    bar_ref: str
    order_ref: str | None
    # PRD Sec 19 causal provenance: the durable evaluation/effect identities
    # the Clerk stored at intake time (`decision_id` == `evaluation_id`;
    # `effect_operation_id` binds the decision to its accepted custody
    # operation). The projector never infers these from timing or ordering —
    # `None` renders the explicit absence of a stored link (e.g. a `no_action`
    # / `blocked` decision that never reached Clerk intake, or a Dry Run row
    # sourced from evidence that does not yet carry these identities).
    decision_id: str | None = None
    effect_operation_id: str | None = None
    simulated: bool = False
    authority_account_id: str | None = None
    authority_kind: Literal["real_paper", "synthetic"] | None = None

    @model_validator(mode="after")
    def simulated_row_has_synthetic_authority(self) -> RecentDecisionView:
        _validate_simulated_authority_metadata(
            simulated=self.simulated,
            authority_account_id=self.authority_account_id,
            authority_kind=self.authority_kind,
        )
        return self


class RecentFillView(BaseModel):
    """Bounded Clerk-attributed fill receipt for the Trader lens."""

    model_config = ConfigDict(frozen=True)

    order_ref: str
    symbol: str
    side: str
    quantity: float | None
    price: float | None
    filled_at_ms: int
    simulated: bool = False
    authority_account_id: str | None = None
    authority_kind: Literal["real_paper", "synthetic"] | None = None

    @model_validator(mode="after")
    def simulated_row_has_synthetic_authority(self) -> RecentFillView:
        _validate_simulated_authority_metadata(
            simulated=self.simulated,
            authority_account_id=self.authority_account_id,
            authority_kind=self.authority_kind,
        )
        return self


class MarketPulseView(BaseModel):
    """Scheduled session, live tradability, and data recency authored by Python."""

    model_config = ConfigDict(frozen=True)

    session: Literal["PRE_MARKET", "OPEN", "AFTER_HOURS", "CLOSED", "UNKNOWN"]
    market_state: Literal["TRADABLE", "HALTED", "CLOSED", "UNKNOWN"]
    market_liveness_reason: str
    market_liveness_observed_at_ms: int
    # Structured symbol for the HALTED headline — never interpolated into
    # ``headline`` prose. The frontend renders it through the canonical
    # ``app-asset-identity`` component rather than a raw ticker string.
    halted_symbol: str | None
    feed_state: Literal["LIVE", "IDLE", "STALE", "MISSING"]
    latest_bar_at_ms: int | None
    age_ms: int | None
    source: str | None
    expected_cadence_ms: int
    headline: str
    explanation: str
    next_step: str | None
    attention_required: bool
    observed_at_ms: int


class BotPanelView(BaseModel):
    """The full 5s-poll panel projection for one bot (§7).

    Everything except chart data: bot health, clerk/account state, the six-
    station rail, a journal-tail reference, the presented actions, and the
    panel-state ``revision`` those actions bind to.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    strategy_key: str
    strategy_label: str
    broker: str
    account_id: str
    symbol: str
    mode: Literal["log_only", "dry_run", "trade"]
    # PRD Sec 11.1/11.2 immutable identity: the exact versioned seal bound to
    # this strategy instance, reused verbatim from its single canonical shape
    # (``app.schemas.signal_program_seal``) rather than re-derived here. This
    # is seal content only — never health, custody, current policy, checkpoint
    # codec, or simulated fill policy (PRD Sec 11.3 draws that line; those
    # stay on ``health`` / ``clerk`` / ``execution_policy`` below). ``None``
    # is an explicit absence: a legacy pre-seal instance or a compatibility
    # strategy with no registered Signal Program, never an inferred one.
    sealed_program: SealedBotProgram | None
    # PRD Sec 11.3/11.4 dynamic run evidence: whether the currently loaded
    # program bytes are proven compatible with the sealed
    # ``(program_version, golden_trace_root)``, freshly re-verified through
    # the same canonical ``prove_running_program_build`` Start/Resume
    # admission uses. Always present — ``NOT_APPLICABLE`` for a compatibility
    # strategy with no registered Signal Program, ``UNPROVEN`` when the seal
    # or receipt evidence does not (yet) close, ``PROVEN`` otherwise.
    program_build: ProgramBuildAdmissionFact
    # PRD Sec 11.3 "current admission-policy version and verdict": the most
    # recent Start/Resume admission decision this panel observed. ``None``
    # while the bot is running — Resume admission is not evaluated for a live
    # run, which is an explicit absence, not a missing read.
    resume_admission: RunAdmissionDecision | None
    updated_at_ms: int
    revision: int
    market_pulse: MarketPulseView
    mission_verdict: MissionVerdictView
    execution_policy: str
    health: BotHealthCard
    clerk: ClerkCard
    rail: TransactionRail
    # Journal-tail reference (§7.4): the bounded read endpoint + the newest seq
    # the panel observed, so the tail component knows where to page from.
    journal_tail_ref: str
    journal_tail_seq: int | None
    actions: list[PanelAction]
    # The one backend-selected banner action per lens (issue #1665). Neither
    # Angular banner may derive a primary action from ``health`` any more.
    primary_action_by_lens: PrimaryActionByLens
    readiness_checks: list[ReadinessCheckView]
    # Server-authored presentation aggregate. Consumers render these counts
    # verbatim so every surface reports the same command-gate posture.
    readiness_ready_count: int
    readiness_blocked_count: int
    exposure: dict[str, float]
    working_orders: list[WorkingOrderView]
    recent_decisions: list[RecentDecisionView]
    recent_fills: list[RecentFillView]
    # S0 rollup summary — backend-computed FIFO P&L (§10).  Frontend renders,
    # never recomputes.  "Fees not reported" renders when fee_fidelity="none".
    fills_today: int | None
    realized_pnl_today: float | None
    open_pnl: float | None

    @model_validator(mode="after")
    def _primary_action_by_lens_is_coherent(self) -> BotPanelView:
        """Fail closed on a dangling or audience-incompatible reference.

        Every non-``None`` lens reference must name an action present in
        ``actions`` (no dangling reference), and the Trader reference must
        additionally be one of the closed Trader-visible lifecycle action ids
        — an Operator-only recovery capability can never become the Trader
        banner's primary command (issue #1665).
        """
        action_ids = {action.action_id for action in self.actions}
        trader_ref = self.primary_action_by_lens.trader
        if trader_ref is not None and (
            trader_ref not in TRADER_LIFECYCLE_ACTION_IDS or trader_ref not in action_ids
        ):
            raise ValueError(
                f"primary_action_by_lens.trader={trader_ref!r} must reference a "
                "Trader-visible lifecycle action present in `actions`"
            )
        operator_ref = self.primary_action_by_lens.operator
        if operator_ref is not None and operator_ref not in action_ids:
            raise ValueError(
                f"primary_action_by_lens.operator={operator_ref!r} must reference "
                "an action present in `actions`"
            )
        return self


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
    outcome: Literal["success"] = "success"
    receipt_id: str
    recorded_at_ms: int
    applied: bool
    revision: int
    concurrency_token: str
    message: str


class PanelActionErrorResponse(BaseModel):
    """A rejected action execution (409/500) (§11, PRD #1716 FR-4).

    Published so the OpenAPI contract and generated frontend types carry the
    exact error shape callers previously narrowed by hand from an untyped
    ``HTTPException.detail`` record. ``reason_code`` is the stable machine
    code (e.g. an admission ``reason_code``) for a raw-identifier display
    through the shared ``receiptLabel`` pipe; ``message``/``why`` remain
    backend-authored prose rendered as-is.
    """

    model_config = ConfigDict(frozen=True)

    action_id: ActionId
    outcome: Literal["conflict", "failure", "unknown"]
    receipt_id: str | None
    recorded_at_ms: int
    message: str
    why: str | None
    reason_code: str | None


# ── §8 Chart shapes ──────────────────────────────────────────────────────────

ChartSource = Literal["ibkr", "polygon", "mixed"]
ChartHistoryTimeframe = Literal["1m", "15m", "30m", "1h", "1d"]


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
    # Stable per-fill identity (mirrors FillRecord.event_key) — distinct
    # from order_ref, which is shared by every partial fill of one order.
    # Consumers that need to distinguish individual fills (a gallery
    # incremental cursor, a client-side merge across partial fills of the
    # same order) must key on this, not on order_ref or filled_at_ms alone:
    # neither is guaranteed unique per fill (two fills of one order share
    # order_ref; two fills — of the same or different orders — can share a
    # millisecond timestamp).
    event_key: str


class ChartOverlayNoticeView(BaseModel):
    """An honest chip explaining a fallback overlay (§8)."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    source: Literal["polygon"]


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


class BotPanelLiveSnapshot(BaseModel):
    """Versioned complete state document used by REST bootstrap and SSE."""

    model_config = ConfigDict(frozen=True)

    stream_epoch: str
    surface_version: int = Field(ge=0)
    panel: BotPanelView
    live_chart: ChartLiveResponse


class LiveSnapshotUnavailableDetail(BaseModel):
    """Retry guidance when a producer has not published its first snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str
    why: str
    next_action: str


class LiveSnapshotUnavailableResponse(BaseModel):
    """Typed 503 envelope for the live-panel bootstrap and stream routes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    detail: LiveSnapshotUnavailableDetail


class ChartHistoryResponse(BaseModel):
    """Bounded Polygon chart response for one selected timeframe (§8).

    ``bars`` is the bounded display window. ``indicator_bars`` includes every
    available preceding warmup candle, capped by the configured Polygon history
    entitlement. The explicit budget fields distinguish a complete calculation
    window from entitlement- or liquidity-limited history. The existing 7-day
    live resolver is not widened.
    """

    model_config = ConfigDict(frozen=True)

    strategy_instance_id: str
    symbol: str
    timeframe: ChartHistoryTimeframe
    from_ms: int
    to_ms: int
    bars: list[ChartBar]
    indicator_bars: list[ChartBar]
    indicator_bar_budget: int
    indicator_bar_budget_satisfied: bool
    fill_markers: list[ChartFillMarker]
    truncated: bool
    as_of_ms: int
