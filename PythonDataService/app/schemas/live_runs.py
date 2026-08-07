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
from app.schemas.operator_blocker import OperatorBlocker
from app.schemas.presented_operator_action import PresentedOperatorActionInvocation


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
    epoch UTC. See :mod:`app.services.live_log_failures` for the parser
    contract.
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

    Mirrors :class:`app.services.live_log_failures.IncidentRow` as the wire
    DTO. The ``incident_category`` enum is the single source of truth for
    classification — the frontend never re-derives meaning from the raw
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
    """Daemon-observed health for the sole clerk of one paper account."""

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


class HostRunnerInstance(BaseModel):
    """One managed strategy instance's live process binding.

    The host-daemon registry is the sole authority for the live
    ``strategy_instance_id -> run_id`` binding (ADR 0004): "live" is a
    process fact, not an artifact fact.
    """

    strategy_instance_id: str
    run_id: str
    run_dir: str
    process: HostRunnerProcessStatus


class HostRunnerInstancesStatus(BaseModel):
    """All strategy instances the host daemon currently manages."""

    instances: list[HostRunnerInstance] = Field(default_factory=list)
    fetched_at_ms: int
    exited_record_retention_count: int | None = Field(default=None, ge=0)
    exited_record_retention_ttl_ms: int | None = Field(default=None, ge=0)
    exited_record_count: int = Field(default=0, ge=0)
    exited_records_pruned_total: int = Field(default=0, ge=0)


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


