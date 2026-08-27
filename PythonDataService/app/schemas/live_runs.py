"""Pydantic v2 schemas for live-runs API.

Models for representing live paper-trading run state, decisions, executions,
trades, and artifacts. All timestamps are int64 milliseconds UTC.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.operator.notices.schema import (
    OperatorNotice,
    OperatorNoticeAction,
    OperatorNoticeActionability,
    OperatorNoticeRemedyStatus,
    OperatorNoticeTier,
    validate_actionability_action_pairing,
)
from app.schemas.bot_lifecycle import BotDutyOutcomeKind


class RunState(StrEnum):
    """State of a live run's execution lifecycle."""

    idle = "idle"
    waiting_for_bars = "waiting_for_bars"
    warming_up = "warming_up"
    running = "running"
    stale = "stale"
    halted = "halted"
    poisoned = "poisoned"
    complete = "complete"
    stopped = "stopped"
    unknown = "unknown"


class ExitReason(StrEnum):
    """Reason why a live run exited."""

    normal = "normal"
    force_flat_complete = "force_flat_complete"
    keyboard_interrupt = "keyboard_interrupt"
    signal = "signal"
    max_orders_exceeded = "max_orders_exceeded"
    fatal_halt = "fatal_halt"
    recovery_flatten = "recovery_flatten"
    exception = "exception"
    # A start was refused because the run is poisoned (poisoned.flag present, or
    # corrupted). Distinct from fatal_halt (the live engine's intra-day trip):
    # this is the cold-start refusal, recorded so the console explains "fresh
    # run_id required" instead of a blank "ended unexpectedly".
    poisoned = "poisoned"


class RunStatusSidecar(BaseModel):
    """Sidecar process metadata for a live run.

    Created and maintained by the observer sidecar process, containing
    lifecycle timestamps and process metadata.

    PRD #619-A adds ``submit_mode_at_start`` and ``readonly_at_start``
    as durable child/run evidence the Resume gate consults for the
    submission_capability check (ADR-0011 amendment: identity and
    capability are independent facts). Both are captured at child
    boot and never mutated after. A legacy 1.x sidecar without either
    field reads as ``None`` and Resume treats capability as UNKNOWN.
    """

    schema_version: int = 2
    run_id: str
    started_at_ms: int
    last_update_ms: int
    ended_at_ms: int | None = None
    exit_code: int | None = None
    exit_reason: ExitReason | None = None
    host_pid: int
    # PRD #619-A — capability evidence.
    submit_mode_at_start: Literal["live_paper", "shadow"] | None = None
    readonly_at_start: bool | None = None
    # Startup/runtime failure evidence. ``exit_reason=exception`` is too coarse
    # for clients to present a useful remedy; typed fields let the operator
    # surface say e.g. "IBKR client ID is already in use" instead of collapsing
    # into a generic reconcile prompt.
    exit_error_code: str | None = None
    exit_error_message: str | None = None
    exit_error_detail: dict[str, Any] = Field(default_factory=dict)


class LiveRunSummary(BaseModel):
    """High-level summary of a live run's state and counts.

    Aggregates data from the ledger, sidecar, and run directory
    to provide a single snapshot of run health and activity.
    """

    run_id: str
    account_id: str
    session_start_ms: int  # ledger.start_date_ms
    created_at_ms: int  # ledger.created_at_ms
    run_started_at_ms: int | None  # sidecar.started_at_ms
    ended_at_ms: int | None  # sidecar.ended_at_ms
    last_activity_ms: int  # max(mtime) across run-dir files
    state: RunState
    decision_count: int
    execution_count: int
    halt_flag_set: bool
    poisoned_flag_set: bool


class DecisionsSummary(BaseModel):
    """Summary of decision records in a live run."""

    row_count: int
    latest_decision: dict | None = None


class ExecutionsSummary(BaseModel):
    """Summary of execution records in a live run."""

    row_count: int
    last_fills: list[dict] = []


class TradesSummary(BaseModel):
    """Summary of trade records in a live run."""

    row_count: int
    open_position: dict | None = None


