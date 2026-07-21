"""Pydantic v2 schemas for live-runs API.

Models for representing live paper-trading run state, decisions, executions,
trades, and artifacts. All timestamps are int64 milliseconds UTC.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.broker.ibkr.models import IbkrMinuteBar
from app.engine.live.daemon_transport import DaemonResultKind
from app.operator.notices.schema import (
    OperatorNotice,
    OperatorNoticeAction,
    OperatorNoticeActionability,
    OperatorNoticeRemedyStatus,
    OperatorNoticeTier,
    RuntimeFreshnessReasonCode,
    validate_actionability_action_pairing,
)
from app.schemas.account_condition_actions import AccountCureAction
from app.schemas.operator_blocker import OperatorBlocker, OperatorConfirmationCopy


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
    # for the cockpit to author a useful remedy; typed fields let the operator
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
    map on plus an ``incident_source`` for the cockpit's BROKER / APP /
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
    # the cockpit so the operator decides.
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

    Reaches the daemon's one-shot ``emergency-flatten`` CLI independent of any
    live binding, so an operator can flatten after a halt/poison (when the
    binding-gated console FLATTEN command is unavailable). ``account`` must echo
    the IBKR account id — defense-in-depth mirroring the CLI ``--account`` gate,
    which refuses if it does not match the connected account.
    """

    account: str = Field(..., min_length=2, max_length=32)
    confirm: bool = Field(..., description="Must be true; typo-proofing gate.")


