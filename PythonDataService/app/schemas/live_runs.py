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


class HostRunnerStartRequest(BaseModel):
    """Request body for starting one existing run from the host daemon."""

    readonly: bool = True
    hydrate_policy: HydratePolicy = "require"
    strategy: str = Field(default="spy_ema_crossover", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    max_orders_per_day: int = Field(default=4, ge=0, le=100)
    ibkr_host: str = Field(default="127.0.0.1", min_length=1, max_length=255)


class HostRunnerStopRequest(BaseModel):
    """Request body for stopping the active host runner subprocess."""

    force: bool = False


class HostRunnerActionResponse(BaseModel):
    """Response for daemon start/stop actions."""

    accepted: bool
    process: HostRunnerProcessStatus


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
    """Pending + ack timeline for a run's command channel (UI-4)."""

    pending: list[CommandView]
    acks: list[CommandAckView]


LiveRunStatus.model_rebuild()