class FlagsSummary(BaseModel):
    """Summary of halt and poisoned flags."""

    halt_flag: dict | None = None  # parsed JSON body if present
    poisoned_flag: dict | None = None  # parsed JSON body if present


class ArtifactFile(BaseModel):
    """Metadata for a single artifact file."""

    name: str
    size_bytes: int | None = None
    mtime_ms: int | None = None
    row_count: int | None = None  # Parquet files only


class ArtifactsSummary(BaseModel):
    """Summary of artifact files in a run directory."""

    files: list[ArtifactFile] = []


class ReconcileSummary(BaseModel):
    """Summary of reconciliation / reference data."""

    latest_receipt_name: str | None = None
    latest_receipt_url: str | None = None  # relative path for download link


class LiveRunStatus(BaseModel):
    """Complete status snapshot of a live run.

    Combines run summary, bar timing, decision/execution/trade counts,
    flags, and artifact metadata into a single response.
    """

    run_id: str
    account_id: str
    state: RunState
    last_bar_time_ms: int | None = None
    last_bar_age_s: float | None = None
    heartbeat_parse_status: Literal["ok", "degraded", "no_bars_yet"] = "no_bars_yet"
    decisions: DecisionsSummary
    executions: ExecutionsSummary
    trades: TradesSummary
    flags: FlagsSummary
    artifacts: ArtifactsSummary
    reconcile: ReconcileSummary
    strategy_instance_id: str | None = None
    desired_state: DesiredStateView | None = None
    command_summary: CommandSummary | None = None
    fetched_at_ms: int


class LogLine(BaseModel):
    """A single line from a live run's log stream.

    Supports both raw text lines and structured bar events.
    """

    ts_ms: int | None = None
    raw_text: str
    event_type: Literal["bar", "raw"] = "raw"
    # populated for bar events
    consolidator_emitted: int | None = None
    snapshot_set: str | None = None


class FailureRecord(BaseModel):
    """One ERROR/CRITICAL block parsed from live.log.

    ``raw_ts`` is the verbatim timestamp string from the log (UTC, since
    the engine logger's ``_StepFormatter`` pins ``time.gmtime``);
    ``ts_ms`` is the same instant as canonical ``int64`` ms since Unix
    epoch UTC. The parser that produced these rows,
    ``app/services/live_log_failures.py``, retired with the
    ``/api/live-runs`` surface (PR-B of #1813, 2026-08-27).

    Not a wire shape. Nothing constructs this model, no route serves it,
    and it has no entry in the OpenAPI contract — an earlier revision of
    this docstring said it "remains as the wire shape for historical rows
    only", which overstated it. It is unreferenced residue of that
    retirement; see the ``live_runs.py`` residue row in
    ``docs/architecture/engine-authority-map.md`` for its disposition.
    """

    ts_ms: int
    raw_ts: str
    level: Literal["ERROR", "CRITICAL"]
    logger: str
    message: str
    traceback: str | None = None


class IncidentRecord(BaseModel):
    """One WARNING/ERROR/CRITICAL block parsed from live.log, with a
    backend-classified ``incident_category`` the frontend keys its copy
    map on plus an ``incident_source`` for the evidence view's BROKER / APP /
    INFRA / OPERATOR badge + filter (codex 2026-06-24 D2 / D8).

    Mirrored ``app/services/live_log_failures.py``'s ``IncidentRow``; that
    classifier retired with the ``/api/live-runs`` surface (PR-B of #1813,
    2026-08-27). Like :class:`FailureRecord`, this is no longer a wire
    shape — nothing constructs it, no route serves it, and it has no entry
    in the OpenAPI contract. It is unreferenced residue of that
    retirement. The paragraphs below describe the contract it carried while
    that surface existed. The ``incident_category`` enum is the
    single source of truth for classification — the frontend never re-derives meaning from the raw
    log text. A missing or unrecognised category is rendered as
    ``unknown`` on the frontend for rollout safety.

    Same ``raw_ts`` / ``ts_ms`` semantics as :class:`FailureRecord` for
    log-parsed rows. Durable operator incidents synthesize ``raw_ts`` from
    their canonical ``occurred_at_ms`` because they do not originate as a
    verbatim ``live.log`` line. ``ts_ms`` is always canonical ``int64`` ms UTC.

    ``dynamic_facts`` carries the typed hybrid-C named values the
    frontend may interpolate into its category template (codex D1).
    Empty by default so rows whose category has no fact extractor (or
    whose runtime emitted the line without enough context) still render
    the template verbatim.
    """

    ts_ms: int
    raw_ts: str
    level: Literal["WARNING", "ERROR", "CRITICAL"]
    logger: str
    message: str
    traceback: str | None = None
    incident_category: str
    incident_source: str
    dynamic_facts: dict[str, str | int] = {}