class AccountEmergencyFlattenResponse(BaseModel):
    """Receipt returned after the account-scoped emergency CLI completes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: bool
    account_id: str = Field(min_length=2, max_length=32)
    audit_run_id: str = Field(min_length=2, max_length=128)
    completed_at_ms: int = Field(ge=0)


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

    @field_validator("ibkr_host")
    @classmethod
    def _validate_ibkr_host(cls, value: str) -> str:
        return _validate_bare_ibkr_host(value)


class HostRunnerStopRequest(BaseModel):
    """Request body for stopping the active host runner subprocess."""

    force: bool = False


class MutationOutcomeUnknownResponse(BaseModel):
    """Typed 409 body for single-shot mutations whose transport outcome
    could not be proven (PRD #619-C5).

    Surfaced by ``deploy_instance`` / ``start_run`` / ``stop_run`` /
    ``emergency_flatten_instance`` / ``renew_daemon_lease`` when the typed daemon POST returns
    ``DaemonResult.kind == "UNREACHABLE"`` with
    ``outcome_ambiguous=True`` — i.e., the request was (partly or
    fully) sent but the response was lost.  The mutation may or may not
    have executed on the daemon side.

    Distinct from 503 ``host daemon unreachable`` (clean pre-send
    failure where retry is safe). 409 CONFLICT signals "eligibility is
    indeterminate" — the operator must refresh state before retrying.

    The durable ``mutation_attempt`` record + Reconcile action + the
    action-conflict matrix in ``operator_surface.actions`` are 619-D's
    job; C5 is the synchronous surfacing pass.
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
    # Canonical endpoint label so the cockpit can show the right copy
    # ("deploy" / "start_run" / "stop_run" / "emergency_flatten" /
    # "renew_daemon_lease").
    endpoint: Literal[
        "deploy",
        "start_run",
        "stop_run",
        "end_day_now",
        "emergency_flatten",
        "renew_daemon_lease",
    ]
    # ``int64 ms UTC`` of the failure.
    occurred_at_ms: int = Field(ge=0)
    # Operator-language one-liner, server-authored per endpoint, telling
    # the operator what they need to do next (refresh state, do not
    # blindly retry).
    runbook_hint: str


class ReconcileMutationResponse(BaseModel):
    """PRD #619-D3 — typed response for the Reconcile action.

    Reconcile is **read-only** — it never replays the mutation. The
    response describes the *outcome* the pure classifier returned and
    the *terminal dispatch_state* the persisted attempt has been
    advanced to; the cockpit reads both and renders the operator
    runbook copy per code (D5).

    ``evidence`` mirrors the snapshot the classifier consumed; the
    operator's audit trail wants to know which facts drove the
    classification, separately from the outcome name.
    """

    model_config = ConfigDict(extra="forbid")

    mutation_attempt_id: str
    action: Literal["start", "stop", "flatten", "resume", "pause"]
    outcome: Literal[
        "EFFECT_CONFIRMED",
        "EFFECT_NOT_OBSERVED",
        "EVIDENCE_CONFLICT",
        "NOT_PROVABLE",
    ]
    dispatch_state: Literal[
        "EFFECT_CONFIRMED",
        "EFFECT_NOT_OBSERVED",
        "EVIDENCE_CONFLICT",
        "NOT_PROVABLE",
    ]
    evidence: dict
    reconciled_at_ms: int = Field(ge=0)


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


class MutationAttemptView(BaseModel):
    """Latest durable mutation receipt carried by the state snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    mutation_attempt_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    run_id: str | None = None
    action: Literal["start", "stop", "flatten", "resume", "pause"]
    requested_at_ms: int = Field(ge=0)
    creation_order: int = Field(default=0, ge=0)
    last_transition_at_ms: int = Field(ge=0)
    dispatch_state: MutationAttemptDispatchState
    outcome: dict | None = None
    evidence: dict | None = None


OperatorSurfaceBlockageStageId = Literal[
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
    rung_id: OperatorSurfaceBlockageStageId | None = None
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
    path now distinguishes the two so the cockpit can render them as
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
    def _validate_sizing(cls, value: dict) -> dict:
        """ADR 0009 / VCR-0001 — enforce an explicit sizing policy at the
        deploy boundary.

        Phase 1 closes the back door that let an empty ``live_config`` fall
        through to legacy ``SimpleFloorSizing`` (the ``set_holdings(SPY, 1.0)``
        → all-in path). New deploys must carry ``live_config.sizing`` and may
        only use the keys ``_live_config_from_ledger`` knows how to round-trip
        — anything else would be hashed into ``run_id`` and then refused at
        start, leaving an unstartable ledger on disk.

        Three gates:

        1. ``sizing`` is required. Empty / missing rejects.
        2. Unknown sibling keys reject (mirrors the ledger reader's allow-list).
        3. ``sizing`` round-trips through the ``SizingPolicy`` discriminated
           union and is re-serialized via ``policy_to_ledger_dict`` so the hash
           stays stable regardless of how the operator stringified ``Decimal``.
        """
        if not isinstance(value, dict):
            return value
        from app.engine.execution.order_sizer import (
            parse_sizing_policy,
            policy_to_ledger_dict,
        )
        from app.engine.live.config import LIVE_CONFIG_LEDGER_KEYS, normalize_allowed_sessions
        from app.schemas.action_plan import ActionPlan

        unknown = set(value.keys()) - LIVE_CONFIG_LEDGER_KEYS
        if unknown:
            raise ValueError(f"unknown live_config keys: {sorted(unknown)}")
        sizing = value.get("sizing")
        if sizing is None:
            raise ValueError(
                "live_config.sizing is required — Phase 1 / ADR 0009 closes the "
                "empty-live_config back door (VCR-0001). Submit an explicit "
                "policy (Safe canary: {'sizing': {'kind': 'FixedShares', 'value': 1}})."
            )
        policy = parse_sizing_policy(sizing)
        value["sizing"] = policy_to_ledger_dict(policy)
        if "action" in value:
            value["action"] = ActionPlan.model_validate(value["action"]).model_dump()
        if "allowed_sessions" in value:
            value["allowed_sessions"] = list(normalize_allowed_sessions(value["allowed_sessions"]))
        # ADR 0014 §6 — round-trip the reconciliation_timing_policy block
        # through its Pydantic model so the deploy boundary rejects
        # mis-shaped configs (e.g. excessive_lag_ms <= caveat_lag_ms) at
        # admission time, not at runtime when the publisher starts.
        if "reconciliation_timing_policy" in value:
            from app.schemas.broker_activity import ReconciliationTimingPolicy

            policy_block = value["reconciliation_timing_policy"]
            value["reconciliation_timing_policy"] = ReconciliationTimingPolicy.model_validate(policy_block).model_dump()
        return value


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


# --- PRD-A UI-1/UI-3/UI-4 contract additions ---


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


class DesiredStateAction(StrEnum):
    """Operator actions mapped to durable desired-state (UI-3)."""

    pause = "pause"
    resume = "resume"
    stop = "stop"


class SetDesiredStateRequest(BaseModel):
    """Body for POST /api/live-runs/{run_id}/desired-state."""

    action: DesiredStateAction
    reason: str = Field(default="", max_length=1024)
    updated_by: str = Field(default="operator", max_length=256)


class DesiredStateRecordResponse(BaseModel):
    """Persisted desired-state record returned after a write."""

    state: str
    updated_at_ms: int
    updated_by: str
    reason: str | None = None
    version: int


class EnqueueCommandRequest(BaseModel):
    """Body for POST /api/live-runs/{run_id}/commands."""

    verb: str = Field(description="PAUSE | RESUME | STOP | FLATTEN | MARK_POISONED | RECONCILE.")


class CommandView(BaseModel):
    """A single pending command in the timeline."""

    seq: int
    verb: str
    rung_receipt: MutationRungReceipt | None = None
    rung_receipt_warnings: list[MutationRungReceipt] = Field(default_factory=list)
    mutation_attempt_id: str | None = None
    mutation_dispatch_state: MutationAttemptDispatchState | None = None


class CommandAckView(BaseModel):
    """A single acknowledged command in the timeline."""

    seq: int
    verb: str
    outcome: dict


class CommandTimelineResponse(BaseModel):
    """Pending + ack timeline for a run's command channel (UI-4).

    Deprecated by ``CommandsTimeline`` (#397); retained for back-compat.
    """

    pending: list[CommandView]
    acks: list[CommandAckView]


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


class EvidenceBinding(BaseModel):
    """The instance's latest run by ledger — evidence only, never live.

    Rendered as stale/completed-run evidence when no process is bound. Never a
    command-routing authority.
    """

    run_id: str
    state: str = "latest_run_by_ledger"
    is_live: bool = False


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

    A gate result is the enforcement-backed predicate the cockpit can
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
    # engine readiness sidecar so the cockpit's
    # ``operator_surface.daily_order_cap`` projection consumes integers
    # rather than parsing the gate prose ``"3 / 50 orders used"``.  Both
    # ``None`` on start_readiness (backend-derived) and when no cap is
    # configured.
    orders_used: int | None = None
    orders_cap: int | None = None


class DecisionColumnDescriptor(BaseModel):
    """Operator-facing descriptor for one strategy-specific decision column (#396).

    Derived from the strategy spec so the console renders any strategy's
    indicators generically. ``format`` is decimal|integer|boolean|text.
    """

    name: str
    label: str
    type: str
    format: str
    semantic: str = ""


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
    # unrealized PnL for the operator-surface risk-chip. ``None`` when
    # the broker connector cannot resolve a value; the cockpit omits the
    # slot rather than rendering ``0.00`` (#611 §"Pinned risk-chip").
    unrealized_pnl: float | None = None


class InstanceStartDefaults(BaseModel):
    """Pre-filled Start-card values for the console (#416).

    The five ``run start`` knobs, defaulted so the operator never starts from a
    blank form. ``strategy`` is sourced from the run's ledger ``strategy_key``
    (the algorithm module the ledger is reconciled to) when present — empty
    string means a legacy ledger with no recorded key, so the field is
    operator-supplied. New ledgers may persist these from deploy-time
    ``start_options`` so a later cockpit start uses the same operator choices.
    """

    strategy: str = ""
    readonly: bool = True
    hydrate_policy: HydratePolicy = "require"
    max_orders_per_day: int = DEFAULT_MAX_ORDERS_PER_DAY
    ibkr_host: str = "127.0.0.1"
    # Re-deploy prefill: the bound run's ledger deploy identity, so the console
    # can deep-link the deploy form to recover a poisoned/halted instance with a
    # fresh run_id (the only recovery path) without the operator re-typing it.
    # Empty for legacy ledgers missing the field; the form then asks for it.
    strategy_spec_path: str = ""
    qc_audit_copy_path: str = ""
    qc_cloud_backtest_id: str = ""
    account_id: str = ""


class InstanceProvenance(BaseModel):
    """What a run's content-addressed identity attests to (ADR 0006).

    The ``run_id`` is ``sha256`` over a clean-tree git commit, the strategy spec
    + its SHA, the QC audit copy + its SHA, the QC backtest id, the account, and
    the start date — so identical inputs always yield the same id. Surfacing the
    inputs lets the console explain *what each fingerprint proves* (e.g. "the
    running algorithm is byte-identical to backtest X") instead of showing a bare
    hash. Sourced from the bound/evidence run's ledger; fields are empty/legacy
    ledgers contribute what they have.
    """

    run_id: str
    schema_version: str = ""
    code_sha: str = ""
    strategy_spec_path: str = ""
    strategy_spec_sha256: str = ""
    qc_audit_copy_path: str = ""
    qc_audit_copy_sha256: str = ""
    qc_cloud_backtest_id: str = ""
    account_id: str = ""
    start_date_ms: int | None = None
    created_at_ms: int | None = None
    # Runtime config hashed into run_id alongside the code/spec/QC inputs (symbol,
    # force_flat_at, consolidator_period_min, …). Surfaced so two runs that differ
    # ONLY in live_config don't show identical "proofs" despite distinct run_ids.
    live_config: dict = Field(default_factory=dict)


class InstanceLastExit(BaseModel):
    """Why the instance's most recent run ended.

    Composed from the run's ``run_status.json`` (exit code/reason) and, when
    present, the indicator-state hydration receipt. Surfaced on a terminated
    run so the console can explain *why* an instance is STOPPED — e.g. a cold
    start that failed under ``hydrate_policy=require`` shows
    ``hydration_failure_reason="missing"``, which the UI turns into seed-day
    guidance.
    """

    run_id: str
    ended_at_ms: int | None = None
    exit_code: int | None = None
    exit_reason: ExitReason | None = None
    exit_error_code: str | None = None
    exit_error_message: str | None = None
    exit_error_detail: dict[str, Any] = Field(default_factory=dict)
    # From indicator_state_hydration.json, when the run wrote one. ``accepted``
    # False with ``failure_reason="missing"`` is the cold-start/seed-day case.
    hydration_accepted: bool | None = None
    hydration_failure_reason: str | None = None
    # From poisoned.flag, when present: the SPECIFIC safety trigger that halted
    # the run (OUTSIDE_MUTATION / LOST_FILL / COLD_START_DIVERGENCE /
    # OPERATOR_DECLARED) + its forensic details, so the console can explain *what*
    # the engine detected rather than a generic "Safety halt".
    halt_trigger: str | None = None
    halt_at_ms: int | None = None
    halt_detail: dict | None = None


class SizingAuditRow(BaseModel):
    """ADR 0009 § 11 — one row of the per-trade audit list.

    ``sizing_provenance_at_resolve_time`` (VCR-0003 last-mile): the
    provenance stamp the engine mints at policy-resolution time per
    ADR 0009 § 11 — one of ``{reference_native, live_override,
    spec_default}``. Surfaced through the WAL fold so the per-trade
    audit can attribute each fill to the policy that produced it.
    ``None`` for legacy rows (SIZING_RESOLVED events authored before
    the field landed) and for skip rows (sizing_skip.jsonl predates
    this column; future revision may add it). Frontend renders an
    "unknown" badge when ``None``.

    ``reference_price`` is ``None`` when the sizing policy can resolve without
    a bar price, e.g. FixedShares. Consumers must render absence rather than
    inventing a price.

    ``skipped`` / ``skip_reason`` (Phase 8 / VCR-0003): present on
    rows folded from ``sizing_skip.jsonl``; absent for WAL rows.
    The Sizing card branch on ``skipped`` to render the "skipped"
    variant.
    """

    ts_ms: int
    symbol: str
    policy_kind: str
    policy_value: str
    intended_qty: int
    reference_price: str | None = None
    sized_via: str
    sizing_provenance_at_resolve_time: str | None = None
    skipped: bool | None = None
    skip_reason: str | None = None


class InstanceSizing(BaseModel):
    """ADR 0009 — sizing surface for the instance console's Sizing card.

    Surfaces the resolved policy from the bound (or latest evidence) run's
    ``live_config.sizing`` plus the two engine-derived ledger stamps. ``policy``
    is ``None`` for a **legacy/pre-policy run** (the ledger has no ``sizing``
    key); the Sizing card renders the degraded "Pre-policy run" badge variant
    in that case (ADR 0009 § 14).
    """

    # Canonical policy form (the same shape the operator submitted, after
    # Pydantic round-trips it through the discriminated union). ``None`` means
    # legacy/pre-policy — the UI shows the honest "pre-policy" badge.
    policy: dict | None = None
    # Operator-facing preset label inferred from the policy shape. Carried
    # explicitly so the UI doesn't re-derive it client-side. ``None`` for
    # pre-policy runs.
    preset: Literal["safe_canary", "reference_parity", "custom", "explicit"] | None = None
    governed_by: Literal["live_config", "strategy_explicit"]
    sizing_provenance: Literal["reference_native", "live_override", "spec_default"]
    # ADR 0009 § 11 — per-trade audit rows, newest first (capped server-side
    # at 50 rows). Empty for runs that predate the audit log.
    per_trade_audit: list[SizingAuditRow] = Field(default_factory=list)


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


HostProcessState = Literal[
    "RUNNING",
    "STOPPING",
    "EXITED",
    "IDLE",
    "WAITING_FOR_HOST",
    "UNREACHABLE",
]
PriorRunClassification = Literal["CLEAN", "HALT_TRIGGERED", "EXITED_WITH_ERROR", "UNKNOWN"]
BrokerSafetyVerdictEnum = Literal["PAPER_ONLY", "UNSAFE", "UNKNOWN"]
BrokerConnectionState = Literal["CONNECTED", "DISCONNECTED", "DEGRADED", "UNKNOWN"]
OperatorSurfaceConditionSeverity = Literal["ok", "info", "warning", "critical", "neutral"]
BrokerConnectionConditionCode = Literal[
    "BROKER_CONNECTED",
    "BROKER_DISCONNECTED",
    "BROKER_DISABLED",
    "BROKER_LINK_SOFT_LOST",
    "BROKER_SUBSCRIPTIONS_STALE",
    "BROKER_DATA_FARM_DEGRADED",
    "BROKER_RECONNECTING",
    "BROKER_RECOVERING",
    "BROKER_HARD_DOWN",
    "BROKER_RUNTIME_UNBOUND",
    "BROKER_CONNECTION_UNKNOWN",
]
ExecutionPosture = Literal["PAPER_EXECUTION", "READ_ONLY", "UNSAFE", "UNKNOWN"]
OperatorVerdict = Literal["READY", "ATTENTION", "UNKNOWN"]
RiskPosture = Literal["FLAT", "LONG", "SHORT", "MIXED", "UNKNOWN"]
ActionPlanConsumption = Literal["ACTIVE", "DECLARATIVE_ONLY", "UNKNOWN"]
TradingSessionPhase = Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]
AccountClerkPhase = Literal["accepting", "reconnecting", "draining", "frozen", "unknown"]
SubmitReadinessCode = Literal[
    "safe_to_submit",
    "safe_to_monitor",
    "blocked_before_submit",
    "broker_state_unproven",
    "account_frozen",
    "waiting_for_clerk_generation",
    "submit_outcome_uncertain",
]
TraderSituationCode = Literal[
    "ready_to_submit",
    "monitor_only",
    "submission_blocked",
    "broker_state_unproven",
    "account_frozen",
    "waiting_for_clerk_generation",
    "submit_outcome_uncertain",
    "attention_required",
    "unknown",
]
TraderAttentionSeverity = Literal["info", "warning", "critical"]


class OperatorSurfaceCurrentRisk(BaseModel):
    """Server-authored risk posture for the Current Risk card and the
    Configuration card's pinned risk-chip (#608 + #611).

    Replaces the Angular derivation in
    ``current-risk-card.component.ts`` that read ``owned_positions``
    directly.
    """

    posture: RiskPosture
    owned_positions: dict[str, int] = Field(default_factory=dict)
    # ``None`` when broker state is unavailable; ``0`` only when broker
    # state is known and empty.  The Frontend renders ``—`` for ``None``
    # and ``0`` for ``0`` (#612 §"Rendering rules").
    pending_order_count: int | None
    verdict: OperatorVerdict
    # PRD #611 contract dep on #608.  ``None`` when the broker connector
    # cannot supply a value.
    unrealized_pnl: float | None = None


class OperatorSurfaceDailyOrderCap(BaseModel):
    """Structured daily-order-cap usage for the Configuration card body
    (#608 + #611 + Slice 1 sidecar contract).

    The engine readiness sidecar emits ``orders_used`` / ``orders_cap``
    as structured fields alongside the existing gate ``detail`` prose;
    the projection consumes the structured values.  Either field is
    ``None`` when not configured / unavailable.
    """

    used: int | None
    limit: int | None


class OperatorSurfaceAccountClerk(BaseModel):
    """Account Clerk generation and lease health from canonical account artifacts.

    ``phase=unknown`` or ``lease_active=False`` means the account exists but
    Clerk write authority is not proven. The cockpit renders either as missing
    proof, not as healthy.
    """

    model_config = ConfigDict(extra="forbid")

    account_id: str
    generation: int | None = Field(default=None, ge=0)
    phase: AccountClerkPhase
    lease_active: bool = False
    recorded_at_ms: int | None = Field(default=None, ge=0)
    source: str | None = None


class OperatorSurfaceAccountObservation(BaseModel):
    """Backend-authored freshness proof for one broker account observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["VERIFIED", "REVOKED", "EXPIRED", "ABSENT"]
    reason_line: str
    observed_at_ms: int | None = Field(default=None, ge=0)
    valid_until_ms: int | None = Field(default=None, ge=0)


class OperatorSurfaceActionPlan(BaseModel):
    """Server-authored action-plan consumption + anomaly verdict (#608).

    ``consumption`` is ``ACTIVE`` when the stored action plan belongs to
    deployment-validation and matches the stock-only shape that live runner
    currently consumes (one long stock leg). Other present action plans remain
    ``DECLARATIVE_ONLY`` until their resolver/runtime path ships.
    ``anomaly_verdict`` is ``READY`` whenever an action plan is present
    because no detector exists yet. When the run's stored ``action_plan``
    is ``None``, both fields are ``UNKNOWN`` — a missing plan is evidence
    of nothing, not evidence of health.
    """

    consumption: ActionPlanConsumption
    anomaly_verdict: OperatorVerdict


class OperatorSurfaceConfiguration(BaseModel):
    """Server-authored configuration completeness verdict (#608).

    ``verdict`` is ``ATTENTION`` when any of the named configuration
    rules fail, ``READY`` when none fail, ``UNKNOWN`` when the inputs
    needed to evaluate any rule are themselves missing.
    ``reason_codes`` lists the failing rules in a stable
    ``ALL_CAPS_SNAKE`` vocabulary.
    """

    verdict: OperatorVerdict
    reason_codes: list[str] = Field(default_factory=list)


ActionEffect = Literal["DURABLE_ONLY", "LIVE_ACTUATION"]


class ActionCapability(BaseModel):
    """Per-action capability emitted by the shared capability evaluator
    (#608, extended by PRD #616).

    Used both by the status projection (``operator_surface.actions.*``)
    and by mutation endpoints which re-evaluate eligibility server-side
    before executing — a stale snapshot must not be exploitable, so the
    same function is the authority on both sides.

    The ``effect`` discriminator distinguishes durable-intent writes
    (always succeed, gate the next host start) from live actuation
    (requires a bound runner).

    ``disabled_reason_code`` carries the **highest-priority** code for
    the single-line tooltip; ``disabled_reasons`` carries the full
    priority-ordered list so the cockpit's expanded view shows every
    applicable reason.  When ``enabled is True`` both are ``None`` /
    ``[]``.
    """

    enabled: bool
    effect: ActionEffect
    disabled_reason_code: str | None = None
    disabled_reasons: list[str] = Field(default_factory=list)
    # Canonical gate result rows backing this action affordance.
    gate_results: list[GateResult] = Field(default_factory=list)


class OperatorSurfaceActions(BaseModel):
    """The five canonical cockpit actions (ADR-0010 / PRD #616).

    Resume, Pause, and Stop are durable-intent writes guarded by the
    shared ``ResumeGuardState`` resolver (broker safety verdict,
    reconciliation receipt, uncertain-intent WAL).  Flatten-and-pause
    requires a live binding plus owned positions.  Mark-poisoned
    requires a live binding (the canonical render site is the Audit
    tab; PRD #617).

    Frontend renders each affordance's enabled state and tooltip from
    these capabilities.  ``disabled_reasons`` carries the full
    priority-ordered list of applicable codes; the single-line
    tooltip renders ``disabled_reasons[0]`` (or
    ``disabled_reason_code`` as a back-compat shorthand for the
    head).
    """

    resume: ActionCapability
    pause: ActionCapability
    stop: ActionCapability
    flatten_and_pause: ActionCapability
    mark_poisoned: ActionCapability


class OperatorSurfaceConfirmations(BaseModel):
    """Backend-authored confirmation copy for Bot Cockpit safety actions."""

    model_config = ConfigDict(extra="forbid")

    mark_poisoned: OperatorConfirmationCopy
    crash_recovery_override: OperatorConfirmationCopy
    retire_replace: OperatorConfirmationCopy
    remove_bot: OperatorConfirmationCopy


class OperatorSurfaceBroker(BaseModel):
    """Server-authored broker block — two independent enums for the
    banner SAFETY pill and the tagline's "Broker: CONNECTED" half
    (PRD #607 / cockpit revision 2026-06-21).

    ``safety_verdict`` is whether the cockpit is allowed to trade
    against this account (ADR-0011: paper-only vs unsafe vs unknown).
    ``connection`` is whether the broker session is up.  They are
    independent: a paper-only account whose IBKR session is reconnecting
    is ``safety_verdict=PAPER_ONLY`` AND ``connection=DEGRADED``;
    composing them into a single enum collapses two facts the operator
    needs to read separately.
    """

    safety_verdict: BrokerSafetyVerdictEnum
    connection: BrokerConnectionState
    connection_condition: OperatorSurfaceNamedCondition


class OperatorSurfaceExecution(BaseModel):
    """Backend-authored execution posture for trader-facing chips.

    This is an authored translation of the engine runtime's
    ``effective_posture``. Angular must render this field when present;
    it must not infer execution posture from broker safety, readonly
    flags, action effects, or host state.
    """

    model_config = ConfigDict(extra="forbid")

    posture: ExecutionPosture


class OperatorSurfaceTradingSession(BaseModel):
    """Server-authored trading-session projection
    (PRD #607 / cockpit revision 2026-06-21).

    The server owns session boundaries (the strategy's configured
    session policy, exchange-aligned bar starts, etc.); Angular only
    advances and formats the visible HH:MM:SS clock from its local
    wall-clock.  Hard-coding RTH in Angular is forbidden because
    every future strategy may declare different hours.

    ``permits_strategy_activity`` is the boolean the cockpit reads to
    decide whether the clock pill should read calm-green vs muted; it
    is server-derived from the phase + the strategy's session policy
    rather than the cockpit deriving it from the phase enum.
    """

    phase: TradingSessionPhase
    permits_strategy_activity: bool | None = None
    next_transition_ms: int | None = None
    timezone: str = "America/New_York"
    as_of_ms: int


class OperatorSurfacePriorRun(BaseModel):
    """Server-authored classification of the instance's last terminated
    run (#608).

    Replaces the Angular logic in ``broker-instances.component.ts`` and
    ``sticky-control-bar.component.ts`` that interprets ``exit_code``,
    ``exit_reason``, and ``halt_trigger`` to drive the LAST RUN banner
    pill.  Mapping rules are documented in #608 and pinned by the unit
    tests under ``tests/services/test_operator_surface.py``.
    """

    classification: PriorRunClassification


class OperatorSurfaceNamedCondition(BaseModel):
    """Backend-authored condition copy for an operator-facing status fact.

    The coarse enum remains available for compatibility and broad gating;
    this condition carries the exact trader-facing meaning so Angular does
    not reverse-map raw transport states into safety copy.
    """

    model_config = ConfigDict(extra="forbid")

    code: BrokerConnectionConditionCode
    severity: OperatorSurfaceConditionSeverity
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)
    remediation: str | None = Field(default=None, max_length=500)


HostProcessStartDisabledReasonCode = Literal[
    "ALREADY_RUNNING",
    "STOPPING",
    "HOST_SERVICE_OFFLINE",
    "STOPPED_REQUIRES_RESUME",
    "STOPPED_REQUIRES_REDEPLOY",
    "START_SETTINGS_INCOMPLETE",
    "ACCOUNT_FROZEN",
    "ACCOUNT_EVIDENCE_STALE",
    "CRASH_RECOVERY_REQUIRED",
]


class HostProcessStartCapability(BaseModel):
    """Server-authored per-instance Start-bot-process affordance
    (ADR-0006 §1 / ADR-0007 / ADR 0013 amendment 2026-06-22).

    Drives the cockpit's "Start bot process" button. The data-plane proxy
    re-runs the same enable check before forwarding the POST to the
    authenticated daemon endpoint, so a stale ``enabled=True`` cannot
    bypass the gate. When enabled, ``run_id`` and ``request`` together
    carry the exact POST the cockpit will fire — Angular never composes
    the body (design "Architectural permission for Start bot process").
    """

    enabled: bool
    # The run to start (``POST /runs/{run_id}/start``). Populated only
    # when ``enabled`` is True; the data-plane proxy re-verifies before
    # forwarding to the daemon.
    run_id: str | None = None
    # Server-authored request body. Built from ``InstanceStartDefaults`` /
    # the bound run's ledger; absent (``None``) when ``enabled`` is False.
    request: HostRunnerStartRequest | None = None
    # Closed reason code; present iff ``enabled`` is False.
    disabled_reason_code: HostProcessStartDisabledReasonCode | None = None
    # Canonical gate result rows backing the Start affordance.
    gate_results: list[GateResult] = Field(default_factory=list)


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
    The cockpit reads it to project ``operator_surface.reconciliation``;
    Resume gates consult it to decide whether evidence is fresh.
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


class OperatorSurfaceHostProcess(BaseModel):
    """Server-authored host-process surface (ADR-0003 / ADR-0006 / ADR-0007).

    The host *service* is operator-owned: the trader runs a deployment
    command to start it when it is UNREACHABLE.  The host-managed per-bot
    *subprocesses* are different — the cockpit launches them through the
    authenticated ``POST /runs/{run_id}/start`` path defined by ADR-0006
    and secured by ADR-0007 (surfaced as ``start_capability``).  This
    block exists so the cockpit can render an honest "bot is not running"
    notice, a per-instance Start affordance, and (for UNREACHABLE only) a
    copyable host-service start command — without Angular ever
    constructing the command or the start request itself.
    """

    state: HostProcessState
    # Operator-language line authored server-side when ``state != RUNNING``.
    # ``None`` when no notice is appropriate (typically when running).
    notice: str | None = None
    # Exact host command the operator can paste. Authored ONLY for
    # ``state == UNREACHABLE`` and only when trusted deployment
    # configuration supplies a non-empty value
    # (``IbkrSettings.live_runner_host_start_command``). Other states do
    # not get a daemon-start command because starting the daemon does not
    # restart an exited per-bot subprocess — those use ``start_capability``.
    # Angular renders verbatim and MUST NOT construct, interpolate, or
    # transform this string. ADR 0013 amendment 2026-06-22; design doc
    # "Deployment-model decision".
    copyable_command: str | None = None
    # Typed last-exit evidence promoted from ``run_status.json``. This lets the
    # operator surface keep broker-startup failures specific after the process
    # has already exited.
    last_exit_error_code: str | None = None
    last_exit_error_message: str | None = None
    last_exit_error_detail: dict[str, Any] = Field(default_factory=dict)
    # Per-instance Start-bot-process button. Always present so the cockpit
    # can render a disabled state with a server-authored reason.
    start_capability: HostProcessStartCapability


class InvokeCapabilityAction(BaseModel):
    """Suggested action: invoke a non-destructive capability inline.

    Permitted capabilities are non-destructive only — destructive
    actions (Stop, Mark Poisoned, Flatten-and-pause) never appear via
    ``invoke_capability``; they reach the operator only through
    ``focus_action`` so they keep their canonical render site.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["invoke_capability"]
    capability: Literal["resume", "pause"]


class FocusAction(BaseModel):
    """Suggested action: navigate to a tab and focus a specific
    affordance.  Destructive actions reach the operator only this way."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["focus_action"]
    tab: Literal["status", "activity", "audit", "configuration"]
    action: Literal["flatten_and_pause", "stop", "mark_poisoned"]


class RedeployAction(BaseModel):
    """Suggested action: navigate to the Configuration tab and start a
    Redeploy (the only path that revives a STOPPED instance — ADR-0010
    §4)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["redeploy"]


class OpenRunbookAction(BaseModel):
    """Suggested action: open an operator runbook (server-authored slug)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["open_runbook"]
    slug: str


class InvokeEndpointAction(BaseModel):
    """Suggested action: invoke an existing backend endpoint by stable name."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["invoke_endpoint"]
    endpoint: Literal["reconcile_instance"]
    method: Literal["POST"] = "POST"
    path_template: Literal["/api/live-instances/{strategy_instance_id}/reconcile"] = (
        "/api/live-instances/{strategy_instance_id}/reconcile"
    )


class NoPrimaryRemediationAction(BaseModel):
    """No primary remediation is appropriate for the current healthy state."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["none"]
    reason: str


GateSuggestedAction = Annotated[
    InvokeCapabilityAction | FocusAction | RedeployAction | OpenRunbookAction,
    Field(discriminator="kind"),
]

TraderPrimaryRemediation = Annotated[
    InvokeCapabilityAction
    | FocusAction
    | RedeployAction
    | OpenRunbookAction
    | InvokeEndpointAction
    | NoPrimaryRemediationAction,
    Field(discriminator="kind"),
]


class OperatorSurfaceEvidenceFact(BaseModel):
    """Raw fact rendered in the right pane's Advanced evidence drawer."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    source: str | None = None
    gate_id: str | None = None
    ts_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    ts_ms_resolved: bool = False

    @model_validator(mode="after")
    def _timestamp_resolution_contract(self) -> OperatorSurfaceEvidenceFact:
        if self.ts_ms is None and self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved cannot be true when ts_ms is absent")
        if self.ts_ms is not None and not self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved=false is reserved for absent timestamps")
        return self


class OperatorSurfaceAttentionGroup(BaseModel):
    """One independent fact that must remain visible alongside the summary."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: TraderAttentionSeverity
    headline: str
    explanation: str
    operator_next_step: str
    remediation: TraderPrimaryRemediation


class OperatorSurfaceProofLine(BaseModel):
    """Backend-authored read-only proof line for the operator UI."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    message: str
    detail: str
    tone: Literal["neutral", "ok", "attention"]


_OPERATOR_SURFACE_PROOF_LINE_IDS = (
    "broker-proof",
    "submit-readiness",
    "account-clerk",
    "reconciliation",
    "runtime-freshness",
)


class OperatorSurfaceSubmitReadiness(BaseModel):
    """Backend-authored answer to "Can this bot submit an order now?"."""

    model_config = ConfigDict(extra="forbid")

    code: SubmitReadinessCode
    label: str
    explanation: str
    can_submit: bool
    blocking_reason_codes: list[str] = Field(default_factory=list)
    template_id: str
    template_version: int = 1


class OperatorSurfaceTraderGuidance(BaseModel):
    """Backend-authored trader semantics for the Overview right pane."""

    model_config = ConfigDict(extra="forbid")

    situation_code: TraderSituationCode
    headline: str
    explanation: str
    risk_headline: str
    risk_explanation: str
    primary_remediation: TraderPrimaryRemediation
    additional_attention_groups: list[OperatorSurfaceAttentionGroup] = Field(default_factory=list)
    proof_lines: list[OperatorSurfaceProofLine]
    advanced_evidence: list[OperatorSurfaceEvidenceFact] = Field(default_factory=list)
    template_id: str
    template_version: int = 1

    @field_validator("proof_lines")
    @classmethod
    def _proof_lines_are_canonical(
        cls,
        proof_lines: list[OperatorSurfaceProofLine],
    ) -> list[OperatorSurfaceProofLine]:
        proof_line_ids = [line.id for line in proof_lines]
        expected_ids = list(_OPERATOR_SURFACE_PROOF_LINE_IDS)
        if proof_line_ids != expected_ids:
            raise ValueError(f"proof_lines must contain canonical ids in order: {expected_ids}")
        return proof_lines


OperatorSurfaceBlockageState = Literal["clear", "info", "warning", "danger", "unknown"]
OperatorSurfaceRunSignalTone = Literal["on", "off", "transition", "attention"]


class OperatorSurfaceBlockageStage(BaseModel):
    """One backend-authored rung in the current blockage ladder.

    The ladder is the compact answer to "what is blocking the bot now?"
    It deliberately uses the same coarse severities as named conditions while
    keeping trader-facing title/summary copy server-authored.
    """

    model_config = ConfigDict(extra="forbid")

    id: OperatorSurfaceBlockageStageId
    label: str
    state: OperatorSurfaceBlockageState
    severity: OperatorSurfaceConditionSeverity
    current: bool = False
    title: str
    summary: str
    next_step: str | None = None
    reason_codes: list[str] = Field(default_factory=list)


class OperatorSurfaceBlockageLadder(BaseModel):
    """Backend-authored lifecycle/blockage overview for the About pane."""

    model_config = ConfigDict(extra="forbid")

    headline: str
    summary: str
    current_stage_id: OperatorSurfaceBlockageStageId | None = None
    stages: list[OperatorSurfaceBlockageStage] = Field(default_factory=list)


class OperatorSurfaceRunSignal(BaseModel):
    """Backend-authored compact run-state signal for Bot Control.

    This is the one-line answer to "is this bot process on?"  The
    title/detail fields are operator-facing prose and must remain
    backend-authored; the cockpit renders them without deriving copy from
    raw process enums.
    """

    model_config = ConfigDict(extra="forbid")

    state_label: str
    tone: OperatorSurfaceRunSignalTone
    title: str
    detail: str


class OperatorGate(BaseModel):
    """Operator-facing projection of an engine readiness gate (PRD #616).

    The engine's ``ReadinessGate`` carries name / status / severity /
    detail.  ``OperatorGate`` adds a canonical ``GateResult`` plus
    server-authored remediation metadata so the cockpit never infers a
    "fix" from the gate name.

    Either ``suggested_action`` is present (a structured, closed-union
    descriptor), or it is ``None`` AND
    ``suggested_action_unavailable_reason`` is populated with a stable
    rationale code (so ``None`` is never ambiguous).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: str  # pass | fail | unknown
    severity: str  # hard | soft
    detail: str
    gate_result: GateResult
    suggested_action: GateSuggestedAction | None = None
    suggested_action_unavailable_reason: str | None = None


class OperatorSurfaceDomainFreshness(BaseModel):
    """One backend-authored runtime domain freshness verdict."""

    state: Literal["FRESH", "STALE", "NOT_APPLICABLE", "UNKNOWN", "DEGRADED"]
    age_ms: int | None = None
    stale_reason_codes: list[RuntimeFreshnessReasonCode] = Field(default_factory=list)


class OperatorSurfaceRuntimeFreshness(BaseModel):
    """Child runtime freshness rendered verbatim by the cockpit."""

    posture_demoted: bool
    stale_reason_codes: list[RuntimeFreshnessReasonCode] = Field(default_factory=list)
    command_loop: OperatorSurfaceDomainFreshness
    broker: OperatorSurfaceDomainFreshness
    bar_loop: OperatorSurfaceDomainFreshness
    control_plane: OperatorSurfaceDomainFreshness
    headline: OperatorNotice | None = None
    additional_reasons: list[OperatorNotice] = Field(default_factory=list)


class OperatorSurfaceControlPlane(BaseModel):
    """Server-authored control-plane (host-daemon) connectivity surface
    (PRD #619 §C).

    The control plane is the data plane's typed HTTP transport to the
    host live-runner daemon. This block surfaces the outcome of the most
    recent daemon poll plus the context an operator needs to diagnose a
    connectivity incident. It is intentionally distinct from
    ``broker.connection`` (the daemon→broker session) and ``host_process``
    (the host runner process the daemon supervises). Composing them
    collapses three independent facts the operator must read separately.

    Authority pattern matches the rest of ``OperatorSurface``: the
    backend authors every field including the operator-language
    ``notice`` and the ``runbook_slug``. Angular renders the strings
    verbatim and MUST NOT compose them from the enum.

    Fields:

    - ``state`` is the ``DaemonResultKind`` produced by the connectivity
      monitor (619-C2). Closed set: ``CONNECTED`` / ``RETRYING`` /
      ``UNREACHABLE`` / ``AUTH_FAILED`` / ``PROTOCOL_ERROR`` /
      ``INCOMPATIBLE_CONTRACT``.
    - ``last_transition_ms`` — ``int64 ms UTC`` of the last ``state``
      change, or ``None`` if the monitor has not yet observed a
      transition.
    - ``last_success_ms`` — ``int64 ms UTC`` of the most recent
      ``CONNECTED`` probe, or ``None`` if no successful poll yet.
    - ``attempt`` — retry-budget counter from the monitor: incremented
      on each failure within the budget window, ``0`` on success, pinned
      at the budget once exhausted.
    - ``daemon_boot_id`` — daemon ``boot_id`` observed on the most
      recent ``CONNECTED`` poll, ``None`` until the first successful
      poll or when the daemon does not declare one.
    - ``notice`` — operator-language prose authored server-side when
      ``state != CONNECTED``. ``None`` when the channel is healthy.
    - ``runbook_slug`` — stable short slug (e.g. ``"daemon-unreachable"``)
      keyed in the operator runbook. ``None`` when no runbook applies.
    """

    model_config = ConfigDict(extra="forbid")

    state: DaemonResultKind
    last_transition_ms: int | None = Field(default=None, ge=0)
    last_success_ms: int | None = Field(default=None, ge=0)
    attempt: int = Field(default=0, ge=0)
    daemon_boot_id: str | None = None
    notice: str | None = None
    runbook_slug: str | None = None


class BrokerObservationConsistency(BaseModel):
    """PRD #619-D4 — server-authored divergence verdict for the operator.

    The data plane and the live-engine child each observe the IBKR
    broker connection independently.  ADR-0011 makes the *child*'s
    observation authoritative for the bound instance; the singleton
    is advisory.  When the two disagree, the operator must see the
    divergence prominently — *without* the child's posture being
    silently overwritten.

    ``verdict`` rules (computed by
    ``services.broker_observation_consistency.evaluate_broker_observation_consistency``):

    - ``CONSISTENT`` — both report the same non-empty account.
    - ``CONFLICTING`` — both report non-empty accounts that differ.
    - ``UNKNOWN`` — one observation is missing or stale (no child
      runtime yet, singleton disabled / disconnected, account
      empty).
    - ``NOT_COMPARABLE`` — the two are configured for different
      modes (paper vs live) and the comparison is not apples-to-
      apples; comparing accounts would mislead.

    Carried as an optional field on ``OperatorSurface``.  ``None``
    is the cockpit's signal to hide the card.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: Literal["CONSISTENT", "CONFLICTING", "UNKNOWN", "NOT_COMPARABLE"]
    child_account: str | None = None
    data_plane_account: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    compared_at_ms: int = Field(ge=0)


# ---------------------------------------------------------------------------
# Cold-start reconciliation projection (ADR-0008 §5 / PR 1).
# ---------------------------------------------------------------------------

ReconciliationState = Literal[
    "NOT_AVAILABLE",
    "IN_PROGRESS",
    "CLEAN",
    "ADOPTED",
    "STALE",
    "FAILED",
]
"""Operator-facing cold-start reconciliation state composed by the
operator-surface projection from the receipt + current freshness inputs."""


class OperatorSurfaceReconciliation(BaseModel):
    """Per-run cold-start reconciliation projection for the cockpit.

    The cockpit renders this verbatim — it does NOT derive verdicts from
    raw receipt fields. ``NOT_AVAILABLE`` is the post-orchestrator state
    when no receipt has landed yet (a fresh run before its first attempt
    completes, or a legacy run from before this PR shipped).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ReconciliationState
    failure_reason: str | None = None
    adopted_intent_ids: tuple[str, ...] = ()
    last_reconcile_ms: int | None = None
    sidecar_wal_seq: int | None = Field(default=None, ge=0)
    broker_observed_at_ms: int | None = Field(default=None, ge=0)


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
    ``facts`` carries the raw diagnostics; the cockpit never derives
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


class OperatorSurfaceNoticePlacement(BaseModel):
    """Backend-authored placement for operator notices.

    The cockpit renders these lists directly. It must not re-run dominance or
    tier/actionability placement rules locally.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    banner: OperatorNotice | None = None
    banner_fold_count: int = Field(default=0, ge=0)
    banner_folded: list[OperatorNotice] = Field(default_factory=list)
    attention: list[OperatorNotice] = Field(default_factory=list)
    quiet_status: list[OperatorNotice] = Field(default_factory=list)


class OperatorSurface(BaseModel):
    """Operator-facing projection of run state for the Terminal Cockpit
    (PRD #607 / Slice 1 / #608, extended by PRD #616).

    Single source of truth for operational verdicts, risk posture,
    structured daily-cap usage, action-plan consumption, broker safety
    verdict, prior-run classification, host-process state, per-action
    capability + reason codes, and the per-gate operator-facing
    remediation metadata.  Frontend renders these fields; it does not
    derive verdicts from raw fields.

    PRD #616 added ``readiness_gates`` (the ``OperatorGate``
    projection with structured ``suggested_action`` / unavailable
    reason) and ``actions.stop`` to the contract.  Both are additive;
    ``schema_version`` does NOT bump for additive fields (per the
    existing rule).
    """

    # Bump on breaking shape changes; additive fields (new capability,
    # new reason code) do NOT bump the version.
    schema_version: int = 2
    host_process: OperatorSurfaceHostProcess
    prior_run: OperatorSurfacePriorRun
    broker: OperatorSurfaceBroker
    execution: OperatorSurfaceExecution | None = None
    configuration: OperatorSurfaceConfiguration
    current_risk: OperatorSurfaceCurrentRisk
    daily_order_cap: OperatorSurfaceDailyOrderCap
    action_plan: OperatorSurfaceActionPlan
    account_clerk: OperatorSurfaceAccountClerk | None = None
    account_observation: OperatorSurfaceAccountObservation | None = None
    submit_readiness: OperatorSurfaceSubmitReadiness
    trader_guidance: OperatorSurfaceTraderGuidance
    # Backend-authored current blockage ladder for the lifecycle/About pane.
    # Additive field; schema version 2 also renames account_owner to account_clerk.
    blockage_ladder: OperatorSurfaceBlockageLadder
    # Backend-authored compact process signal rendered beside one-click
    # lifecycle controls. Additive field; schema version 2 also renames
    # account_owner to account_clerk.
    run_signal: OperatorSurfaceRunSignal
    actions: OperatorSurfaceActions
    confirmations: OperatorSurfaceConfirmations
    trading_session: OperatorSurfaceTradingSession
    # PRD #616 — operator-facing projection of the engine readiness
    # gates with server-authored remediation metadata.  Empty list when
    # the engine has no readiness vector (e.g. nothing-deployed).  The
    # ordering preserves the engine's gate order.
    readiness_gates: list[OperatorGate] = Field(default_factory=list)
    # OperatorBlocker is the shared deploy/control blocker atom. Empty when no
    # backend-authored operator blocker applies to this runtime surface.
    blockers: list[OperatorBlocker] = Field(default_factory=list)
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None = None
    # PRD #619-C3 — host-daemon connectivity surface. ``None`` when the
    # data plane was booted without a daemon URL (live_runner_daemon_url
    # empty); in that case the cockpit hides the control-plane card.
    control_plane: OperatorSurfaceControlPlane | None = None
    # PRD #619-D4 — broker observation divergence surface. ``None`` when
    # the comparison is impossible (e.g. nothing-ever-deployed) so the
    # cockpit hides the card; otherwise the four-way verdict tells the
    # operator whether the child and the data plane agree about which
    # broker account the instance is connected to. Never overwrites
    # the child's authoritative posture on ``broker``.
    broker_observation_consistency: BrokerObservationConsistency | None = None
    # ADR-0008 §5 / PR 1 — cold-start reconciliation projection. ``None``
    # when the comparison is impossible (no live binding, no run dir to
    # read the receipt from); otherwise an honest state token (CLEAN /
    # ADOPTED / STALE / FAILED / IN_PROGRESS / NOT_AVAILABLE). The cockpit
    # renders the banner from ``state``; raw receipt fields are intentionally
    # not surfaced — operators read the projection, not the receipt.
    reconciliation: OperatorSurfaceReconciliation | None = None
    # PR 2 — post-halt watchdog incident headline. ``None`` when no
    # unresolved uncertain-outcome watchdog incident exists for the run.
    # When set, the cockpit should surface this notice to the operator
    # until reconciliation completes and the incident is cleared.
    # PR 5/6 will wire the full incident UI; PR 2 plumbs the schema
    # only so cmd_start can surface the blocking condition.
    incident_headline: OperatorNotice | None = None
    # PR 5 — broker-activity publisher health surface. ``None`` when no
    # strategy instance is bound (e.g. nothing-ever-deployed) and no
    # publisher is registered for the current instance. The cockpit
    # replaces the implicit "Loading history…" spinner with the typed
    # state machine from this field.
    broker_activity_health: BrokerActivityHealth | None = None
    # ADR-0025 / PRD #972 — single dominant headline and tier × actionability
    # placement. Frontend consumes this projection verbatim.
    notice_placement: OperatorSurfaceNoticePlacement = Field(default_factory=OperatorSurfaceNoticePlacement)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v2_blockers(cls, value: object) -> object:
        """Keep persisted v2 ``run_status`` surfaces readable after routing was added."""

        if not isinstance(value, dict) or value.get("schema_version", 2) != 2:
            return value
        blockers = value.get("blockers")
        if not isinstance(blockers, list):
            return value
        upgraded = dict(value)
        upgraded["blockers"] = [
            {
                **blocker,
                "anchor": blocker.get("anchor", {"kind": "surface", "subject_key": None}),
                "audience": blocker.get("audience", "operator"),
            }
            if isinstance(blocker, dict)
            else blocker
            for blocker in blockers
        ]
        return upgraded


LifecycleChartStatus = Literal[
    "passed",
    "active",
    "blocked",
    "poison",
    "freeze",
    "inactive",
    "unknown",
]

LifecycleChartLane = Literal["bot", "account", "broker", "recovery"]
LifecycleChartActionability = Literal["operator-actionable", "system-only", "no-action-needed"]
LifecycleChartActionId = Literal[
    "start_process",
    "resume",
    "pause",
    "flatten_and_pause",
    "stop",
    "mark_poisoned",
    "redeploy",
]


class LifecycleChartReceipt(BaseModel):
    """One structured receipt behind a lifecycle node.

    Receipts are evidence display rows only. They do not add authority beyond
    the canonical artifacts and operator-surface facts that authored them.
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str
    headline: str | None = None
    detail: str | None = None
    unit: str | None = None
    source: str | None = None
    gate_id: str | None = None
    ts_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    ts_ms_resolved: bool = False

    @model_validator(mode="after")
    def _timestamp_resolution_contract(self) -> LifecycleChartReceipt:
        if self.ts_ms is None and self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved cannot be true when ts_ms is absent")
        if self.ts_ms is not None and not self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved=false is reserved for absent timestamps")
        return self


class LifecycleChartNode(BaseModel):
    """One backend-authored node in the bot lifecycle overview chart.

    The frontend may choose layout and styling, but it must not infer node
    truth from raw status fields. ``status`` and ``evidence_summary`` are the
    operator-facing facts authored by the backend.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    technical_label: str | None = None
    lane: LifecycleChartLane
    status: LifecycleChartStatus
    status_label: str
    operator_actionability: LifecycleChartActionability = "operator-actionable"
    summary: str | None = None
    why: str | None = None
    operator_next_step: str | None = None
    expandable: bool = False
    subgraph_id: str | None = None
    evidence_summary: str | None = None
    ts_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    ts_ms_resolved: bool = False
    receipts: list[LifecycleChartReceipt] = Field(default_factory=list)

    @model_validator(mode="after")
    def _timestamp_resolution_contract(self) -> LifecycleChartNode:
        if self.ts_ms is None and self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved cannot be true when ts_ms is absent")
        if self.ts_ms is not None and not self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved=false is reserved for absent timestamps")
        return self


class LifecycleChartEdge(BaseModel):
    """One backend-authored transition in the bot lifecycle chart."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    target: str
    status: LifecycleChartStatus
    label: str | None = None
    animated: bool = False
    source_handle: str | None = None
    target_handle: str | None = None


class LifecycleChartAction(BaseModel):
    """An action affordance associated with the current chart state."""

    model_config = ConfigDict(extra="forbid")

    id: LifecycleChartActionId
    label: str
    enabled: bool
    reason_code: str | None = None
    reason_headline: str
    reason_detail: str
    target_node_id: str | None = None
    tone: Literal["primary", "secondary", "danger"] = "secondary"


class LifecycleChartGraph(BaseModel):
    """A lifecycle graph. The global graph may link to focused subgraphs."""

    model_config = ConfigDict(extra="forbid")

    graph_id: str
    title: str
    primary_node_id: str
    nodes: list[LifecycleChartNode]
    edges: list[LifecycleChartEdge]


class BotLifecycleChartView(BaseModel):
    """Backend-authored lifecycle overview for the bot control Overview tab."""

    model_config = ConfigDict(extra="forbid")

    chart_id: str
    selected_bot_id: str
    title: str
    global_graph: LifecycleChartGraph
    subgraphs: dict[str, LifecycleChartGraph] = Field(default_factory=dict)
    actions: list[LifecycleChartAction] = Field(default_factory=list)
    only_fresh_run_available: bool = False


BotLifecyclePhaseValue = Literal["OFF_DUTY", "ON_DUTY", "RETIRED"]
BotLifecyclePresenceLabel = Literal["Off duty", "On duty", "Retired"]
BotLifecycleDisplayStatus = Literal[
    "Off duty",
    "Ready",
    "On duty",
    "Clocking out",
    "Sick bay",
    "Off roster",
    "Retired",
]
BotLifecycleActionId = Literal[
    "confirm_start",
    "end_day_now",
    "retire_replace",
    "add_to_roster",
    "take_off_roster",
]


class BotLifecycleAction(BaseModel):
    """One rendered lifecycle action.

    The Button Rule relies on actions being a closed vocabulary. A disabled
    action is not a graveyard button; it exists only when the backend must carry
    a refusal reason for the single primary action.
    """

    model_config = ConfigDict(extra="forbid")

    id: BotLifecycleActionId
    label: str
    enabled: bool = True
    reason: str | None = None
    offer_id: str | None = None
    expires_at_ms: int | None = None


class BotLifecycleCondition(BaseModel):
    """One open condition explaining why daily lifecycle shows Sick bay."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["account", "bot"]
    severity: Literal["warning", "critical"]
    title: str
    detail: str
    owner_label: str
    cure_action: AccountCureAction
    cure_label: str


class BotDailyLifecycleProjection(BaseModel):
    """Rev-3 daily lifecycle projection for one bot.

    ``phase`` tracks presence only. Health is derived by the evaluator from
    receipts and open conditions; the display status is the closed vocabulary the
    UI renders.
    """

    model_config = ConfigDict(extra="forbid")

    phase: BotLifecyclePhaseValue
    presence_label: BotLifecyclePresenceLabel
    display_status: BotLifecycleDisplayStatus
    attention_badge: Literal["Sick bay", "Ready", "Off roster"] | None = None
    reason: str | None = None
    on_roster: bool = True
    active_run_id: str | None = None
    latest_run_id: str | None = None
    drift_detected: bool = False
    conditions: list[BotLifecycleCondition] = Field(default_factory=list)
    primary_action: BotLifecycleAction | None = None
    ambient_actions: list[BotLifecycleAction] = Field(default_factory=list)


class BotRollCallSummary(BaseModel):
    """Fleet-level counts for the morning roll-call sheet."""

    model_config = ConfigDict(extra="forbid")

    ready: int = 0
    off_roster: int = 0
    sick_bay: int = 0
    on_duty: int = 0
    off_duty: int = 0
    retired: int = 0
    generated_at_ms: int | None = None
    session_date: str | None = None
    effective_stop_ms: int | None = None


class BotRollCallOffer(BaseModel):
    """One persisted roll-call start offer returned from the tick endpoint."""

    model_config = ConfigDict(extra="forbid")

    offer_id: str
    strategy_instance_id: str
    run_id: str
    session_date: str
    issued_at_ms: int
    expires_at_ms: int


class BotRollCallResponse(BaseModel):
    """Response for the operator/scheduler roll-call tick."""

    model_config = ConfigDict(extra="forbid")

    summary: BotRollCallSummary
    offers: list[BotRollCallOffer] = Field(default_factory=list)


class BotLifecycleRosterRequest(BaseModel):
    """Operator roster mutation for the next roll call."""

    model_config = ConfigDict(extra="forbid")

    on_roster: bool
    updated_by: str = Field(default="operator", min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class BotRetireReplaceRequest(BaseModel):
    """Retire & Replace attestation.

    Replacement is default-on: the endpoint retires this instance and the UI
    immediately continues to the deploy form with existing lineage defaults.
    """

    model_config = ConfigDict(extra="forbid")

    confirm_account_flat: bool
    replacement_requested: bool = True
    updated_by: str = Field(default="operator", min_length=1, max_length=128)
    reason: str = Field(default="Retire & Replace", min_length=1, max_length=500)


class BotLifecycleMutationResponse(BaseModel):
    """Response returned after a lifecycle write persist point."""

    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: str
    lifecycle: BotDailyLifecycleProjection


class BotAttendanceCell(BaseModel):
    """One per-session attendance marker rendered on the bot catalog."""

    model_config = ConfigDict(extra="forbid")

    session_date: str
    status: Literal["clean", "rested", "sick", "retired"]
    label: str
    receipt_ref: str | None = None


class BotEveningReportRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_instance_id: str
    label: str
    status: Literal["clean", "rested", "sick", "retired"]
    receipt_ref: str | None = None


class BotEveningReport(BaseModel):
    """Backend-authored day report for the fleet page."""

    model_config = ConfigDict(extra="forbid")

    session_date: str
    generated_at_ms: int
    clean_exits: int = 0
    rested: int = 0
    sick: int = 0
    retired: int = 0
    summary: str
    rows: list[BotEveningReportRow] = Field(default_factory=list)


class LiveInstanceStatus(BaseModel):
    """Instance-addressed status: the operator's control-room subject (ADR 0004).

    The current run is attached as evidence; the ``live_binding`` is the only
    thing commands may target.
    """

    stream_epoch: str = ""
    surface_version: int = Field(default=0, ge=0)
    strategy_instance_id: str
    process: InstanceProcessView
    live_binding: LiveBinding | None = None
    evidence_binding: EvidenceBinding | None = None
    latest_mutation: MutationAttemptView | None = None
    desired_state: DesiredStateView | None = None
    readiness: ReadinessVector | None = None
    latest_decision: dict | None = None
    latest_signal_tone: SignalTone = "neutral"
    decision_columns: list[DecisionColumnDescriptor] = Field(default_factory=list)
    broker: InstanceBrokerView | None = None
    # Pre-filled Start-card values (#416); None when the instance has no run to
    # resolve a ledger from (nothing-deployed).
    start_defaults: InstanceStartDefaults | None = None
    # What the run's content-addressed identity attests to (commit, spec+SHA, QC
    # audit copy+SHA, backtest id, account). None when nothing is deployed. Lets
    # the console explain the hashes ("what this proves") instead of dumping them.
    provenance: InstanceProvenance | None = None
    # ADR 0009 — sizing surface for the Sizing card. ``None`` when nothing is
    # deployed; pre-policy runs surface with ``policy=None`` (the UI shows the
    # honest pre-policy badge).
    sizing: InstanceSizing | None = None
    # Why the most recent run ended, when it has terminated. None while a run is
    # live or when nothing was ever deployed. Lets the console explain a STOPPED
    # instance instead of leaving the operator to read run_status.json by hand.
    last_exit: InstanceLastExit | None = None
    # The monitor/chart symbol, sourced from the action-plan traded stock when
    # present, then ``live_config.symbol`` for legacy signal=trade runs. ``None``
    # when nothing is deployed or when the ledger predates both fields — the UI
    # must treat null as "unknown" rather than substituting a default.
    symbol: str | None = None
    # PRD #593 Slice 1A — the operator-declared instrument plan for the
    # bound (or evidence) run, sourced from ``ledger.live_config.action``.
    # ``None`` when nothing is deployed OR the ledger pre-dates the
    # field — the cockpit must distinguish "declared empty" (an empty
    # ``ActionPlan`` dict) from "ledger pre-dates the field" (``None``).
    # Typed as ``dict`` so the response shape stays open while leg
    # variants are still being added in #595 (stock) and #596 (option).
    action_plan: dict | None = None
    # PRD #593 Slice 1A — the strategy registry's ``instrument_surface``
    # value for the bound run's ``strategy_key``. Informational only in
    # Slices 1–3 (every current strategy registers as ``explicit``).
    # ``None`` when nothing is deployed, the ledger has no
    # ``strategy_key``, or the strategy isn't in the registry. Pinned to
    # the registry's ``Literal["policy", "explicit"]`` so the wire
    # contract refuses an unknown value rather than silently passing it
    # to the cockpit (ADR 0012 §6 — the enum is the source of truth).
    instrument_surface: Literal["policy", "explicit"] | None = None
    # PRD #593 Slice 1E (#598) / ADR 0012 §7 — unhashed redeploy lineage,
    # sourced from the ledger's ``lineage`` block. Typed precisely so
    # the wire contract is the single source of truth (matches the
    # Slice 1A precedent for ``instrument_surface``). Pydantic accepts
    # unknown extras by default so a future daemon-side enrichment
    # passes through without breaking the cockpit.
    lineage: RedeployLineage | None = None
    # PRD #607 / Slice 1 / #608 — operator-facing projection. Always
    # present (never ``None``); per-section blocks are populated by the
    # cumulative Slice 1 cycles.
    operator_surface: OperatorSurface
    # Server-authored lifecycle chart for the bot Overview tab. Angular
    # renders this graph directly; it does not infer lifecycle, gate, or
    # action truth from lower-level fields.
    lifecycle_chart: BotLifecycleChartView
    # Rev 3 daily lifecycle projection: three durable phases, closed display
    # vocabulary, roster flag, and Button Rule action ids.
    daily_lifecycle: BotDailyLifecycleProjection
    fetched_at_ms: int


class ChartSnapshotRun(BaseModel):
    """One run's contribution to a chart snapshot (Slice 5).

    ``started_at_ms`` / ``ended_at_ms`` come from ``run_status.json`` and
    drive the chart's inactive-interval shading; ``is_current`` is true for
    the run that owns the live binding so the chart can scope the active-
    entry line to it. ``color_index`` is a small integer the frontend maps
    to a stable per-run color tag for the trade markers.
    """

    run_id: str
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    is_current: bool = False
    color_index: int = 0
    trades: list[dict] = Field(default_factory=list)
    executions: list[dict] = Field(default_factory=list)


class ChartOverlayNotice(BaseModel):
    """Non-persistent market-data overlay warning for the chart window."""

    code: str
    message: str
    session_date: str | None = None
    source: Literal["polygon"] = "polygon"


class ChartSnapshotResponse(BaseModel):
    """Aggregated chart payload for one (instance, date, resolution).

    Replaces the prior split of ``/bars/snapshot`` + per-run
    ``/trades`` + per-run ``/executions`` calls on the chart card —
    returns the day's bars and every run for that instance in a single
    envelope so the frontend doesn't have to know how many runs exist.
    """

    date: str = Field(..., description="YYYY-MM-DD UTC date the snapshot covers.")
    symbol: str
    resolution: str
    timeframe: str = "1m"
    from_ms: int | None = None
    to_ms: int | None = None
    has_bars: bool
    is_streaming: bool = False
    now_ms: int
    bars: list[IbkrMinuteBar] = Field(default_factory=list)
    runs: list[ChartSnapshotRun] = Field(default_factory=list)
    overlay_notices: list[ChartOverlayNotice] = Field(default_factory=list)


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


class ActivityFillMarker(BaseModel):
    """One broker-confirmed fill marker rendered on the price chart."""

    id: str
    row_seq: int
    order_key: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    price: float
    chart_ts_ms: int
    exec_ts_ms: int
    position_effect: str
    replay_count: int = 1
    evidence: list[ActivityEvidenceRef] = Field(default_factory=list)


class ActivityPositionAnnotation(BaseModel):
    """Position lifecycle label derived from the broker-confirmed fills."""

    id: str
    ts_ms: int
    symbol: str
    label: str
    net_position: float
    uncertain: bool = False
    reason: str | None = None


class ActivityOrderOverlay(BaseModel):
    """Optional chart overlay for a working order with a meaningful price."""

    id: str
    order_key: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    price: float
    status: str
    ts_ms: int


class ActivityOrderRow(BaseModel):
    """Same-day order blotter row for the Activity tab's Orders Today panel."""

    order_key: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: float
    order_type: str
    status: str
    group: Literal["active", "resolved", "engine_pending"]
    chart_ts_ms: int
    submitted_ts_ms: int
    last_update_ts_ms: int
    filled_quantity: float = 0.0
    avg_fill_price: float | None = None
    position_effect: str | None = None
    replay_count: int = 1
    evidence: list[ActivityEvidenceRef] = Field(default_factory=list)


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


class ActivityBrokerCategorySummary(BaseModel):
    """Grouped broker-tail category rendered before row-level drill-down."""

    category_id: str
    label: str
    kind: Literal["order", "heartbeat", "evidence"]
    event_count: int = Field(ge=0)
    last_event_ts_ms: int | None = None
    row_ids: list[str] = Field(default_factory=list)


class ActivityPositionSnapshot(BaseModel):
    """Present-tense broker position snapshot carried by the projection."""

    symbol: str
    quantity: float
    source: Literal["broker_snapshot", "unavailable"] = "broker_snapshot"
    as_of_ms: int | None = None


class ActivityReconciliationWarning(BaseModel):
    """Fail-honest warning when lifecycle derivation cannot be trusted."""

    code: str
    message: str
    row_ids: list[str] = Field(default_factory=list)


LifecycleEventSeverity = Literal["info", "warning", "critical"]
LifecycleEventCategory = Literal[
    "decision",
    "risk_gate",
    "order",
    "fill",
    "position_change",
    "account_balance",
    "freeze",
    "halt",
    "poison",
    "desired_state",
    "lifecycle_transition",
    "account_event",
    "evidence",
]


class LifecycleEvidenceRef(BaseModel):
    """Reference to the durable evidence behind a lifecycle event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    source_label: str | None = None
    source_local_seq: int | None = Field(default=None, ge=0)
    path: str | None = None
    row_id: str | None = None
    summary: str | None = None


class BotLifecycleEvent(BaseModel):
    """Typed read-side lifecycle event projected from existing evidence.

    This is not a new write-ahead log. It is the normalized row shape used by
    node details, timelines, and future persistence. Legacy evidence that lacks
    a trustworthy timestamp keeps ``ts_ms=None`` and ``ts_ms_resolved=False`` so
    the UI can surface the gap instead of pretending chronology is proven.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    event_id: str
    bot_id: str | None = None
    run_id: str | None = None
    account_id: str | None = None
    event_type: str
    category: LifecycleEventCategory
    node_id: str | None = None
    status: LifecycleChartStatus | None = None
    status_label: str | None = None
    severity: LifecycleEventSeverity = "info"
    ts_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    ts_ms_resolved: bool = True
    source: str
    source_rank: int = Field(ge=0)
    source_local_seq: int = Field(ge=0)
    summary: str
    why: str | None = None
    operator_next_step: str | None = None
    evidence_refs: list[LifecycleEvidenceRef] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    rendered_template_id: str | None = None

    @model_validator(mode="after")
    def _timestamp_resolution_contract(self) -> BotLifecycleEvent:
        if self.ts_ms is None and self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved cannot be true when ts_ms is absent")
        if self.ts_ms is not None and not self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved=false is reserved for absent timestamps")
        return self


class AccountEventProjection(BaseModel):
    """Tolerant typed view over ``account_events.jsonl`` rows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    account_id: str
    event_type: str
    seq: int | None = Field(default=None, ge=1)
    file_position: int = Field(ge=1)
    ts_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    ts_ms_resolved: bool
    ts_ms_source: str | None = None
    summary: str
    why: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class LifecycleNodeDetail(BaseModel):
    """Server-authored drill-down for one lifecycle chart node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    status: LifecycleChartStatus
    status_label: str
    summary: str
    why: str | None = None
    operator_next_step: str | None = None
    since_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    related_events: list[BotLifecycleEvent] = Field(default_factory=list)
    evidence_refs: list[LifecycleEvidenceRef] = Field(default_factory=list)


class LiveInstanceActivityProjection(BaseModel):
    """Backend-materialized Activity tab view for one exchange/session date.

    Broker-confirmed chart markers, the Orders Today panel, Broker Activity
    table, and raw evidence drill-downs all read this one contract so no
    activity marker can exist without a matching ledger row. Chart bars are
    resolved by ``/chart-snapshot``.
    """

    schema_version: int = 1
    strategy_instance_id: str
    session_date: str
    timezone: str = "America/New_York"
    symbol: str
    resolution: str
    has_bars: bool
    now_ms: int
    bars: list[dict] = Field(default_factory=list)
    fill_markers: list[ActivityFillMarker] = Field(default_factory=list)
    position_annotations: list[ActivityPositionAnnotation] = Field(default_factory=list)
    order_overlays: list[ActivityOrderOverlay] = Field(default_factory=list)
    orders_today: list[ActivityOrderRow] = Field(default_factory=list)
    broker_activity_summary: list[ActivityBrokerCategorySummary] = Field(default_factory=list)
    broker_activity_rows: list[ActivityBrokerEventRow] = Field(default_factory=list)
    position_snapshot: list[ActivityPositionSnapshot] = Field(default_factory=list)
    reconciliation_warnings: list[ActivityReconciliationWarning] = Field(default_factory=list)
    evidence: list[ActivityEvidenceRef] = Field(default_factory=list)


class ActiveDateEntry(BaseModel):
    """Slice 6 — one date the operator can select on the chart.

    ``has_bars`` distinguishes dates with persisted OHLCV (Slice 4
    onwards) from dates that pre-date persistence. The latter still
    appear in the picker because the instance ran on that date, but the
    chart renders a "bars unavailable" badge alongside whatever trade
    markers the per-run parquets carry.
    """

    date: str = Field(..., description="YYYY-MM-DD UTC date.")
    run_count: int = Field(..., ge=0, description="Number of runs touching the date.")
    has_bars: bool = Field(..., description="True when persisted bars exist for the date.")


class LiveInstanceSummary(BaseModel):
    """One row in the account fleet overview.

    PRD #616 added ``readiness_verdict`` and ``readiness_as_of_ms`` so
    the cockpit can render an honest outer-tab badge
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


class BotCatalogPnl(BaseModel):
    """Backend-authored P&L fields for the bot catalog card.

    ``None`` is the honest value when the current data sources cannot provide
    a component; the frontend renders absence rather than recomputing money.
    """

    realized: float | None = None
    unrealized: float | None = None
    total: float | None = None


class BotCatalogMetrics(BaseModel):
    """Metrics rendered by the bot catalog card.

    Financial and execution counts are authored server-side so Angular does
    not infer operational or numerical facts from low-level cockpit evidence.
    """

    pnl: BotCatalogPnl = Field(default_factory=BotCatalogPnl)
    trade_count: int | None = None
    current_exposure: str
    open_positions: int | None = None
    error_count: int


class BotCatalogRow(BaseModel):
    """One server-composed bot catalog card.

    This is the DataView/listing counterpart to ``LiveInstanceStatus``: it
    carries only display/filter fields the catalog needs, already composed in
    operator language by the backend.
    """

    strategy_instance_id: str
    name: str
    description: str | None = None
    status_label: str
    status_detail: str | None = None
    status_tone: Literal["positive", "warning", "danger", "neutral"] = "neutral"
    only_fresh_run_available: bool = False
    needs_attention: bool
    trading_mode: Literal["paper", "live", "unknown"] = "unknown"
    symbols: list[str] = Field(default_factory=list)
    engine: str | None = None
    engine_asset_class: str | None = None
    created_at_ms: int | None = None
    updated_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_run_label: str
    last_run_result: str
    last_run_detail: str | None = None
    process_state: str
    desired_state: str | None = None
    readiness_verdict: Literal["READY", "BLOCKED", "DEGRADED", "UNKNOWN"] = "UNKNOWN"
    daily_lifecycle: BotDailyLifecycleProjection
    start_request: HostRunnerStartRequest | None = None
    attendance: list[BotAttendanceCell] = Field(default_factory=list)
    metrics: BotCatalogMetrics


class BotCatalogResponse(BaseModel):
    """Fleet-wide bot catalog projection."""

    bots: list[BotCatalogRow] = Field(default_factory=list)
    roll_call: BotRollCallSummary = Field(default_factory=BotRollCallSummary)
    evening_report: BotEveningReport | None = None


class BotDeleteRequest(BaseModel):
    """Operator request to remove a bot from active catalog/control surfaces."""

    mode: Literal["soft"] = "soft"
    deleted_by: str = Field(default="operator", min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


class BotDeleteResponse(BaseModel):
    """Result of a bot soft delete.

    ``deleted_run_ids`` are hidden from live-instance catalog/list/status
    projections. The underlying run artifacts stay on disk for audit.
    """

    strategy_instance_id: str
    mode: Literal["soft"] = "soft"
    deleted_at_ms: int = Field(ge=0)
    deleted_by: str
    reason: str | None = None
    deleted_run_ids: list[str] = Field(default_factory=list)
    marker_path: str
    hidden_from_catalog: bool = True


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
    cockpit renders one DTO without an Angular-side merge.

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
    live binding the durable write still gates the next start.
    """

    actuated: bool
    run_id: str | None = None
    command_seq: int | None = None
    detail: str


class SetInstanceDesiredStateResponse(BaseModel):
    """Single intent knob: durable write first, then live actuation if bound."""

    durable: DesiredStateRecordResponse
    actuation: IntentActuation
    rung_receipt: MutationRungReceipt
    rung_receipt_warnings: list[MutationRungReceipt] = Field(default_factory=list)
    mutation_attempt_id: str
    mutation_dispatch_state: MutationAttemptDispatchState


class ReconcileAckResponse(BaseModel):
    """Acknowledgement envelope for runtime ``POST .../reconcile``.

    Reconciliation PR 2. The data plane enqueues a RECONCILE command on
    the bound run; the engine flips the submit barrier synchronously,
    spawns the async control task, and overwrites the command ack with
    its completion outcome when the orchestrator lands. The cockpit
    polls ``operator_surface.reconciliation`` to observe IN_PROGRESS →
    CLEAN/ADOPTED/FAILED transitions; this envelope just confirms the
    request was queued and the engine recognised it.

    ``request_id`` is opaque — it is generated by the data plane for
    operator correlation; the engine mints its own internal id (visible
    on the command ack file) and the receipt projection is the source
    of truth for state changes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    accepted_at_ms: int = Field(gt=0)
    rung_receipt: MutationRungReceipt
    rung_receipt_warnings: list[MutationRungReceipt] = Field(default_factory=list)
