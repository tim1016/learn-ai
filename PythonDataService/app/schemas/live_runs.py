"""Pydantic v2 schemas for live-runs API.

Models for representing live paper-trading run state, decisions, executions,
trades, and artifacts. All timestamps are int64 milliseconds UTC.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


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
    """

    schema_version: int = 1
    run_id: str
    started_at_ms: int
    last_update_ms: int
    ended_at_ms: int | None = None
    exit_code: int | None = None
    exit_reason: ExitReason | None = None
    host_pid: int


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


HydratePolicy = Literal["require", "optional", "disabled"]


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
    started_at_ms: int | None = None
    ended_at_ms: int | None = None
    exit_code: int | None = None
    command: list[str] = Field(default_factory=list)
    log_path: str | None = None
    message: str | None = None


class HostRunnerHealth(BaseModel):
    """Health envelope returned by the host-side runner daemon."""

    ok: bool
    repo_root: str
    live_runs_root: str
    fetched_at_ms: int
    process: HostRunnerProcessStatus
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


class HostRunnerStartRequest(BaseModel):
    """Request body for starting one existing run from the host daemon."""

    readonly: bool = True
    hydrate_policy: HydratePolicy = "require"
    strategy: str = Field(default="spy_ema_crossover", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    max_orders_per_day: int = Field(default=50_000, ge=0, le=100_000)
    ibkr_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)


class HostRunnerStopRequest(BaseModel):
    """Request body for stopping the active host runner subprocess."""

    force: bool = False


class HostRunnerActionResponse(BaseModel):
    """Response for daemon start/stop actions."""

    accepted: bool
    process: HostRunnerProcessStatus


class HostRunnerDeployRequest(BaseModel):
    """Request body for creating a run via the daemon (ADR 0006).

    The daemon supplies ``repo_root`` / ``run_root`` from its own config — they
    are deliberately NOT client-chosen. ``strategy_spec_path`` and
    ``qc_audit_copy_path`` are resolved against the daemon's repo root and
    confined to it. The QC anchor (``qc_cloud_backtest_id`` +
    ``qc_audit_copy_path``) is required — a live run is never created without it.
    """

    strategy_spec_path: str = Field(min_length=1)
    qc_audit_copy_path: str = Field(min_length=1)
    qc_cloud_backtest_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
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


# --- PRD-A UI-1/UI-3/UI-4 contract additions ---


class DesiredStatePathStatus(StrEnum):
    """How the desired-state sidecar resolved for a run (UI-1)."""

    ok = "ok"
    absent = "absent"
    corrupt = "corrupt"
    unknown_no_ledger_binding = "unknown_no_ledger_binding"


class DesiredStateView(BaseModel):
    """Resolved durable-intent view; ``path_status`` carries resolution.

    ``state`` is null unless ``path_status == ok``. Absence is the
    effective-RUNNING default; an empty ledger binding yields
    ``unknown_no_ledger_binding`` and is never guessed from parquet.
    """

    state: str | None = None
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

    verb: str = Field(
        description="PAUSE | RESUME | STOP | FLATTEN | MARK_POISONED | RECONCILE."
    )


class CommandView(BaseModel):
    """A single pending command in the timeline."""

    seq: int
    verb: str


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
    status: str  # queued | acknowledged | failed
    reason: str | None = None
    issued_by: str | None = None
    queued_at_ms: int | None = None
    acked_at_ms: int | None = None
    outcome: str | None = None
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


class ReadinessGate(BaseModel):
    """One named input to the "can this strategy act on the next bar?" verdict
    (ADR 0005). ``status`` is pass|fail|unknown; ``severity`` is hard|soft."""

    name: str
    status: str  # pass | fail | unknown
    severity: str  # hard | soft
    detail: str


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


class InstanceStartDefaults(BaseModel):
    """Pre-filled Start-card values for the console (#416).

    The five ``run start`` knobs, defaulted so the operator never starts from a
    blank form. ``strategy`` is sourced from the run's ledger ``strategy_key``
    (the algorithm module the ledger is reconciled to) when present — empty
    string means a legacy ledger with no recorded key, so the field is
    operator-supplied. The other four mirror ``HostRunnerStartRequest`` defaults;
    they are not persisted in the ledger.
    """

    strategy: str = ""
    readonly: bool = True
    hydrate_policy: HydratePolicy = "require"
    max_orders_per_day: int = 50_000
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


class LiveInstanceStatus(BaseModel):
    """Instance-addressed status: the operator's control-room subject (ADR 0004).

    The current run is attached as evidence; the ``live_binding`` is the only
    thing commands may target.
    """

    strategy_instance_id: str
    process: InstanceProcessView
    live_binding: LiveBinding | None = None
    evidence_binding: EvidenceBinding | None = None
    desired_state: DesiredStateView | None = None
    readiness: ReadinessVector | None = None
    latest_decision: dict | None = None
    decision_columns: list[DecisionColumnDescriptor] = Field(default_factory=list)
    broker: InstanceBrokerView | None = None
    # Pre-filled Start-card values (#416); None when the instance has no run to
    # resolve a ledger from (nothing-deployed).
    start_defaults: InstanceStartDefaults | None = None
    # What the run's content-addressed identity attests to (commit, spec+SHA, QC
    # audit copy+SHA, backtest id, account). None when nothing is deployed. Lets
    # the console explain the hashes ("what this proves") instead of dumping them.
    provenance: InstanceProvenance | None = None
    # Why the most recent run ended, when it has terminated. None while a run is
    # live or when nothing was ever deployed. Lets the console explain a STOPPED
    # instance instead of leaving the operator to read run_status.json by hand.
    last_exit: InstanceLastExit | None = None
    fetched_at_ms: int


class LiveInstanceSummary(BaseModel):
    """One row in the account fleet overview."""

    strategy_instance_id: str
    process_state: str
    bound_run_id: str | None = None
    latest_run_id: str | None = None
    desired_state: str | None = None


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