HydratePolicy = Literal["require", "optional", "disabled"]
DEFAULT_MAX_ORDERS_PER_DAY = 2_000


class HostRunnerProcessState(StrEnum):
    """Lifecycle state of the host-side runner subprocess."""

    idle = "idle"
    running = "running"
    exited = "exited"
    stopping = "stopping"


class HostRunnerProcessStatus(BaseModel):
    """Current host-daemon process status.

    This is intentionally process-level, not trading-state-level. Trading
    state remains authoritative in :class:`LiveRunStatus`, derived from the
    run directory artifacts.
    """

    state: HostRunnerProcessState
    run_id: str | None = None
    # Multi-process registry (ADR 0004): the strategy instance this process
    # belongs to. None for legacy runs with no ledger binding.
    strategy_instance_id: str | None = None
    pid: int | None = None
    ibkr_client_id: int | None = Field(default=None, ge=0)
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    exit_code: int | None = None
    exit_reason: str | None = None
    command: list[str] = Field(default_factory=list)
    log_path: str | None = None
    message: str | None = None


class AccountClerkHealth(BaseModel):
    """Process health reported by the host runner's Clerk inventory.

    Not the Alpaca Broker V2 authority selector — see
    ``active_sqlite_facade`` in ``app/services/sqlite_clerk_compat.py`` for
    why.
    """

    account_id: str
    generation: int = Field(ge=1)
    pid: int | None = Field(default=None, ge=1)
    status: str
    started_at_ms: int = Field(ge=0)
    renewed_at_ms: int | None = Field(default=None, ge=0)
    valid_until_ms: int | None = Field(default=None, ge=0)
    lease_valid: bool


class HostRunnerHealth(BaseModel):
    """Health envelope returned by the host-side runner daemon."""

    ok: bool
    repo_root: str
    live_runs_root: str
    fetched_at_ms: int
    process: HostRunnerProcessStatus
    # Process inventory only, not the authority selector — see
    # active_sqlite_facade in app/services/sqlite_clerk_compat.py.
    clerks: list[AccountClerkHealth] = Field(default_factory=list)
    # Code-freshness: the daemon does not reload on `git pull`, so an operator
    # needs to see whether the running code matches the working tree.
    # ``git_sha`` is the SHA the daemon process is actually RUNNING (captured at
    # launch); ``repo_head_sha`` is the live on-disk HEAD (what a restart would
    # run); ``code_stale`` is True when they differ (restart to apply fixes);
    # ``commits_behind`` is a best-effort count of how far behind. All None/False
    # when git is unavailable.
    git_sha: str | None = None
    repo_head_sha: str | None = None
    code_stale: bool = False
    commits_behind: int | None = None
    # PRD #619-B — control-plane identity. ``daemon_boot_id`` is the UUID
    # the daemon process generated at startup; spawned children read it
    # via the ``LIVE_RUNNER_DAEMON_BOOT_ID`` env var and the child
    # watchdog (B5) treats a mismatch as ``BOOT_ID_CHANGED``. ``lease_status``
    # mirrors ``daemon_lease.json.status`` (``CONNECTED`` / ``DRAINING``);
    # ``last_lease_written_at_ms`` is the timestamp of the most recent
    # successful lease write. ``orphan_candidates_count`` is the size of
    # the read-only investigation list the orphan classifier (B6)
    # produced at boot — the daemon does NOT auto-adopt; >0 surfaces on
    # the client so the operator decides.
    daemon_boot_id: str | None = None
    lease_status: str | None = None
    last_lease_written_at_ms: int | None = None
    lease_threshold_ms: int | None = None
    lease_write_error: str | None = None
    orphan_candidates_count: int = 0
    orphan_candidates: list[dict[str, Any]] = Field(default_factory=list)
    platform: str | None = None
    supervisor: str | None = None