class HostRunnerStartRequest(BaseModel):
    """Request body for starting one existing run from the host daemon."""

    readonly: bool = True
    hydrate_policy: HydratePolicy = "require"
    strategy: str = Field(default="spy_ema_crossover", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    max_orders_per_day: int = Field(default=DEFAULT_MAX_ORDERS_PER_DAY, ge=0, le=100_000)
    ibkr_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    roll_call_offer_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    presented_action: PresentedOperatorActionInvocation | None = None

    @field_validator("ibkr_host")
    @classmethod
    def _validate_ibkr_host(cls, value: str) -> str:
        return _validate_bare_ibkr_host(value)


class HostRunnerStopRequest(BaseModel):
    """Request body for stopping the active host runner subprocess."""

    force: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    presented_action: PresentedOperatorActionInvocation | None = None


class MutationOutcomeUnknownResponse(BaseModel):
    """Typed 409 body for single-shot mutations whose transport outcome
    could not be proven (PRD #619-C5).

    Surfaced by ``deploy_instance`` / ``start_run`` /
    ``renew_daemon_lease`` when the typed daemon POST returns
    ``DaemonResult.kind == "UNREACHABLE"`` with
    ``outcome_ambiguous=True`` — i.e., the request was (partly or
    fully) sent but the response was lost.  The mutation may or may not
    have executed on the daemon side.

    Distinct from 503 ``host daemon unreachable`` (clean pre-send
    failure where retry is safe). 409 CONFLICT signals "eligibility is
    indeterminate" — the operator must refresh state before retrying.

    The durable ``mutation_attempt`` record owns later reconciliation;
    this model is the synchronous surfacing contract.
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
    endpoint: Literal[
        "deploy",
        "start_run",
        "renew_daemon_lease",
    ]
    # ``int64 ms UTC`` of the failure.
    occurred_at_ms: int = Field(ge=0)
    # Operator-language one-liner, server-authored per endpoint, telling
    # the operator what they need to do next (refresh state, do not
    # blindly retry).
    runbook_hint: str


MutationAttemptDispatchState = Literal[
    "PREPARED",
    "DISPATCHING",
    "RESPONSE_CONFIRMED",
    "OUTCOME_UNKNOWN",
    "EFFECT_CONFIRMED",
    "EFFECT_NOT_OBSERVED",
    "NOT_PROVABLE",
    "EVIDENCE_CONFLICT",
]


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


class HostRunnerActionResponse(BaseModel):
    """Response for daemon start/stop actions.

    VCR-0018-B / Phase 6B — ``accepted`` historically conflated
    "signal accepted by the OS" with "process actually exited". The Stop
    path now distinguishes the two so clients can render them as
    separate stages:

    - ``command_id`` is a stable per-stop identifier returned immediately
      on signal acceptance.
    - ``stop_outcome`` is the deferred outcome carried in the same
      response. Values: ``"signal_accepted"``, ``"exited"``,
      ``"still_running_after_2s"``. None for non-stop actions.
    - ``exit_reason`` carries the run's documented exit reason when the
      process actually exits.
    """

    accepted: bool
    process: HostRunnerProcessStatus
    command_id: str | None = None
    stop_outcome: str | None = None
    exit_reason: str | None = None
    rung_receipt: MutationRungReceipt | None = None
    rung_receipt_warnings: list[MutationRungReceipt] = Field(default_factory=list)
    mutation_attempt_id: str | None = None
    mutation_dispatch_state: MutationAttemptDispatchState | None = None
    idempotency_key: str | None = None
    idempotency_replayed: bool = False


class IdentityCoherenceConfirmation(BaseModel):
    """Operator confirmation for a Fresh-run symbol identity change.

    Unhashed deploy-admission evidence: the backend compares these symbols to
    the current request and the inherited instance symbol before allowing an
    immediate start through an incoherent redeploy.
    """

    inherited_symbol: str = Field(min_length=1)
    signal_stream: str | None = None
    action_plan_symbol: str | None = None


ExposureCoherencePosture = Literal["FLAT", "LONG", "SHORT", "MIXED", "UNKNOWN"]


class ExposureCoherenceFacts(BaseModel):
    posture: ExposureCoherencePosture
    pending_order_count: int | None = Field(default=None, ge=0)
    owned_positions: dict[str, int] = Field(default_factory=dict)
    source: str
    strategy_instance_id: str | None = None
    run_id: str | None = None


class ExposureCoherenceConfirmation(BaseModel):
    """Operator confirmation for starting despite inherited exposure evidence.

    This is unhashed deploy-admission evidence, not run identity. The public
    deploy endpoint compares it with the current instance exposure facts before
    allowing ``Deploy & start`` through a non-flat or unknown exposure state.
    """

    posture: ExposureCoherencePosture
    pending_order_count: int | None = Field(default=None, ge=0)
    owned_positions: dict[str, int] = Field(default_factory=dict)
    strategy_instance_id: str | None = None
    run_id: str | None = None


class HostRunnerDeployBaseRequest(BaseModel):
    """Common deploy request fields shared by public API and host daemon.

    ``account_id`` is deliberately absent here. The public data-plane API
    derives it from connected broker evidence; the host-daemon request carries
    the derived value after that boundary has failed closed.
    """

    strategy_spec_path: str = Field(min_length=1)
    qc_audit_copy_path: str = Field(min_length=1)
    qc_cloud_backtest_id: str = Field(min_length=1)
    start_date_ms: int = Field(ge=0)
    strategy_instance_id: str = ""
    # The hand-coded algorithm module the run starts under (#416). Recorded in
    # the ledger so the console defaults the Start card and `run start` rejects a
    # mismatched --strategy. Optional; "" leaves the run unguarded (legacy).
    strategy_key: str = ""
    live_config: dict = Field(default_factory=dict)
    force: bool = False
    # When true, chain a host-runner start after a successful create.
    start: bool = False
    start_options: HostRunnerStartRequest = Field(default_factory=HostRunnerStartRequest)
    # PRD #593 Slice 1E (#598) / ADR 0012 §7 — redeploy lineage. Both
    # fields are **unhashed**: they are persisted in the ledger's
    # ``lineage`` block alongside other unhashed metadata (``code_sha``,
    # ``sizing_provenance``, ``created_at_ms``) but are NOT in
    # ``LIVE_CONFIG_LEDGER_KEYS`` and NOT in ``compute_run_id``.
    # Otherwise re-deploying the same plan from two different parents
    # would mint two ``run_id``s and break the idempotent-redeploy
    # contract Slice 1A pinned.
    parent_run_id: str | None = None
    redeploy_reason: str | None = None

    @field_validator("live_config", mode="after")
    @classmethod
    def _validate_live_config(cls, value: dict) -> dict:
        """Delegate deploy-domain normalization outside the schema model."""
        if not isinstance(value, dict):
            return value
        from app.engine.live.deploy import (
            DeployError,
            validate_and_normalize_deploy_config,
        )
        try:
            return validate_and_normalize_deploy_config(value)
        except DeployError as exc:
            raise ValueError(str(exc)) from exc


class LiveInstanceDeployRequest(HostRunnerDeployBaseRequest):
    """Public deploy request accepted by ``/api/live-instances``.

    Legacy clients may still send ``account_id``. The data-plane route treats
    it only as an optional consistency check and never forwards it as authority;
    the connected broker session authors the daemon payload.
    """

    model_config = ConfigDict(extra="allow")

    inherited_symbol: str | None = None
    inherited_symbol_source: str | None = None
    identity_coherence_confirmation: IdentityCoherenceConfirmation | None = None
    inherited_exposure_posture: ExposureCoherencePosture | None = None
    inherited_exposure_pending_order_count: int | None = Field(default=None, ge=0)
    inherited_exposure_positions: dict[str, int] = Field(default_factory=dict)
    inherited_exposure_source: str | None = None
    exposure_coherence_confirmation: ExposureCoherenceConfirmation | None = None
    presented_action: PresentedOperatorActionInvocation | None = None

    @model_validator(mode="after")
    def _validate_legacy_extras(self) -> LiveInstanceDeployRequest:
        extras = self.model_extra or {}
        unexpected = sorted(key for key in extras if key != "account_id")
        if unexpected:
            raise ValueError(f"unknown deploy request fields: {unexpected}")
        if "account_id" in extras:
            value = extras["account_id"]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("legacy account_id must be a non-empty string when provided")
        return self

    def client_supplied_account_id(self) -> str | None:
        value = (self.model_extra or {}).get("account_id")
        if not isinstance(value, str):
            return None
        return value.strip()


class HostRunnerDeployRequest(HostRunnerDeployBaseRequest):
    """Request body for creating a run via the daemon (ADR 0006).

    The daemon supplies ``repo_root`` / ``run_root`` from its own config — they
    are deliberately NOT client-chosen. ``strategy_spec_path`` and
    ``qc_audit_copy_path`` are resolved against the daemon's repo root and
    confined to it. The QC anchor (``qc_cloud_backtest_id`` +
    ``qc_audit_copy_path``) is required — a live run is never created without it.
    ``account_id`` is backend-authored by the public API boundary before this
    request reaches the daemon.
    """

    account_id: str = Field(min_length=1)


class HostRunnerDeployResponse(BaseModel):
    """Result of a deploy: the content-addressed run plus an optional chained
    start. ``created`` is ``False`` for an idempotent no-op (the run already
    existed with a matching ledger)."""

    run_id: str
    run_dir: str
    created: bool
    start: HostRunnerActionResponse | None = None


class QcAuditCopyListing(BaseModel):
    """Committed QC audit copies under ``references/qc-shadow`` (ADR 0006).

    ``entries`` are repo-relative POSIX paths suitable to pass straight back as
    a deploy's ``qc_audit_copy_path``. Empty when the directory is absent or the
    daemon is unreachable.
    """

    scope_root: str
    entries: list[str] = Field(default_factory=list)


class AuditCopySizingLookup(BaseModel):
    """ADR 0009 § 3 — deploy-form gate status for the Reference parity preset.

    Returned by the daemon's audit-copy-sizing lookup endpoint and surfaced to
    the deploy form's inline gate banner. Three verdicts:

    * ``proven_match`` — registered + sha re-verifies + proposed policy
      matches the registered rule (or no proposed policy was supplied, which
      is the deploy-form's pre-select case).
    * ``proven_mismatch`` — registered + sha re-verifies, but the proposed
      policy differs from the registered rule.
    * ``cannot_prove`` — entry absent, file missing, sha drift, or allow-list
      unavailable.
    """

    verdict: Literal["proven_match", "proven_mismatch", "cannot_prove"]
    # Operator-facing one-line summary; safe to render verbatim.
    detail: str
    # The registered rule (when known) and the proposed live rule (when sent),
    # both rendered as dicts via the same shape ``live_config.sizing`` uses.
    expected_rule: dict | None = None
    actual_rule: dict | None = None


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


class DesiredStateRecordResponse(BaseModel):
    """Persisted lifecycle intent returned by retained mutation receipts."""

    state: str
    updated_at_ms: int
    updated_by: str
    reason: str | None = None
    version: int


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


# --- ADR 0004: instance-addressed operator console ---


class InstanceProcessView(BaseModel):
    """Live process snapshot for a strategy instance, from the host-daemon
    registry (the live-binding authority). ``state`` is ``unreachable`` when
    the daemon could not be queried — distinct from ``idle`` (daemon reachable,
    nothing running)."""

    state: str  # running | stopping | exited | idle | unreachable
    pid: int | None = None
    ibkr_client_id: int | None = Field(default=None, ge=0)
    bound_run_id: str | None = None
    started_at_ms: int | None = None


class LiveBinding(BaseModel):
    """The run an instance is writing to *right now* (registry-sourced).

    Present only when a process is live. Commands route here and nowhere else.
    """

    run_id: str
    run_dir: str | None = None
    source: str = "registry"


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


SignalTone = Literal["ok", "warn", "neutral"]


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


class RedeployLineage(BaseModel):
    """PRD #593 Slice 1E (#598) / ADR 0012 §7 — unhashed redeploy
    lineage. Persisted in the ledger's ``lineage`` block alongside
    ``code_sha`` and ``sizing_provenance`` (NOT inside ``live_config``),
    so the fields stay out of the content hash that produces ``run_id``.

    Wire-shape mirror of the TypeScript ``ActionPlanLineage`` interface.
    """

    parent_run_id: str | None = None
    redeploy_reason: str | None = None
    # ``int64`` ms UTC wall-clock when the redeploy was issued.
    redeployed_at_ms: int | None = None


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
    """Durable evidence of a single cold-start reconciliation attempt.

    Written once per run by ``reconciliation_orchestrator.reconcile`` (PR 1
    of the cold-start gate) to ``<run_dir>/reconciliation_receipt.json``.
    Resume guards consult it to decide whether evidence is fresh.
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


class ActivityEvidenceRef(BaseModel):
    """Reference to a captured IBKR API request/callback observation.

    The Activity projection is operator-facing and normalized, but every
    row that comes from broker evidence can link back to the raw request /
    response snapshot captured by the full broker API diagnostics recorder.
    """

    source: str
    seq: int
    ts_ms: int
    request_call: str
    response_callback: str | None = None
    order_ref: str | None = None
    order_id: int | None = None
    perm_id: int | None = None
    exec_id: str | None = None
    symbol: str | None = None


class ActivityBrokerEventRow(BaseModel):
    """Normalized broker event ledger row for the selected session date."""

    id: str
    visible_row_id: str | None = None
    ts_ms: int
    row_type: str
    display_type: str | None = None
    source: str
    source_label: str | None = None
    symbol: str | None = None
    side: Literal["BUY", "SELL"] | None = None
    quantity: float | None = None
    price: float | None = None
    status: str | None = None
    summary: str
    verdict: str
    replay_count: int = 1
    fold_key: str | None = None
    fold_count: int = Field(default=1, ge=1)
    cluster_key: str | None = None
    cluster_label: str | None = None
    child_evidence_ids: list[str] = Field(default_factory=list)
    constituent_fill_ids: list[str] = Field(default_factory=list)
    evidence: list[ActivityEvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_visible_contract(self) -> ActivityBrokerEventRow:
        if not self.visible_row_id:
            self.visible_row_id = self.id
        if self.display_type is None:
            self.display_type = self.row_type.replace("_", " ").title()
        if self.source_label is None:
            self.source_label = self.source.replace("_", " ").title()
        return self


class ActivityReconciliationWarning(BaseModel):
    """Fail-honest warning when lifecycle derivation cannot be trusted."""

    code: str
    message: str
    row_ids: list[str] = Field(default_factory=list)


class LiveInstanceSummary(BaseModel):
    """One row in the account fleet overview.

    PRD #616 added ``readiness_verdict`` and ``readiness_as_of_ms`` so
    the fleet roster can render an honest status badge
    (``dep_val_smoke_001 · IDLE · BLOCKED``) for background instances
    without an N+1 fetch of every instance's full status.  Backend
    authors these from the same readiness source as the per-instance
    status endpoint.  ``UNKNOWN`` is the honest answer when readiness
    cannot be resolved (no run, no engine).
    """

    strategy_instance_id: str
    process_state: str
    bound_run_id: str | None = None
    latest_run_id: str | None = None
    desired_state: str | None = None
    readiness_verdict: Literal["READY", "BLOCKED", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    readiness_as_of_ms: int | None = None
    blockers: list[OperatorBlocker] = Field(default_factory=list)


class FleetRosterSnapshot(BaseModel):
    """Versioned fleet roster snapshot for REST and SSE consumers.

    The roster is authored by the same shared fleet-daemon observation used by
    per-bot SurfaceHub producers, so adding a streaming client never creates an
    extra host-daemon polling cadence.
    """

    stream_epoch: str = ""
    surface_version: int = Field(default=0, ge=0)
    fetched_at_ms: int
    daemon_fetched_at_ms: int | None = None
    instances: list[LiveInstanceSummary] = Field(default_factory=list)


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


class IntentActuation(BaseModel):
    """Result of actuating durable intent against the live binding (ADR 0004).

    ``actuated`` is true only when a command was queued on a live run. With no
    live binding the durable write still gates the next start. ``effect_state``
    keeps accepted intent distinct from its observed runtime effect: a durable
    request remains ``PENDING`` until the engine can observe it, while a queued
    command is only queued, not proof that the engine has applied it.
    """

    actuated: bool
    effect_state: Literal["QUEUED", "PENDING"] = "PENDING"
    run_id: str | None = None
    command_seq: int | None = None
    detail: str

    @model_validator(mode="after")
    def _derive_effect_state(self) -> IntentActuation:
        if self.actuated and self.effect_state != "QUEUED":
            return self.model_copy(update={"effect_state": "QUEUED"})
        return self


class SetInstanceDesiredStateResponse(BaseModel):
    """Single intent knob: durable write first, then live actuation if bound."""

    durable: DesiredStateRecordResponse
    actuation: IntentActuation
    rung_receipt: MutationRungReceipt
    rung_receipt_warnings: list[MutationRungReceipt] = Field(default_factory=list)
    mutation_attempt_id: str
    mutation_dispatch_state: MutationAttemptDispatchState


class EndDayIntentResponse(SetInstanceDesiredStateResponse):
    """End-day receipt with durable PAUSED intent and a separate clock-out effect."""

    process: InstanceProcessView
    command_id: str | None = None
    stop_outcome: str