class EmergencyFlattenRequest(BaseModel):
    """Body for the account-wide emergency flatten (§ 7.2 #6).

    Reaches the held Account Clerk independent of any live binding, so an
    operator can flatten after a halt/poison. The Clerk closes intake, records
    cancellation uncertainty, writes any liquidations under its own broker
    session, and only completes after a fresh paper-account snapshot is flat.
    """

    account: str = Field(..., min_length=2, max_length=32)
    confirmation_token: Literal["FLATTEN"] = Field(
        ..., description="Exact typed confirmation required for the destructive account action."
    )
    idempotency_key: str = Field(
        ..., min_length=1, max_length=128, description="Public emergency operation identity."
    )


class AccountEmergencyFlattenDispatchRequest(EmergencyFlattenRequest):
    """Host-only envelope carrying the Clerk-issued authorization receipt id."""

    authorization_id: str = Field(
        ..., min_length=16, max_length=128, description="Short-lived Clerk reconciliation authorization."
    )


class AccountEmergencyFlattenAuthorizationRequest(BaseModel):
    """Host-only request for the Clerk's short-lived flatten authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str = Field(..., min_length=1, max_length=128)
    reconciliation_evidence_version: str = Field(..., min_length=1, max_length=128)


class AccountEmergencyFlattenResponse(BaseModel):
    """Receipt returned after the Clerk re-observes the account flat."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    account_id: str = Field(min_length=2, max_length=32)
    audit_run_id: str = Field(min_length=2, max_length=128)
    completed_at_ms: int = Field(ge=0)
    idempotency_key: str | None = None
    idempotency_replayed: bool = False


def _validate_bare_ibkr_host(value: str) -> str:
    """Keep host-side broker destinations free of URL/path syntax."""

    host = value.strip()
    if host != value or not host:
        raise ValueError("ibkr_host must not contain surrounding whitespace")
    lowered = host.lower()
    if any(token in lowered for token in ("://", "/", "\\", "@")):
        raise ValueError("ibkr_host must be a bare host name or IP address")
    return host


class HostRunnerClerkEnsureRequest(BaseModel):
    """Host-side broker destination for starting an account Clerk."""

    ibkr_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)

    @field_validator("ibkr_host")
    @classmethod
    def _validate_ibkr_host(cls, value: str) -> str:
        return _validate_bare_ibkr_host(value)


class MutationOutcomeUnknownResponse(BaseModel):
    """Typed 409 body for single-shot mutations whose transport outcome
    could not be proven (PRD #619-C5).

    Surfaced by ``renew_daemon_lease`` when the typed daemon POST returns
    ``DaemonResult.kind == "UNREACHABLE"`` with
    ``outcome_ambiguous=True`` — i.e., the request was (partly or
    fully) sent but the response was lost.  The mutation may or may not
    have executed on the daemon side.

    Distinct from 503 ``host daemon unreachable`` (clean pre-send
    failure where retry is safe). 409 CONFLICT signals "eligibility is
    indeterminate" — the operator must refresh state before retrying.

    The caller must refresh read-side state before retrying; this model is the
    synchronous surfacing contract.
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["UNKNOWN"] = "UNKNOWN"
    reason_code: Literal["OUTCOME_UNKNOWN"] = "OUTCOME_UNKNOWN"
    # Stable short code (``read_timeout`` / ``write_timeout`` /
    # ``remote_protocol_error`` / ``network_error`` / ``transport_error``) —
    # forwarded from the ``DaemonResult.error_category``.
    error_category: str
    # Safe-detail-capped daemon-side message if any (None when the
    # underlying exception carried no message).
    detail: str | None = None
    # Canonical endpoint label so clients can show the right copy.
    endpoint: Literal["renew_daemon_lease"]
    # ``int64 ms UTC`` of the failure.
    occurred_at_ms: int = Field(ge=0)
    # Operator-language one-liner, server-authored per endpoint, telling
    # the operator what they need to do next (refresh state, do not
    # blindly retry).
    runbook_hint: str


MutationBlockageStageId = Literal[
    "control_plane",
    "host_process",
    "broker",
    "account_safety",
    "account_clerk",
    "reconciliation",
    "preflight",
    "trading_session",
    "runtime_freshness",
]

MutationRungReceiptCode = Literal[
    "mutation.next_blocking_rung",
    "mutation.scoped_all_clear",
    "mutation.observational_warning",
]


class MutationRungReceipt(BaseModel):
    """Notice-shaped post-mutation receipt authored from the fresh ladder.

    These receipts are not persisted operator incidents, so their ``code`` values
    intentionally live outside the closed ``OperatorNoticeCode`` union. They
    still obey the notice actionability vocabulary and action-pairing contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: MutationRungReceiptCode
    tier: OperatorNoticeTier
    title: str
    message: str
    rung_id: MutationBlockageStageId | None = None
    source_codes: list[str] = Field(default_factory=list)
    forensic_facts: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    actionability: OperatorNoticeActionability
    resolution: str = Field(min_length=1)
    remedy_status: OperatorNoticeRemedyStatus | None = None
    action: OperatorNoticeAction
    occurred_at_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _actionability_contract(self) -> MutationRungReceipt:
        validate_actionability_action_pairing(
            actionability=self.actionability,
            action=self.action,
            remedy_status=self.remedy_status,
            noun="receipts",
        )
        return self


# --- Read-only live-run evidence contract ---


class DesiredStatePathStatus(StrEnum):
    """How the desired-state sidecar resolved for a run (UI-1)."""

    ok = "ok"
    absent = "absent"
    corrupt = "corrupt"
    unknown_no_ledger_binding = "unknown_no_ledger_binding"


class DesiredStateValue(StrEnum):
    """Canonical durable desired-state values stored on disk."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class DesiredStateView(BaseModel):
    """Resolved durable-intent view; ``path_status`` carries resolution.

    ``state`` is null unless ``path_status == ok``. Absence is the
    effective-RUNNING default; an empty ledger binding yields
    ``unknown_no_ledger_binding`` and is never guessed from parquet.
    """

    state: DesiredStateValue | None = None
    updated_at_ms: int | None = None
    updated_by: str | None = None
    reason: str | None = None
    version: int | None = None
    path_status: DesiredStatePathStatus


class CommandSummary(BaseModel):
    """Pending/ack counts + latest verb for a run's command channel."""

    pending_count: int
    acked_count: int
    latest_verb: str | None = None
    latest_seq: int | None = None


class CommandTimelineEntry(BaseModel):
    """One command with its full lifecycle (#397).

    ``status``: ``queued`` (pending, no ack) -> ``acknowledged`` (ack with an
    ok outcome) | ``failed`` (ack with an error outcome). Timestamps are
    payload-sourced where present, else derived from file mtime.
    """

    seq: int
    verb: str
    status: Literal["queued", "acknowledged", "failed"]
    reason: str | None = None
    issued_by: str | None = None
    queued_at_ms: int | None = None
    acked_at_ms: int | None = None
    outcome: str | None = None
    reason_code: str | None = None
    durable_control: bool = False
    failure_kind: Literal["durable_control_write_failed"] | None = None
    outcome_detail: str | None = None


class CommandsTimeline(BaseModel):
    """Canonical unified command timeline: one entry per command, newest first,
    with the dispatcher's poll cadence so the client's staleness threshold is
    server-provided (#397)."""

    entries: list[CommandTimelineEntry]
    poll_interval_ms: int


LiveRunStatus.model_rebuild()


GateResultStatus = Literal[
    "pass",
    "block",
    "poison",
    "freeze",
    "unknown",
    "not_applicable",
]


class GateResult(BaseModel):
    """Canonical lifecycle gate result row.

    A gate result is the enforcement-backed predicate clients can
    render and diagnose. Older readiness rows still expose their
    ``name`` / ``status`` / ``severity`` / ``detail`` fields for
    compatibility; ``GateResult`` is the normalized contract newer
    account-level gates consume.
    """

    model_config = ConfigDict(extra="forbid")

    gate_id: str
    status: GateResultStatus
    source: str
    operator_reason: str
    operator_next_step: str | None = None
    evidence_at_ms: int = Field(ge=0)


class ReadinessGate(BaseModel):
    """One named input to the "can this strategy act on the next bar?" verdict
    (ADR 0005). ``status`` is pass|fail|unknown; ``severity`` is hard|soft."""

    name: str
    status: str  # pass | fail | unknown
    severity: str  # hard | soft
    detail: str
    gate_result: GateResult | None = None


class ReadinessVector(BaseModel):
    """Structured readiness verdict (ADR 0005).

    ``kind``/``source``: ``live_readiness``/``engine`` when authored by the
    running engine; ``start_readiness``/``backend_derived`` when computed for a
    dead instance from durable artifacts. ``verdict`` is READY|BLOCKED|DEGRADED|
    UNKNOWN. ``live_readiness_available`` is set only on start_readiness.
    """

    kind: str
    as_of_ms: int
    source: str
    verdict: str
    summary: str
    gates: list[ReadinessGate] = Field(default_factory=list)
    live_readiness_available: bool | None = None
    # PRD #607 / Slice 1 (#608) — structured cap counters emitted by the
    # engine readiness sidecar so readiness consumers use integers
    # rather than parsing the gate prose ``"3 / 50 orders used"``.  Both
    # ``None`` on start_readiness (backend-derived) and when no cap is
    # configured.
    orders_used: int | None = None
    orders_cap: int | None = None


class InstanceBrokerView(BaseModel):
    """The instance's namespace-attributed broker slice (ADR 0005, #398).

    Engine-authored, from the live-state sidecar: ownership is keyed on
    ``bot_order_namespace``; ``owned_positions`` is the engine's running tally of
    its own namespace fills (``expected_position_by_symbol``) — never decomposed
    from the raw net account snapshot. The instance broker gate is
    self-consistency only.
    """

    bot_order_namespace: str
    owned_positions: dict[str, int] = Field(default_factory=dict)
    pending_order_count: int = 0
    # PRD #607 / Slice 4 (#611) contract dep on #608: broker-side
    # unrealized PnL for broker evidence. ``None`` when
    # the broker connector cannot resolve a value; clients omit the
    # slot rather than rendering ``0.00`` (#611 §"Pinned risk-chip").
    unrealized_pnl: float | None = None


BrokerConnectionState = Literal["CONNECTED", "DISCONNECTED", "DEGRADED", "UNKNOWN"]
TradingSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]
# ---------------------------------------------------------------------------
# Reconciliation receipt (ADR-0008 §5 / PR 1 cold-start orchestrator).
# ---------------------------------------------------------------------------

ReceiptStatus = Literal["in_progress", "passed", "failed"]
"""Lifecycle status of a reconciliation receipt.

``in_progress`` is written first (so a crash mid-reconcile leaves an honest
sentinel rather than a stale ``passed`` receipt from the previous boot);
``passed`` / ``failed`` overwrite it with the verdict via atomic replace.
"""

ReceiptOutcome = Literal["clean", "adopted"]
"""Meaningful only when ``status == passed``.

``clean`` = the broker snapshot matched the projection (Continue).
``adopted`` = one or more owned orphans were folded in via
``ADOPTED_BROKER_ORDER`` (Adopt).
"""


class ReconciliationReceipt(BaseModel):
    """Durable historical evidence of a cold-start reconciliation attempt.

    The retired IBKR runtime wrote this to
    ``<run_dir>/reconciliation_receipt.json``. Read projections retain the
    schema for existing artifacts; no current execution guard consumes it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    status: ReceiptStatus
    outcome: ReceiptOutcome | None = None
    run_id: str
    strategy_instance_id: str
    namespace: str
    started_at_ms: int = Field(gt=0)
    completed_at_ms: int | None = Field(default=None, ge=0)
    last_reconcile_ms: int | None = Field(default=None, ge=0)
    sidecar_wal_seq: int = Field(default=0, ge=0)
    broker_observed_at_ms: int | None = Field(default=None, ge=0)
    adopted_intent_ids: tuple[str, ...] = ()
    failure_reason: str | None = None


class OpenRunbookAction(BaseModel):
    """Suggested action: open an operator runbook (server-authored slug)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["open_runbook"]
    slug: str


# ---------------------------------------------------------------------------
# Cold-start reconciliation projection (ADR-0008 §5 / PR 1).
# ---------------------------------------------------------------------------

"""Operator-facing cold-start reconciliation evidence."""


class BrokerActivityHealthFacts(BaseModel):
    """Raw diagnostic facts behind the broker-activity health verdict.

    Frontend renders these in the forensic-detail panel only; it must
    not derive state from them.  State comes exclusively from
    ``BrokerActivityHealth.state``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    publisher_registered: bool
    publisher_running: bool
    latest_row_seq: int | None
    seconds_since_registered: int | None
    seconds_since_last_row: int | None


class BrokerActivityHealth(BaseModel):
    """PR 5 — broker-activity publisher health surface.

    A single typed verdict (``state``) plus an optional operator-facing
    notice (``headline``) and a list of all active notices (``notices``).
    ``facts`` carries the raw diagnostics; clients never derive
    state from them.

    States:
    - ``ready``       — publisher registered + running + emitting rows (or
                        still within the silent-boot window).
    - ``starting``    — publisher registered but not yet running; within
                        the starting-timeout window.
    - ``degraded``    — publisher registered + running but no rows recently.
    - ``unavailable`` — publisher not registered or timed out while starting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ready", "starting", "degraded", "unavailable"]
    headline: OperatorNotice | None = None
    notices: list[OperatorNotice] = Field(default_factory=list)
    facts: BrokerActivityHealthFacts


class BotDutyOutcomeView(BaseModel):
    """Durable terminal duty evidence rendered by the operator surface."""

    model_config = ConfigDict(extra="forbid")

    kind: BotDutyOutcomeKind
    reason_code: str
    recorded_at_ms: int
    run_id: str | None = None


class ChartOverlayNotice(BaseModel):
    """Non-persistent market-data overlay warning for the chart window."""

    code: str
    message: str
    session_date: str | None = None
    source: Literal["polygon"] = "polygon"


class FleetExplainedBucket(BaseModel):
    """One instance's contribution to the account's explained position (#399)."""

    strategy_instance_id: str
    positions: dict[str, int]


class FleetContamination(BaseModel):
    """Account-level contamination — the one readiness signal authored by the
    backend (ADR 0005, #399). ``residual = net - Σ explained``; a non-zero
    residual is a position no managed instance created. ``verdict`` is
    clean|contaminated|unknown (unknown when the net snapshot is unavailable).
    """

    net_positions: dict[str, int] | None = None
    explained_total: dict[str, int] = Field(default_factory=dict)
    explained_by_instance: list[FleetExplainedBucket] = Field(default_factory=list)
    residual: dict[str, int] = Field(default_factory=dict)
    verdict: str
    policy_blocks_starts: bool = False
    summary: str


class FleetAccountSummary(BaseModel):
    """Account/fleet altitude DTO (PRD #616).

    Server-authored single source of truth for the account row: it
    separates account identity from position contamination so the
    client renders one DTO without an Angular-side merge.

    ``account_identity == 'CONSISTENT'`` iff every managed instance
    agrees on ``account_id`` AND (when known) that id matches the
    broker-connected account.  ``account_identity_reason_codes`` is a
    closed ``ALL_CAPS_SNAKE`` vocabulary (``ACCOUNT_ID_MISSING``,
    ``INSTANCE_ACCOUNT_MISMATCH``, ``BROKER_ACCOUNT_UNAVAILABLE``,
    ``BROKER_ACCOUNT_MISMATCH``).

    Position contamination semantics are unchanged: ``verdict ==
    'contaminated'`` iff ``net_broker_positions − Σ managed instance
    positions ≠ 0``.  Configuration / identity disagreement is reported
    via ``account_identity``, never overloaded onto ``contamination``.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: str | None = None
    account_identity: Literal["CONSISTENT", "CONFLICTING", "UNKNOWN"]
    account_identity_reason_codes: list[str] = Field(default_factory=list)
    contamination: FleetContamination
