"""Instance-addressed operator console API (ADR 0004).

The operator's subject is the **strategy instance**, not the run. These
endpoints resolve, *server-side*, the authoritative live binding from the host
daemon (the process registry) and merge it with disk-derived evidence
(latest run by ledger) and durable desired-state. The client never scans runs
to infer liveness; it receives both bindings with names that make misuse hard
(`live_binding` vs `evidence_binding`).

Run-addressed reads stay in ``live_runs.py`` and are evidence-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, NoReturn, TypedDict

from fastapi import APIRouter, Body, HTTPException, Query, Response, status
from pydantic import ValidationError

from app.broker.ibkr.api_evidence import get_ibkr_api_evidence_recorder
from app.broker.ibkr.config import IbkrSettings, get_settings
from app.broker.runtime_snapshot import BrokerRuntimeSnapshot, snapshot_data_plane_broker
from app.config import settings as app_settings
from app.engine.action_plan.parity import parity_diagnostics
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountFreezeEvidence,
    read_account_events,
    read_account_freeze,
    read_account_owner_generation,
)
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateCorruptError,
    BotLifecycleStateRecord,
    BotLifecycleStateRepo,
    BotRollCallOfferRecord,
    stable_bot_lifecycle_state_path,
)
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.daemon_connectivity_monitor import (
    get_monitor as get_daemon_connectivity_monitor,
)
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRecord,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.engine_runtime import (
    ENGINE_RUNTIME_FILENAME,
    EngineRuntimeSnapshot,
    read_engine_runtime_snapshot,
)
from app.engine.live.fleet import (
    compute_fleet_account_summary,
)
from app.engine.live.halt import read_poisoned_flag
from app.engine.live.intent_events import IntentEvent, IntentEventType
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.live_artifact_io import (
    artifact_exists,
    read_parquet_rows,
    read_parquet_tail,
)
from app.engine.live.live_state_sidecar import (
    LiveStateSidecarCorruptError,
    LiveStateSidecarRepo,
    stable_live_state_path,
)
from app.engine.live.order_identity import mint_intent_id
from app.engine.live.readiness import build_start_readiness
from app.engine.live.readiness_sidecar import read_readiness
from app.engine.live.signal_tone import latest_signal_tone
from app.engine.strategy.spec.descriptors import decision_column_descriptors
from app.engine.strategy.spec.schema import load_spec_from_path
from app.lean_sidecar.trading_calendar import session_state_at_ms
from app.operator.incidents.store import IncidentStore
from app.operator.notices.schema import OperatorNotice
from app.routers.live_runs import (
    _ACTION_TO_STATE,
    COMMAND_POLL_INTERVAL_MS,
    _confine,
    _desired_state_root,
    _now_ms,
    _read_ledger,
    _read_sidecar,
    _resolve_desired_state,
    _validate_path_segment,
    build_command_timeline,
)
from app.schemas.account_recovery import CrashRecoveryOverrideRequest, CrashRecoveryOverrideResponse
from app.schemas.action_plan import ActionPlan, ActionPlanPreviewResponse
from app.schemas.daemon_diagnostics import DaemonDiagnosticReport, DaemonDominantCondition
from app.schemas.live_runs import (
    ActiveDateEntry,
    ActivityBrokerCategorySummary,
    ActivityBrokerEventRow,
    ActivityEvidenceRef,
    ActivityFillMarker,
    ActivityOrderRow,
    ActivityPositionAnnotation,
    ActivityPositionSnapshot,
    ActivityReconciliationWarning,
    AuditCopySizingLookup,
    BotCatalogResponse,
    BotDeleteRequest,
    BotDeleteResponse,
    BotLifecycleMutationResponse,
    BotLifecycleRosterRequest,
    BotRetireReplaceRequest,
    BotRollCallResponse,
    BrokerObservationConsistency,
    ChartOverlayNotice,
    ChartSnapshotResponse,
    ChartSnapshotRun,
    CommandsTimeline,
    CommandView,
    DesiredStateAction,
    DesiredStateRecordResponse,
    EmergencyFlattenRequest,
    EnqueueCommandRequest,
    FleetAccountSummary,
    FleetContamination,
    HostRunnerActionResponse,
    HostRunnerDeployRequest,
    HostRunnerDeployResponse,
    HostRunnerHealth,
    HostRunnerStartRequest,
    HostRunnerStopRequest,
    InstanceLastExit,
    InstanceProcessView,
    InstanceProvenance,
    InstanceSizing,
    InstanceStartDefaults,
    IntentActuation,
    LiveBinding,
    LiveInstanceActivityProjection,
    LiveInstanceDeployRequest,
    LiveInstanceStatus,
    LiveInstanceSummary,
    MutationOutcomeUnknownResponse,
    MutationRungReceipt,
    OperatorSurfaceAccountOwner,
    QcAuditCopyListing,
    ReadinessVector,
    ReconcileAckResponse,
    ReconcileMutationResponse,
    SetDesiredStateRequest,
    SetInstanceDesiredStateResponse,
    SignalTone,
    SizingAuditRow,
)
from app.schemas.operator_blocker import DeployPreflightResponse
from app.services import deploy_preflight as deploy_preflight_service
from app.services import fleet_contamination as fleet_contamination_service
from app.services.account_crash_recovery import (
    CrashRecoveryNotRequiredError,
    crash_recovery_block_detail,
    crash_recovery_blocking_binding,
    crash_recovery_gate_for_instance,
    record_crash_recovery_override_evidence,
)
from app.services.account_truth_snapshot import get_account_truth_snapshot_provider
from app.services.activity_evidence_matching import (
    activity_evidence_ref_from_event,
    matching_evidence_refs,
)
from app.services.activity_lifecycle_consistency import (
    NY_TZ as _NY_TZ,
)
from app.services.activity_lifecycle_consistency import (
    activity_lifecycle_consistency_warnings as compare_activity_lifecycle_refs,
)
from app.services.activity_lifecycle_consistency import (
    activity_order_refs_for_session,
    ny_session_bounds_ms,
    runs_active_in_window,
)
from app.services.activity_projection_contract import (
    activity_cluster_label,
    activity_evidence_narrative,
    fold_activity_event_rows,
)
from app.services.activity_repair_projection import load_activity_repair_projection
from app.services.bot_catalog_projection import compose_bot_catalog_row, trading_mode_from_configured_mode
from app.services.bot_daily_lifecycle import project_bot_daily_lifecycle
from app.services.bot_deletion import (
    BOT_DELETION_FILENAME,
    BotDeletionCorruptError,
    BotDeletionRecord,
    bot_has_soft_deletion,
    bot_run_is_soft_deleted,
    read_bot_deletion,
    soft_delete_bot_runs,
    stable_bot_deletion_path,
)
from app.services.bot_lifecycle_chart import compose_bot_lifecycle_chart
from app.services.bot_lifecycle_conditions import lifecycle_conditions_for_instance
from app.services.bot_lifecycle_projection import (
    account_event_to_lifecycle_event,
    project_account_events,
    project_intent_events,
    sort_lifecycle_events,
)
from app.services.bot_roll_call import (
    active_roll_call_offer,
    attendance_for_instance,
    bot_roll_call_offer_repo,
    ensure_roll_call_offer,
    evening_report_from_rows,
    roll_call_offer_schema,
    roll_call_summary_from_rows,
    status_is_roll_call_eligible,
)
from app.services.broker_activity_publisher_registry import get_publisher_registry
from app.services.broker_activity_wal import BrokerActivityWal, instance_broker_activity_wal_path
from app.services.daemon_diagnostics import (
    get_daemon_diagnostics_service,
    project_daemon_diagnostic_report,
    redact_host_runner_health,
)
from app.services.daily_session_schedule import start_boundary_verdict
from app.services.deploy_admission import (
    SymbolResolution,
    evaluate_deploy_start_admission,
    resolve_symbol_from_ledger,
)
from app.services.fleet_contamination import (
    collect_fleet_position_explanations,
)
from app.services.fleet_contamination import (
    fetch_net_positions as _fetch_net_positions,
)
from app.services.fleet_contamination import (
    instance_broker as _instance_broker,
)
from app.services.fleet_contamination import (
    read_instance_live_state as _read_instance_live_state,
)
from app.services.fleet_contamination import (
    scan_runs_by_instance as _scan_runs_by_instance,
)
from app.services.instance_context import InstanceContext, load_instance_context
from app.services.live_chart_window import (
    ChartWindowError,
    coerce_chart_timeframe,
    resolve_chart_window,
)
from app.services.live_instance_surface_assembler import (
    LiveInstanceSurfaceAssembler,
    LiveInstanceSurfaceDependencies,
)
from app.services.mutation_attempt import (
    TERMINAL_STATES,
    MutationAttempt,
    MutationAttemptRepo,
    ReconciliationEvidence,
    reconcile_mutation_effect,
    transition_attempt,
)
from app.services.mutation_rung_receipts import mutation_rung_receipts
from app.services.operator_capability import evaluate_action
from app.services.operator_surface import (
    compute_operator_surface,
)
from app.services.resume_guard_state import (
    ResumeGuardState,
    empty_guard_state,
    resolve_guard_state_from_paths,
)
from app.services.runtime_freshness import (
    RuntimeFreshness,
    evaluate_runtime_freshness,
    unavailable_runtime_freshness,
)
from app.services.strategy_validation_manifest import (
    StrategyValidationManifestError,
)
from app.services.surface_hub import (
    SnapshotUnavailableError,
    SurfaceHub,
    SurfaceHubRegistry,
)

if TYPE_CHECKING:
    from app.services.broker_activity_publisher import BrokerActivityPublisher

# The instance command channel is reserved for one-shot operations; PAUSE/
# RESUME/STOP are the durable intent knob (POST .../desired-state), not commands.
_ONE_SHOT_VERBS = frozenset({CommandVerb.FLATTEN, CommandVerb.RECONCILE, CommandVerb.MARK_POISONED})

# Durable intent action -> live-actuation command verb. PAUSE/RESUME/STOP are the
# only verbs the durable knob actuates; the engine persists them as reconciling
# writers, so live actuation leaves desired_state.json at the same semantic state.
_ACTION_TO_VERB = {
    DesiredStateAction.pause: CommandVerb.PAUSE,
    DesiredStateAction.resume: CommandVerb.RESUME,
    DesiredStateAction.stop: CommandVerb.STOP,
}

_STATUS_DAEMON_DIAGNOSTICS_TIMEOUT_S = 1.5

logger = logging.getLogger(__name__)

router = APIRouter(tags=["live-instances"])
_SURFACE_HUBS = SurfaceHubRegistry[LiveInstanceStatus]()

# strategy_instance_id flows into a daemon URL and a filesystem path; confine it
# to a single safe segment at the boundary.
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Process states that mean a run is being actively written right now.
_LIVE_STATES = frozenset({"running", "stopping"})
_LIFECYCLE_ACTIVITY_ORDER_EVENT_TYPES = frozenset(
    {
        IntentEventType.PENDING_INTENT,
        IntentEventType.SUBMITTED,
        IntentEventType.ACK_FAILED_UNCERTAIN,
        IntentEventType.SUBMITTED_RECOVERED,
        IntentEventType.INTENT_NOT_ACCEPTED,
        IntentEventType.SUBMIT_UNCERTAIN_HALTED,
        IntentEventType.ADOPTED_BROKER_ORDER,
    }
)


def _validate_instance_id(strategy_instance_id: str) -> str:
    """Validate the operator-supplied instance id and return a sanitized literal.

    Mirrors ``_validate_run_id``: run the value through ``_validate_path_segment``
    then assert a strict single-segment regex via ``fullmatch`` as the sole guard
    on the *returned* literal. That regex guard on the value that reaches the
    daemon URL and the desired-state path is the form the scanner recognizes as
    breaking the CodeQL py/path-injection taint chain.
    """
    try:
        safe = _validate_path_segment(strategy_instance_id, field="strategy_instance_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc
    if _INSTANCE_ID_RE.fullmatch(safe) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid strategy_instance_id: {strategy_instance_id!r}",
        )
    return safe


def _run_is_soft_deleted(artifacts_root: Path, sid: str, run_id: str) -> bool:
    try:
        return bot_run_is_soft_deleted(artifacts_root, sid, run_id)
    except (ValueError, BotDeletionCorruptError) as exc:
        logger.warning(
            "failed to read bot deletion marker while scanning runs",
            extra={"strategy_instance_id": sid, "run_id": run_id, "exception": repr(exc)},
        )
        return False


def _sid_has_soft_deletion(artifacts_root: Path, sid: str) -> bool:
    try:
        return bot_has_soft_deletion(artifacts_root, sid)
    except (ValueError, BotDeletionCorruptError) as exc:
        logger.warning(
            "failed to read bot deletion marker",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        return False


def _sid_has_soft_deletion_from_directory(artifacts_root: Path, sid: str) -> bool:
    """Prove a deletion marker without using request input in a path.

    Directory entries originate from the trusted artifacts root; ``sid`` is
    used only for equality. This keeps cached-status deletion proof off the
    user-input-to-filesystem dataflow while preserving the durable marker as
    authority across process restarts.
    """

    live_state_root = artifacts_root / "live_state"
    try:
        with os.scandir(live_state_root) as entries:
            matched = next((entry for entry in entries if entry.name == sid), None)
        if matched is None or not matched.is_dir(follow_symlinks=False):
            return False
        with os.scandir(matched.path) as children:
            return any(
                child.name == BOT_DELETION_FILENAME and child.is_file(follow_symlinks=False) for child in children
            )
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning(
            "failed to scan bot deletion markers",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        return False


def _resolve_bot_lifecycle_state(root: Path, sid: str) -> BotLifecycleStateRecord | None:
    try:
        path = stable_bot_lifecycle_state_path(root.parent, sid)
    except ValueError:
        return None
    try:
        return BotLifecycleStateRepo(path).read()
    except BotLifecycleStateCorruptError as exc:
        logger.warning(
            "failed to read bot lifecycle state",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        return None


def _bot_lifecycle_state_repo(root: Path, sid: str) -> BotLifecycleStateRepo:
    try:
        path = stable_bot_lifecycle_state_path(root.parent, sid)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc
    return BotLifecycleStateRepo(path)


def _active_roll_call_offer(root: Path, sid: str, *, now_ms: int) -> BotRollCallOfferRecord | None:
    try:
        return active_roll_call_offer(root, sid, now_ms=now_ms)
    except BotLifecycleStateCorruptError as exc:
        logger.warning(
            "failed to read bot roll-call offers",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        return None


async def _daily_lifecycle_mutation_response(
    sid: str,
    root: Path,
    settings: IbkrSettings,
) -> BotLifecycleMutationResponse:
    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    status_view = await _resolve_instance_status_from_process(
        sid,
        root,
        settings,
        daemon,
        runs_by_instance=_visible_runs_by_instance(root),
    )
    return BotLifecycleMutationResponse(
        strategy_instance_id=sid,
        lifecycle=status_view.daily_lifecycle,
    )


def _visible_runs_by_instance(
    root: Path, runs_by_instance: dict[str, list[dict]] | None = None
) -> dict[str, list[dict]]:
    source = runs_by_instance if runs_by_instance is not None else _scan_runs_by_instance(root)
    visible: dict[str, list[dict]] = {}
    for sid, runs in source.items():
        kept = [run for run in runs if not _run_is_soft_deleted(root.parent, sid, str(run.get("run_id") or ""))]
        if kept:
            visible[sid] = kept
    return visible


def _interpret_daemon_process(daemon: dict | None, root: Path) -> tuple[InstanceProcessView, LiveBinding | None]:
    """Turn the daemon's process snapshot into a process view + live binding.

    ``None`` (daemon unreachable) is rendered as ``unreachable`` with no live
    binding — never guessed from disk.
    """
    if daemon is None:
        return InstanceProcessView(state="unreachable"), None
    state = str(daemon.get("state") or "idle")
    run_id = daemon.get("run_id")
    pid = daemon.get("pid")
    started = daemon.get("started_at_ms")
    raw_client_id = daemon.get("ibkr_client_id")
    ibkr_client_id = raw_client_id if isinstance(raw_client_id, int) else None
    if state in _LIVE_STATES and run_id:
        run_dir = root / run_id
        binding = LiveBinding(run_id=run_id, run_dir=str(run_dir) if run_dir.is_dir() else None)
        view = InstanceProcessView(
            state=state,
            pid=pid,
            ibkr_client_id=ibkr_client_id,
            bound_run_id=run_id,
            started_at_ms=started,
        )
        return view, binding
    # exited / idle: a run id may be present (the run that just exited) but it is
    # not a live binding.
    return (
        InstanceProcessView(
            state=state,
            pid=pid,
            ibkr_client_id=ibkr_client_id,
            bound_run_id=run_id,
            started_at_ms=started,
        ),
        None,
    )


def _visible_live_run_dir(root: Path, live_binding: LiveBinding) -> Path | None:
    """Return the locally visible bound run dir, confined under ``root``.

    The daemon is a separate process and reports the live binding. Before this
    API writes a command file, re-check that the bound ``run_id`` resolves under
    this service's live-runs root and that the directory exists locally. A root
    mismatch stays durable-only; the engine would not see a command written to a
    freshly-created phantom directory.
    """
    try:
        safe_run_id = _validate_path_segment(live_binding.run_id, field="run_id")
        run_dir = _confine(root, safe_run_id)
    except ValueError:
        return None
    if live_binding.run_dir is not None:
        try:
            reported = Path(live_binding.run_dir).resolve()
            if reported != run_dir:
                return None
        except OSError:
            return None
    return run_dir if run_dir.is_dir() else None


def _resolve_readiness(
    root: Path,
    live_binding: LiveBinding | None,
    runs: list[dict],
    desired_state: str | None,
) -> ReadinessVector:
    """Transport the engine-authored live-readiness vector when a live binding is
    locally visible; otherwise derive a labelled start-readiness from durable
    artifacts (ADR 0005). The engine authors live readiness — the backend never
    recomputes it; it only derives start-readiness for a dead instance.
    """
    if live_binding is not None:
        run_dir = _visible_live_run_dir(root, live_binding)
        if run_dir is not None:
            raw = read_readiness(run_dir)
            if raw is not None:
                try:
                    return ReadinessVector.model_validate(raw)
                except ValidationError:
                    pass  # malformed sidecar -> fall through to start-readiness
    latest_run_dir = Path(runs[0]["run_dir"]) if runs else None
    poisoned = latest_run_dir is not None and (latest_run_dir / "poisoned.flag").exists()
    halted = latest_run_dir is not None and (latest_run_dir / "halt.flag").exists()
    return ReadinessVector.model_validate(
        build_start_readiness(
            as_of_ms=_now_ms(),
            desired_state=desired_state,
            poisoned=poisoned,
            halted=halted,
            reconcile_passed=None,
        )
    )


def _resolve_account_freeze(
    artifacts_root: Path,
    runs: list[dict],
) -> AccountFreezeEvidence | None:
    for run in runs:
        try:
            ledger = _read_ledger(Path(run["run_dir"]))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "failed to read ledger while resolving account freeze",
                extra={"run_dir": str(run.get("run_dir")), "exception": repr(exc)},
            )
            continue
        account_id = ledger.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            continue
        account_freeze = read_account_freeze(artifacts_root, account_id)
        if account_freeze is not None:
            return account_freeze
    return None


def _run_dir_account_id(run_dir: Path) -> str | None:
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, json.JSONDecodeError):
        return None
    value = ledger.get("account_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _raise_if_crash_recovery_blocks_start(
    artifacts_root: Path,
    *,
    account_id: str,
    strategy_instance_id: str,
) -> None:
    binding = crash_recovery_blocking_binding(
        artifacts_root,
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
    )
    if binding is None:
        return
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail=crash_recovery_block_detail(strategy_instance_id, binding),
    )


def _resolve_account_owner_surface(
    artifacts_root: Path,
    account_id: str | None,
) -> OperatorSurfaceAccountOwner | None:
    if account_id is None:
        return None
    try:
        generation = read_account_owner_generation(artifacts_root, account_id)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        logger.warning(
            "failed to read account owner generation while resolving operator surface",
            extra={"account_id": account_id, "exception": repr(exc)},
        )
        return OperatorSurfaceAccountOwner(account_id=account_id, phase="unknown")
    if generation is None:
        return OperatorSurfaceAccountOwner(account_id=account_id, phase="unknown")
    return OperatorSurfaceAccountOwner(
        account_id=generation.account_id,
        generation=generation.generation,
        phase=generation.phase,
        recorded_at_ms=generation.recorded_at_ms,
        source=generation.source,
    )


def _project_instance_account_lifecycle_events(
    artifacts_root: Path,
    *,
    account_id: str | None,
    sid: str,
    run_id: str | None,
    bot_order_namespace: str | None,
) -> list:
    if account_id is None:
        return []
    try:
        rows = read_account_events(artifacts_root, account_id)
    except (OSError, json.JSONDecodeError, AccountArtifactError) as exc:
        logger.warning(
            "failed to read account events while resolving lifecycle chart",
            extra={"account_id": account_id, "exception": repr(exc)},
        )
        return []
    projected_account_events = project_account_events(
        [
            row
            for row in rows
            if isinstance(row, Mapping)
            and _account_event_matches_instance(
                row,
                sid=sid,
                run_id=run_id,
                bot_order_namespace=bot_order_namespace,
            )
        ],
        account_id=account_id,
    )
    return [
        account_event_to_lifecycle_event(event).model_copy(update={"bot_id": sid}) for event in projected_account_events
    ]


def _account_event_matches_instance(
    row: Mapping,
    *,
    sid: str,
    run_id: str | None,
    bot_order_namespace: str | None,
) -> bool:
    diagnostics = row.get("diagnostics")
    diagnostic_values = diagnostics if isinstance(diagnostics, Mapping) else {}

    row_sid = _nonempty_str(diagnostic_values.get("strategy_instance_id")) or _nonempty_str(
        diagnostic_values.get("bot_id")
    )
    row_sid = row_sid or _nonempty_str(row.get("strategy_instance_id")) or _nonempty_str(row.get("bot_id"))
    row_run_id = _nonempty_str(diagnostic_values.get("run_id")) or _nonempty_str(row.get("run_id"))
    row_namespace = _nonempty_str(row.get("bot_order_namespace"))
    order_ref = _nonempty_str(diagnostic_values.get("order_ref")) or _nonempty_str(row.get("order_ref"))
    if row_namespace is None and order_ref is not None and ":" in order_ref:
        row_namespace = order_ref.rsplit(":", 1)[0]

    sid_matches = row_sid == sid
    run_matches = run_id is not None and row_run_id == run_id
    namespace_matches = bot_order_namespace is not None and row_namespace == bot_order_namespace
    if row_sid is not None and not sid_matches:
        return False
    if run_id is not None and row_run_id is not None and not run_matches:
        return False
    if bot_order_namespace is not None and row_namespace is not None and not namespace_matches:
        return False
    return sid_matches or run_matches or namespace_matches


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strategy_state(
    root: Path,
    live_binding: LiveBinding | None,
    runs: list[dict],
) -> tuple[dict | None, SignalTone, list[dict]]:
    """Latest decision row + spec-derived column descriptors for the instance.

    Reads from the live run when visible, else the latest evidence run. The
    descriptors come from the run's strategy spec (the single source of column
    semantics), so the console renders any strategy's indicators generically.
    """
    run_dir: Path | None = None
    if live_binding is not None:
        run_dir = _visible_live_run_dir(root, live_binding)
    if run_dir is None and runs:
        run_dir = Path(runs[0]["run_dir"])
    if run_dir is None:
        return None, "neutral", []

    decisions_path = run_dir / "decisions.parquet"
    rows = read_parquet_tail(decisions_path, 1, on_error="warn_empty") if artifact_exists(decisions_path) else []
    latest_decision = rows[0] if rows else None

    descriptors: list[dict] = []
    try:
        ledger = _read_ledger(run_dir)
        spec = load_spec_from_path(ledger["strategy_spec_path"])
        descriptors = decision_column_descriptors(spec)
    except (OSError, ValueError, KeyError):
        descriptors = []
    return latest_decision, latest_signal_tone(latest_decision), descriptors


def _resolve_readonly_default(settings: object) -> bool:
    """The Start-card's default for the suppress-orders flag.

    Defaults to ``False`` (place orders) **only** when both hold:
      * IBKR is in paper mode (``mode == "paper"``) — orders are paper, so
        trading-by-default is safe and expected; and
      * ``IBKR_READONLY`` is not set (the engine's ``place_paper_order`` refuses
        orders when ``settings.readonly`` is true, so promising order placement
        while readonly is on would be a lie).

    Fails **closed** otherwise — a missing/unknown ``mode`` (config drift, partial
    rollout) or ``IBKR_READONLY=true`` yields ``True`` (shadow / no orders), so a
    real-money or locked-down run never auto-trades from a server default.
    """
    mode = getattr(settings, "mode", None)
    operator_readonly = bool(getattr(settings, "readonly", True))
    return operator_readonly or mode != "paper"


def _resolve_evidence_run_dir(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> Path | None:
    """The run dir the status view describes: the visible live run, else the
    latest evidence run, else None (nothing deployed). Shared by start-defaults
    and provenance so they always read the same ledger."""
    if live_binding is not None:
        run_dir = _visible_live_run_dir(root, live_binding)
        if run_dir is not None:
            return run_dir
    if runs:
        return Path(runs[0]["run_dir"])
    return None


def _mutation_attempt_root(live_runs_root: Path) -> Path:
    """Artifact root for durable ``mutation_attempt`` records.

    Sibling to ``live_runs/`` and ``live_state/`` under the artifacts
    parent — same layout as the rest of 619's per-instance evidence.
    """
    return live_runs_root.parent / "mutation_attempts"


def _resolve_reconciliation_inputs(root: Path, live_binding: LiveBinding | None):
    """Read the cold-start reconciliation receipt + current freshness inputs
    for the operator-surface projection (ADR-0008 §5 / PR 1).

    Returns ``(receipt, current_wal_seq, current_run_id, current_namespace,
    wal_events)``.
    Every element is ``None`` when the input is unresolvable — e.g. no live
    binding, the run dir is missing, the WAL is empty / corrupt, or the
    receipt is absent. The projection turns absences into ``NOT_AVAILABLE``
    rather than raising.
    """
    from app.services.resume_guard_state import read_full_reconciliation_receipt

    run_dir = _resolve_live_run_dir(root, live_binding)
    if run_dir is None or not run_dir.exists():
        return None, None, None, None, []
    receipt = read_full_reconciliation_receipt(run_dir)
    current_run_id = live_binding.run_id
    current_namespace: str | None = None
    current_wal_seq: int | None = None
    events: list[IntentEvent] = []
    wal_path = run_dir / "intent_events.jsonl"
    if wal_path.exists():
        try:
            events = IntentWal(wal_path).read_tail()
            if events:
                current_wal_seq = events[-1].seq
                current_namespace = events[-1].bot_order_namespace
        except IntentWalCorruptError:
            # A corrupt WAL is surfaced elsewhere; for the reconciliation
            # projection we treat it as "no current seq" so a stale-flag
            # comparison falls through to the other rules.
            current_wal_seq = None
            events = []
    return receipt, current_wal_seq, current_run_id, current_namespace, events


def _session_started_at_ms(process: InstanceProcessView, live_binding: LiveBinding | None) -> int | None:
    if live_binding is None:
        return None
    started_at_ms = process.started_at_ms
    if isinstance(started_at_ms, bool):
        return None
    if isinstance(started_at_ms, int) and started_at_ms >= 0:
        return started_at_ms
    return None


def _resolve_live_run_dir(root: Path, live_binding: LiveBinding | None) -> Path | None:
    if live_binding is None or live_binding.run_dir is None:
        return None
    run_dir = Path(live_binding.run_dir)
    return run_dir if run_dir.is_absolute() else root / run_dir


def _resolve_durable_control_write_failure(run_dir: Path | None) -> str | None:
    """Keep a typed failure until a newer durable-control command succeeds."""

    if run_dir is None:
        return None
    timeline = build_command_timeline(_confine(run_dir, "commands"))
    for entry in timeline.entries:
        if not entry.durable_control:
            continue
        if entry.failure_kind == "durable_control_write_failed":
            return entry.outcome_detail or "The bot could not persist its control state."
        if entry.status == "acknowledged":
            return None
    return None


def _resolve_durable_control_write_failure_for_status(
    root: Path,
    live_binding: LiveBinding | None,
    runs: list[dict],
) -> str | None:
    return _resolve_durable_control_write_failure(_resolve_evidence_run_dir(root, live_binding, runs))


def _resolve_incident_headline(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> OperatorNotice | None:
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None or not run_dir.is_dir():
        return None
    incidents = [
        incident
        for incident in IncidentStore(run_dir).list_unresolved()
        if incident.category in {"watchdog", "order", "submit", "safety-halt"}
    ]
    if not incidents:
        return None
    latest = max(incidents, key=lambda incident: incident.started_at_ms)
    return latest.notice


def _resolve_latest_mutation(live_runs_root: Path, strategy_instance_id: str) -> MutationAttempt | None:
    """Read the most recent ``MutationAttempt`` for the instance.

    Returns ``None`` when no attempts have been persisted (typical for
    a freshly-deployed instance) or when the storage root is absent.
    Malformed and forward-incompatible artifacts are also surfaced as
    ``None`` per ``MutationAttemptRepo.latest_for`` semantics — the
    action-conflict matrix treats absence and corruption identically:
    no prior unresolved mutation to consider.
    """
    return MutationAttemptRepo(_mutation_attempt_root(live_runs_root)).latest_for(strategy_instance_id)


def _resolve_runtime_freshness(
    root: Path,
    live_binding: LiveBinding | None,
    *,
    now_ms: int,
) -> RuntimeFreshness | None:
    """Resolve child-authored runtime evidence for the current live binding.

    Runtime freshness is meaningful only for a bound child. Missing,
    malformed, or forward-incompatible artifacts fail closed and are
    surfaced explicitly rather than falling back to process-registry
    liveness.
    """
    _, freshness = _resolve_engine_runtime_snapshot_and_freshness(
        root,
        live_binding,
        now_ms=now_ms,
    )
    return freshness


def _resolve_engine_runtime_snapshot_and_freshness(
    root: Path,
    live_binding: LiveBinding | None,
    *,
    now_ms: int,
) -> tuple[EngineRuntimeSnapshot | None, RuntimeFreshness | None]:
    """Read child-authored runtime evidence once and evaluate freshness."""
    if live_binding is None:
        return None, None
    run_dir = _visible_live_run_dir(root, live_binding)
    if run_dir is None:
        return None, unavailable_runtime_freshness("ENGINE_RUNTIME_MISSING")
    path = run_dir / ENGINE_RUNTIME_FILENAME
    if not path.is_file():
        return None, unavailable_runtime_freshness("ENGINE_RUNTIME_MISSING")
    snapshot = read_engine_runtime_snapshot(path)
    if snapshot is None:
        return None, unavailable_runtime_freshness("ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE")
    return snapshot, evaluate_runtime_freshness(
        snapshot,
        now_ms=now_ms,
        session_state=session_state_at_ms(now_ms),
    )


def _safety_verdict_final_from_engine_runtime(
    snapshot: EngineRuntimeSnapshot | None,
) -> Literal["paper-only", "unsafe", "unknown"] | None:
    """Resolve ADR-0011 safety from fresh child-authored broker identity."""
    if snapshot is None:
        return None
    if snapshot.broker.identity == "PAPER_VERIFIED":
        return "paper-only"
    if snapshot.broker.identity == "LIVE_DETECTED":
        return "unsafe"
    return "unknown"


def _broker_connection_state_from_engine_runtime(
    snapshot: EngineRuntimeSnapshot | None,
) -> (
    Literal[
        "connected",
        "soft_lost",
        "subscriptions_stale",
        "degraded_data_farm",
        "reconnecting",
        "recovering",
        "hard_down",
        "disconnected",
        "disabled",
        "unknown",
    ]
    | None
):
    """Resolve broker connection from fresh child-authored runtime evidence.

    Readiness is still the engine's readiness-gate contract, but it can be
    absent while the child process is running and publishing a fresh
    ``engine_runtime.json`` broker probe. In that case, prefer the fresh
    runtime broker block so the operator surface does not report an unknown
    broker while the child has just proven the session state.
    """
    if snapshot is None:
        return None
    state = snapshot.broker.connection_state
    if state in {
        "connected",
        "soft_lost",
        "subscriptions_stale",
        "degraded_data_farm",
        "reconnecting",
        "recovering",
        "hard_down",
        "disconnected",
        "disabled",
    }:
        return state
    return "unknown"


def _resolve_broker_observation_consistency(
    live_binding: LiveBinding | None,
    *,
    runtime_snapshot: EngineRuntimeSnapshot | None,
    configured_mode: Literal["paper", "live"] | None,
    now_ms: int,
) -> BrokerObservationConsistency | None:
    """Compute the divergence verdict for the current live binding.

    Returns ``None`` when there is nothing to compare (no live
    binding) so the cockpit hides the card.  Otherwise returns the
    backend-authored verdict — including ``UNKNOWN`` when the child
    runtime artifact is missing.  The data-plane snapshot is read at
    the same instant as the freshness evaluator to keep both views
    consistent within a single status response.
    """
    from app.broker.runtime_snapshot import snapshot_data_plane_broker
    from app.services.broker_observation_consistency import (
        evaluate_broker_observation_consistency,
    )

    if live_binding is None:
        return None
    child_block = runtime_snapshot.broker if runtime_snapshot is not None else None
    return evaluate_broker_observation_consistency(
        child=child_block,
        data_plane=snapshot_data_plane_broker(),
        child_configured_mode=configured_mode,
        now_ms=now_ms,
    )


def _resolve_start_run_id(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> str | None:
    """Run_id the per-instance Start affordance will POST against.

    The cockpit's Start button targets ``POST /runs/{run_id}/start``. The
    canonical run is the bound run if present, else the latest evidence
    run — the same resolution ``_start_defaults`` uses to seed the form
    body. Returns ``None`` when the instance has no run on disk yet
    (nothing-deployed); the projection then disables Start with
    ``START_SETTINGS_INCOMPLETE``.
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    return run_dir.name if run_dir is not None else None


def _start_defaults(
    root: Path, live_binding: LiveBinding | None, runs: list[dict], *, readonly_default: bool
) -> InstanceStartDefaults | None:
    """Pre-filled Start-card values for the console (#416).

    Resolves the same run the rest of the status view does (the visible live
    run, else the latest evidence run) and seeds ``strategy`` from that ledger's
    ``strategy_key`` — the algorithm module the ledger is reconciled to. ``None``
    when the instance has no run to resolve a ledger from (nothing-deployed); a
    ledger without a ``strategy_key`` (legacy) yields an empty ``strategy`` for
    the operator to supply.

    ``readonly_default`` is resolved by :func:`_resolve_readonly_default` (paper
    mode + ``IBKR_READONLY`` unset → place orders; everything else → shadow).
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError):
        return InstanceStartDefaults(readonly=readonly_default)
    return InstanceStartDefaults(
        strategy=str(ledger.get("strategy_key", "")),
        readonly=readonly_default,
        # Deploy identity for a one-click re-deploy (fresh run_id) of a
        # poisoned/halted instance. Empty for legacy ledgers missing the field.
        strategy_spec_path=str(ledger.get("strategy_spec_path", "")),
        qc_audit_copy_path=str(ledger.get("qc_audit_copy_path", "")),
        qc_cloud_backtest_id=str(ledger.get("qc_cloud_backtest_id", "")),
        account_id=str(ledger.get("account_id", "")),
    )


def _resolve_symbol_resolution(
    root: Path,
    live_binding: LiveBinding | None,
    runs: list[dict],
) -> SymbolResolution | None:
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError):
        return None
    return resolve_symbol_from_ledger(ledger, _container_resolve_repo_path)


def _resolve_symbol(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> str | None:
    """Monitor/chart symbol for the instance.

    Resolution order:
      1. ``ledger.live_config.action`` single stock target, when present
      2. ``ledger.live_config.symbol`` (signal stream; legacy signal=trade runs)
      3. ``strategy_spec.symbols[0]`` (the spec the ledger is reconciled to —
         the canonical signal stream fallback)

    Slice 2: the operator console reads this on the chart card instead of
    hardcoding 'SPY'. Returns ``None`` only when nothing is deployed, the
    ledger is unreadable, OR neither the live_config nor the spec carry a
    symbol; the UI treats null as "unknown" rather than substituting a
    default.

    The spec fallback (added post-Slice-2 — see deployment validation
    smoke run 2026-06-12) is what closes the gap where a clean-tree deploy
    leaves ``live_config: {}`` because the operator didn't pass a symbol
    override.

    TODO(PR #483 review): when an instance is redeployed with a different
    symbol (e.g. ``QQQ`` -> ``SPY``), a historical ``/chart-snapshot`` for
    a pre-redeploy date queries persistence with the new symbol and either
    misses bars or returns the wrong partition. Acceptable for the current
    one-strategy-per-instance fleet; revisit by accepting a ``target_date``
    here and resolving the symbol from the run(s) that overlap that date,
    returning ``None`` on a mixed-symbol day.
    """
    resolution = _resolve_symbol_resolution(root, live_binding, runs)
    return resolution.value if resolution is not None else None


def _container_resolve_repo_path(path: str) -> list[Path]:
    """Yield candidate container paths for a host-recorded repo path.

    The host daemon writes absolute host paths into the ledger (e.g.
    ``/Users/.../learn-ai/PythonDataService/app/engine/strategy/spec/...``)
    because *its* process sees the repo at the host root. The data plane
    runs inside a container where the same file lives under ``/app/app/...``
    (compose volume ``./PythonDataService/app:/app/app``).

    Try the literal first, then translate by anchoring on common repo
    sub-roots — the same path eventually resolves under one of them. The
    caller stops at the first existing file.
    """
    candidates = [Path(path)]
    for marker, container_root in (
        ("PythonDataService/app/", "/app/app/"),
        ("PythonDataService/", "/app/"),
        ("references/", "/app/references/"),
    ):
        idx = path.find(marker)
        if idx >= 0:
            translated = container_root + path[idx + len(marker) :]
            candidates.append(Path(translated))
    return [c for c in candidates if c.is_file()] or candidates


def _sizing_audit_rows(strategy_instance_id: str) -> list[dict]:
    """Read the per-trade sizing audit log for the instance's Sizing card.

    VCR-0003 PR B — prefer the durable WAL+skip-log fold from the latest
    run dir (the source of truth that survives a restart). Fall back to
    the in-memory ``sizing_resolutions`` sidecar projection only when the
    fold is empty — preserves backward-compat for runs that predate
    Phase 8 (no WAL evidence on disk).

    Returns the most recent 50 rows (newest first). Empty when neither
    source has evidence. Never raises.
    """
    settings = get_settings()
    artifacts_root = Path(settings.live_runs_root).parent

    from app.engine.live.run_lookup import latest_run_dir_for_instance

    run_dir = latest_run_dir_for_instance(artifacts_root, strategy_instance_id)
    if run_dir is not None:
        wal_rows = _fold_wal_sizing_audit(run_dir)
        if wal_rows:
            return wal_rows

    try:
        sidecar_path = stable_live_state_path(artifacts_root, strategy_instance_id)
    except ValueError:
        return []
    try:
        envelope = LiveStateSidecarRepo(sidecar_path, trusted_root=artifacts_root / "live_state").read()
    except (LiveStateSidecarCorruptError, OSError):
        return []
    if envelope is None:
        return []
    rows = list(envelope.sizing_resolutions or [])
    rows.reverse()  # newest first for the UI
    return rows[:50]


def _fold_wal_sizing_audit(run_dir: Path) -> list[dict]:
    """VCR-0003 PR A — fold the durable Sizing-card audit from a run's WAL
    and skip log into the merged per-trade row shape the in-memory sidecar
    already surfaces.

    Reads SIZING_RESOLVED events from ``<run_dir>/intent_events.jsonl`` and
    SIZING_SKIP rows from ``<run_dir>/sizing_skip.jsonl``. The two sources
    are **disjoint** by construction (Phase 8 / ADR 0009 § 11): a skip
    never mints an intent_id and so never writes SIZING_RESOLVED; a
    non-skip always mints and writes SIZING_RESOLVED but never appends to
    the skip log. Returns the most recent 50 rows newest-first.

    Within the WAL slice, the authoritative chronology is ``seq`` (monotone
    by spec — ADR-0008 §3/§5), NOT ``ts_ms``: wall-clock can collide or
    step-back around fsync. ``read_tail()`` returns events in seq order, so
    the last 50 by iteration order are the last 50 by seq. The WAL
    contribution is capped by seq BEFORE the cross-source ts_ms sort so
    wall-clock reorder cannot silently drop the seq-most-recent
    SIZING_RESOLVED below the 50-row cap.

    Fail-open: a missing file is empty; a corrupt file degrades to "no
    rows from that source" for unrecoverable IO failures, and per-line for
    malformed JSONL. The Sizing card is a UI surface — it should render
    the surviving evidence rather than block on partial corruption. PR B
    will wire this as the primary source for ``_sizing_audit_rows``, with
    the sidecar projection as the legacy-run fallback.
    """
    wal_rows: list[dict] = []
    skip_rows: list[dict] = []

    wal_path = run_dir / "intent_events.jsonl"
    if wal_path.is_file():
        try:
            events = IntentWal(wal_path).read_tail()
        except (IntentWalCorruptError, OSError):
            events = []
        for event in events:
            # Use ``!=`` rather than ``is not``: a future ConfigDict tweak
            # (e.g. ``use_enum_values=True``) would make event_type a raw
            # ``str`` and ``is`` comparison would silently always be True,
            # dropping every SIZING_RESOLVED row.
            if event.event_type != IntentEventType.SIZING_RESOLVED:
                continue
            wal_rows.append(
                {
                    "ts_ms": event.ts_ms or 0,
                    "symbol": event.symbol or "",
                    "policy_kind": event.policy_kind or "",
                    "policy_value": event.policy_value or "",
                    "intended_qty": int(event.intended_qty or 0),
                    "reference_price": event.reference_price,
                    "sized_via": event.sized_via or "policy_set_holdings",
                    # VCR-0003 last-mile — surface the provenance stamp the
                    # engine mints at resolve time so the per-trade audit
                    # can attribute each fill to the policy that produced
                    # it ({reference_native, live_override, spec_default}).
                    # SIZING_RESOLVED events authored before this field
                    # was minted (or skip rows that never carry it) render
                    # as ``None`` — preserved as ``None`` rather than
                    # coerced to a sentinel string so the frontend can
                    # render the "unknown" badge variant.
                    "sizing_provenance_at_resolve_time": (event.sizing_provenance_at_resolve_time or None),
                }
            )

    skip_path = run_dir / "sizing_skip.jsonl"
    if skip_path.is_file():
        try:
            text = skip_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except ValueError:
                # Per-line fail-open: a single corrupt line drops only
                # itself, not the trailing prefix of valid lines.
                continue
            if not isinstance(payload, dict):
                # A syntactically-valid non-dict JSON value (``null``,
                # ``42``, ``[]``, ``"foo"``) would crash ``.get(...)`` with
                # AttributeError — guard it explicitly.
                continue
            try:
                row = {
                    "ts_ms": int(payload.get("ts_ms_utc") or 0),
                    "symbol": payload.get("symbol", ""),
                    "policy_kind": payload.get("policy_kind", ""),
                    "policy_value": payload.get("policy_value", ""),
                    "intended_qty": int(payload.get("target_qty") or 0),
                    "reference_price": payload.get("reference_price", ""),
                    "sized_via": "policy_set_holdings_skip",
                    "skipped": True,
                    "skip_reason": payload.get("reason", ""),
                    # VCR-0003 last-mile — sizing skip rows don't currently
                    # capture provenance (the IntentEvent invariant carve-out
                    # for skips lives in sizing_skip.jsonl, which has its
                    # own minimal schema). Surface ``None`` rather than
                    # omitting the key so the frontend renders the
                    # "unknown" badge variant uniformly across WAL and
                    # skip rows. If a future sizing_skip.jsonl revision
                    # adds the field, pass it through here.
                    "sizing_provenance_at_resolve_time": payload.get("sizing_provenance_at_resolve_time"),
                }
            except (TypeError, ValueError):
                # ``int(<list>)`` raises TypeError; ``int("not-a-number")``
                # raises ValueError. Either is per-line fail-open.
                continue
            skip_rows.append(row)

    # ADR-0008 — cap the WAL contribution by seq order (iteration order
    # from read_tail is monotone-by-spec) BEFORE the cross-source ts_ms
    # sort. Skip rows have no seq; cap them by ts_ms.
    wal_rows = wal_rows[-50:]
    skip_rows.sort(key=lambda r: r["ts_ms"], reverse=True)
    skip_rows = skip_rows[:50]

    merged = wal_rows + skip_rows
    merged.sort(key=lambda r: r["ts_ms"], reverse=True)
    return merged[:50]


def _sizing(
    root: Path, live_binding: LiveBinding | None, runs: list[dict], strategy_instance_id: str
) -> InstanceSizing | None:
    """ADR 0009 — surface the bound (or latest evidence) run's sizing surface
    to the instance console's Sizing card.

    A run with no ``live_config.sizing`` key (legacy/pre-policy) returns an
    ``InstanceSizing`` with ``policy=None`` and ``preset=None`` — the UI shows
    the honest "Pre-policy run" degraded variant (Decision 14). A ledger that
    predates the ``governed_by`` / ``sizing_provenance`` fields uses the same
    ``live_config`` / ``live_override`` defaults the engine derives.
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError):
        return None

    live_config = ledger.get("live_config") or {}
    sizing_payload = live_config.get("sizing") if isinstance(live_config, dict) else None
    governed_by = ledger.get("governed_by", "live_config")
    sizing_provenance = ledger.get("sizing_provenance", "live_override")

    preset: str | None
    if sizing_payload is None:
        preset = None
    else:
        kind = sizing_payload.get("kind") if isinstance(sizing_payload, dict) else None
        if kind == "FixedShares" and sizing_payload.get("value") == 1:
            preset = "safe_canary"
        elif kind == "SetHoldings" and sizing_payload.get("fraction") == "1.0":
            preset = "reference_parity"
        elif kind == "StrategyExplicit":
            preset = "explicit"
        else:
            preset = "custom"

    return InstanceSizing(
        policy=sizing_payload if isinstance(sizing_payload, dict) else None,
        preset=preset,
        governed_by=governed_by if governed_by in ("live_config", "strategy_explicit") else "live_config",
        sizing_provenance=sizing_provenance
        if sizing_provenance in ("reference_native", "live_override", "spec_default")
        else "live_override",
        per_trade_audit=[
            SizingAuditRow.model_validate(row)
            for row in _sizing_audit_rows(strategy_instance_id)
            if isinstance(row, dict)
        ],
    )


def _resolve_action_plan(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> dict | None:
    """PRD #593 Slice 1A — surface the bound (or evidence) run's declared
    instrument plan to the cockpit.

    Returns ``None`` when nothing is deployed, the ledger is unreadable,
    or the ledger pre-dates the ``live_config.action`` key — the cockpit
    distinguishes that case from "operator declared an empty plan", which
    serializes as ``{"on_enter": [], "on_exit": []}``.
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(
            "Failed to resolve action_plan from run ledger",
            extra={"run_dir": str(run_dir), "error": repr(exc)},
        )
        return None
    live_config = ledger.get("live_config") or {}
    if not isinstance(live_config, dict):
        return None
    action = live_config.get("action")
    return action if isinstance(action, dict) else None


def _resolve_lineage(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> dict | None:
    """PRD #593 Slice 1E (#598) — surface the bound (or evidence) run's
    redeploy lineage to the cockpit. The block is persisted by the
    daemon at deploy time alongside other unhashed metadata; it lives
    OUTSIDE ``live_config`` so the fields stay out of ``run_id``.

    Returns ``None`` when nothing is deployed, the ledger is unreadable,
    or the ledger pre-dates the lineage block (legacy runs).
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError):
        return None
    lineage = ledger.get("lineage")
    return lineage if isinstance(lineage, dict) else None


def _resolve_instrument_surface(
    root: Path, live_binding: LiveBinding | None, runs: list[dict]
) -> Literal["policy", "explicit"] | None:
    """PRD #593 Slice 1A — surface the registered ``instrument_surface``
    for the bound run's strategy. Informational in Slices 1–3 (every
    current strategy is ``explicit``); Slice 4 introduces enforcement.

    Returns ``None`` when nothing is deployed, the ledger is unreadable,
    the ledger has no ``strategy_key``, or the strategy is not registered
    — the cockpit treats null as "unknown" rather than substituting a
    default.
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(
            "Failed to resolve instrument_surface from run ledger",
            extra={"run_dir": str(run_dir), "error": repr(exc)},
        )
        return None
    strategy_key = ledger.get("strategy_key")
    if not isinstance(strategy_key, str) or not strategy_key:
        return None
    from app.engine.strategy.registry import _STRATEGY_REGISTRY

    reg = _STRATEGY_REGISTRY.get(strategy_key)
    if reg is None:
        return None
    return reg.instrument_surface


def _provenance(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> InstanceProvenance | None:
    """What the bound/evidence run's content-addressed identity attests to (the
    hashed deploy inputs), so the console can explain the hashes instead of
    dumping them. ``None`` when nothing is deployed or the ledger is unreadable;
    legacy ledgers contribute whatever fields they carry."""
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError):
        return None

    def _opt_int(key: str) -> int | None:
        value = ledger.get(key)
        return int(value) if isinstance(value, int) else None

    return InstanceProvenance(
        run_id=str(ledger.get("run_id") or run_dir.name),
        schema_version=str(ledger.get("schema_version", "")),
        code_sha=str(ledger.get("code_sha", "")),
        strategy_spec_path=str(ledger.get("strategy_spec_path", "")),
        strategy_spec_sha256=str(ledger.get("strategy_spec_sha256", "")),
        qc_audit_copy_path=str(ledger.get("qc_audit_copy_path", "")),
        qc_audit_copy_sha256=str(ledger.get("qc_audit_copy_sha256", "")),
        qc_cloud_backtest_id=str(ledger.get("qc_cloud_backtest_id", "")),
        account_id=str(ledger.get("account_id", "")),
        start_date_ms=_opt_int("start_date_ms"),
        created_at_ms=_opt_int("created_at_ms"),
        live_config=dict(ledger.get("live_config") or {}),
    )


async def _build_live_instance_summaries(
    settings: IbkrSettings,
    root: Path,
) -> list[LiveInstanceSummary]:
    by_instance = _visible_runs_by_instance(root)

    _result, daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
    daemon_reachable = daemon is not None
    daemon_by_sid: dict[str, dict] = {}
    if daemon:
        for inst in daemon.get("instances", []):
            sid = inst.get("strategy_instance_id")
            if sid:
                daemon_by_sid[sid] = inst

    summaries: list[LiveInstanceSummary] = []
    for sid in sorted(set(by_instance) | set(daemon_by_sid)):
        if sid not in by_instance and _sid_has_soft_deletion(root.parent, sid):
            continue
        managed = daemon_by_sid.get(sid)
        runs = by_instance.get(sid, [])
        if managed is not None:
            proc_state = str(managed.get("process", {}).get("state") or "idle")
            bound = managed.get("run_id") if proc_state in _LIVE_STATES else None
        else:
            proc_state = "offline" if daemon_reachable else "unreachable"
            bound = None
        desired = _resolve_desired_state(root, sid)
        # PRD #616 — surface the per-instance readiness verdict so the
        # cockpit can render the outer-tab badge (PROCESS · READINESS)
        # without an N+1 fetch of every instance's full status.
        live_binding_for_sid = LiveBinding(run_id=bound) if bound is not None else None
        readiness = _resolve_readiness(root, live_binding_for_sid, runs, desired.state)
        readiness_verdict: Literal["READY", "BLOCKED", "DEGRADED", "UNKNOWN"]
        if readiness is None or readiness.verdict not in ("READY", "BLOCKED", "DEGRADED"):
            readiness_verdict = "UNKNOWN"
        else:
            readiness_verdict = readiness.verdict  # type: ignore[assignment]
        readiness_as_of_ms = readiness.as_of_ms if readiness is not None else None
        summaries.append(
            LiveInstanceSummary(
                strategy_instance_id=sid,
                process_state=proc_state,
                bound_run_id=bound,
                latest_run_id=runs[0]["run_id"] if runs else None,
                desired_state=desired.state,
                readiness_verdict=readiness_verdict,
                readiness_as_of_ms=readiness_as_of_ms,
            )
        )
    return summaries


@router.get("", response_model=list[LiveInstanceSummary])
async def list_live_instances() -> list[LiveInstanceSummary]:
    """Account fleet overview: every known strategy instance, live or not."""
    settings = get_settings()
    root = Path(settings.live_runs_root)
    return await _build_live_instance_summaries(settings, root)


def _daemon_process_from_instance(managed: dict | None) -> dict | None:
    if managed is None:
        return None
    process = dict(managed.get("process") or {})
    if not process.get("run_id") and managed.get("run_id"):
        process["run_id"] = managed.get("run_id")
    return process


async def _resolve_activity_publisher_for_status(
    sid: str,
    live_binding: LiveBinding | None,
) -> tuple[BrokerActivityPublisher | None, int | None]:
    """Read publisher facts without starting or mutating producer lifecycle."""
    del live_binding
    registry = get_publisher_registry()
    publisher = registry.get(sid)
    return publisher, registry.registered_at_ms(sid)


async def _resolve_daemon_diagnostic_condition_for_status(
    sid: str,
) -> DaemonDominantCondition | None:
    try:
        report = await asyncio.wait_for(
            get_daemon_diagnostics_service().report(strategy_instance_id=sid),
            timeout=_STATUS_DAEMON_DIAGNOSTICS_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning(
            "status-time daemon diagnostics deferred (%s)",
            exc,
            extra={"strategy_instance_id": sid},
        )
        return None
    instance = next(
        (row for row in report.per_instance if row.strategy_instance_id == sid),
        None,
    )
    if instance is None:
        return None
    return instance.dominant_condition


async def _resolve_fleet_blocks_starts_for_status(
    settings: IbkrSettings,
    root: Path,
) -> bool:
    if not settings.fleet_dirty_blocks_starts:
        return False
    try:
        fleet = await _compute_account_fleet_contamination(settings, root)
    except Exception as exc:
        logger.warning("status-time fleet contamination deferred (%s)", exc)
        return False
    return fleet.policy_blocks_starts


def _get_surface_assembler() -> LiveInstanceSurfaceAssembler:
    return LiveInstanceSurfaceAssembler(
        LiveInstanceSurfaceDependencies(
            get_settings=get_settings,
            visible_runs_by_instance=_visible_runs_by_instance,
            sid_has_soft_deletion=_sid_has_soft_deletion_from_directory,
            bot_soft_deleted_detail=_bot_soft_deleted_detail,
            fetch_instance_process=host_daemon_client.fetch_instance_process,
            interpret_daemon_process=_interpret_daemon_process,
            scan_runs_by_instance=_scan_runs_by_instance,
            resolve_account_freeze=_resolve_account_freeze,
            resolve_desired_state=_resolve_desired_state,
            strategy_state=_strategy_state,
            instance_last_exit=_instance_last_exit,
            resolve_readiness=_resolve_readiness,
            resolve_readonly_default=_resolve_readonly_default,
            instance_broker=_instance_broker,
            start_defaults=_start_defaults,
            sizing=_sizing,
            resolve_action_plan=_resolve_action_plan,
            get_daemon_connectivity_monitor=get_daemon_connectivity_monitor,
            resolve_resume_guard_state_for=_resolve_resume_guard_state_for,
            now_ms=_now_ms,
            resolve_engine_runtime_snapshot_and_freshness=(_resolve_engine_runtime_snapshot_and_freshness),
            safety_verdict_final_from_engine_runtime=(_safety_verdict_final_from_engine_runtime),
            resolve_safety_verdict_final=_resolve_safety_verdict_final,
            broker_connection_state_from_engine_runtime=(_broker_connection_state_from_engine_runtime),
            broker_connection_state_from_readiness=(_broker_connection_state_from_readiness),
            instance_ledger_account_id=_instance_ledger_account_id,
            crash_recovery_gate_for_instance=crash_recovery_gate_for_instance,
            resolve_account_owner_surface=_resolve_account_owner_surface,
            get_account_truth_snapshot_provider=get_account_truth_snapshot_provider,
            resolve_latest_mutation=_resolve_latest_mutation,
            resolve_broker_observation_consistency=(_resolve_broker_observation_consistency),
            resolve_reconciliation_inputs=_resolve_reconciliation_inputs,
            resolve_activity_publisher_for_status=_resolve_activity_publisher_for_status,
            resolve_fleet_blocks_starts_for_status=(_resolve_fleet_blocks_starts_for_status),
            resolve_daemon_diagnostic_condition_for_status=(_resolve_daemon_diagnostic_condition_for_status),
            resolve_incident_headline=_resolve_incident_headline,
            resolve_bot_lifecycle_state=_resolve_bot_lifecycle_state,
            resolve_live_run_dir=_resolve_live_run_dir,
            compute_operator_surface=compute_operator_surface,
            resolve_durable_control_write_failure_for_status=(_resolve_durable_control_write_failure_for_status),
            resolve_start_run_id=_resolve_start_run_id,
            active_roll_call_offer=_active_roll_call_offer,
            lifecycle_conditions_for_instance=lifecycle_conditions_for_instance,
            project_bot_daily_lifecycle=project_bot_daily_lifecycle,
            provenance=_provenance,
            resolve_symbol=_resolve_symbol,
            resolve_instrument_surface=_resolve_instrument_surface,
            resolve_lineage=_resolve_lineage,
            read_instance_live_state=_read_instance_live_state,
            session_started_at_ms=_session_started_at_ms,
            project_intent_events=project_intent_events,
            project_instance_account_lifecycle_events=(_project_instance_account_lifecycle_events),
            sort_lifecycle_events=sort_lifecycle_events,
            compose_bot_lifecycle_chart=compose_bot_lifecycle_chart,
        )
    )


async def _resolve_instance_status_from_process(
    sid: str,
    root: Path,
    settings: IbkrSettings,
    daemon_process: dict | None,
    *,
    runs_by_instance: dict[str, list[dict]] | None = None,
) -> LiveInstanceStatus:
    """Compatibility entry point for non-HTTP projections and tests."""

    return await _get_surface_assembler().assemble_from_process(
        sid,
        root,
        settings,
        daemon_process,
        runs_by_instance=runs_by_instance,
    )


async def _assemble_instance_surface(strategy_instance_id: str) -> LiveInstanceStatus:
    return await _get_surface_assembler().assemble(strategy_instance_id)


async def _reconcile_surface_activity_publisher(snapshot: LiveInstanceStatus) -> None:
    """Match publisher ownership to the producer-observed live binding."""

    strategy_instance_id = snapshot.strategy_instance_id
    registry = get_publisher_registry()
    if snapshot.live_binding is None:
        await registry.unregister(strategy_instance_id)
        return
    if registry.get(strategy_instance_id) is not None:
        return

    from app.routers.broker_activity import (
        PublisherBootstrapError,
        bootstrap_publisher_for_instance,
    )

    try:
        await asyncio.wait_for(
            bootstrap_publisher_for_instance(strategy_instance_id),
            timeout=2.0,
        )
    except TimeoutError:
        logger.warning(
            "surface producer activity bootstrap timed out",
            extra={"strategy_instance_id": strategy_instance_id},
        )
    except PublisherBootstrapError as exc:
        logger.info(
            "surface producer activity bootstrap deferred (%s): %s",
            exc.code,
            exc.detail,
            extra={
                "strategy_instance_id": strategy_instance_id,
                "bootstrap_error_code": exc.code,
            },
        )
    except Exception:
        logger.exception(
            "surface producer activity bootstrap failed unexpectedly",
            extra={"strategy_instance_id": strategy_instance_id},
        )


def _surface_hub_for(strategy_instance_id: str) -> SurfaceHub[LiveInstanceStatus]:
    return _SURFACE_HUBS.get_or_create(
        strategy_instance_id,
        assemble=lambda: _assemble_instance_surface(strategy_instance_id),
        on_snapshot=_reconcile_surface_activity_publisher,
    )


async def start_surface_hubs() -> None:
    """Start producer lifecycles for every bot visible at data-plane boot."""

    settings = get_settings()
    root = Path(settings.live_runs_root)
    strategy_instance_ids = set(_visible_runs_by_instance(root))
    _result, daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
    for instance in (daemon or {}).get("instances", []):
        sid = instance.get("strategy_instance_id")
        if isinstance(sid, str) and sid:
            strategy_instance_ids.add(sid)
    hubs = [_surface_hub_for(sid) for sid in sorted(strategy_instance_ids)]
    await _SURFACE_HUBS.start_all(hubs)


async def stop_surface_hubs() -> None:
    """Stop every producer task during data-plane shutdown."""

    await _SURFACE_HUBS.stop_all()


async def _ensure_surface_hub_started(
    strategy_instance_id: str,
) -> None:
    hub = _surface_hub_for(strategy_instance_id)
    if not hub.is_running:
        try:
            await hub.start()
        except Exception:
            logger.exception(
                "surface hub startup deferred after mutation",
                extra={"strategy_instance_id": strategy_instance_id},
            )


@router.get("/catalog", response_model=BotCatalogResponse)
async def list_bot_catalog() -> BotCatalogResponse:
    """Server-authored bot catalog cards for the frontend DataView."""
    settings = get_settings()
    root = Path(settings.live_runs_root)
    by_instance = await asyncio.to_thread(_visible_runs_by_instance, root)

    _result, daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
    daemon_by_sid: dict[str, dict] = {}
    if daemon:
        for inst in daemon.get("instances", []):
            sid = inst.get("strategy_instance_id")
            if sid:
                daemon_by_sid[sid] = inst

    rows = []
    trading_mode = trading_mode_from_configured_mode(getattr(settings, "mode", None))
    for sid in sorted(set(by_instance) | set(daemon_by_sid)):
        if sid not in by_instance and _sid_has_soft_deletion(root.parent, sid):
            continue
        status_view = await _resolve_instance_status_from_process(
            sid,
            root,
            settings,
            _daemon_process_from_instance(daemon_by_sid.get(sid)),
            runs_by_instance=by_instance,
        )
        row = compose_bot_catalog_row(status_view, trading_mode)
        rows.append(
            row.model_copy(
                update={
                    "attendance": attendance_for_instance(
                        runs=by_instance.get(sid, []),
                        lifecycle_state=_resolve_bot_lifecycle_state(root, sid),
                        read_sidecar=_read_sidecar,
                    )
                }
            )
        )
    rows.sort(key=lambda row: (row.created_at_ms or row.last_run_at_ms or 0, row.name), reverse=True)
    return BotCatalogResponse(
        bots=rows,
        roll_call=roll_call_summary_from_rows(rows, now_ms=_now_ms()),
        evening_report=evening_report_from_rows(rows, now_ms=_now_ms()),
    )


@router.post("/roll-call", response_model=BotRollCallResponse)
async def run_roll_call() -> BotRollCallResponse:
    """Persist the morning roll-call offers for all eligible non-retired bots."""

    settings = get_settings()
    root = Path(settings.live_runs_root)
    by_instance = await asyncio.to_thread(_visible_runs_by_instance, root)
    _result, daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
    daemon_by_sid: dict[str, dict] = {}
    if daemon:
        for inst in daemon.get("instances", []):
            sid = inst.get("strategy_instance_id")
            if sid:
                daemon_by_sid[sid] = inst

    now_ms = _now_ms()
    rows = []
    offers = []
    retired_count = 0
    trading_mode = trading_mode_from_configured_mode(getattr(settings, "mode", None))
    summary_session_date: str | None = None
    summary_effective_stop_ms: int | None = None
    for sid in sorted(set(by_instance) | set(daemon_by_sid)):
        lifecycle_state = _resolve_bot_lifecycle_state(root, sid)
        if lifecycle_state is not None and lifecycle_state.phase == BotLifecyclePhase.RETIRED:
            retired_count += 1
            continue
        status_view = await _resolve_instance_status_from_process(
            sid,
            root,
            settings,
            _daemon_process_from_instance(daemon_by_sid.get(sid)),
            runs_by_instance=by_instance,
        )
        rows.append(compose_bot_catalog_row(status_view, trading_mode))
        if not status_is_roll_call_eligible(status_view):
            continue
        runs = by_instance.get(sid, [])
        if not runs:
            continue
        run_dir = Path(runs[0]["run_dir"])
        boundary = start_boundary_verdict(now_ms, _live_config_for_run_dir(run_dir))
        if not boundary.allowed or boundary.effective_stop_ms is None or boundary.session_date is None:
            continue
        offer = ensure_roll_call_offer(
            root,
            sid=sid,
            run_id=status_view.operator_surface.host_process.start_capability.run_id or str(runs[0]["run_id"]),
            session_date=boundary.session_date,
            issued_at_ms=now_ms,
            expires_at_ms=boundary.effective_stop_ms,
            evidence_snapshot={
                "readiness_verdict": (status_view.readiness.verdict if status_view.readiness is not None else None),
                "process_state": status_view.process.state,
                "display_status": status_view.daily_lifecycle.display_status,
            },
        )
        offers.append(roll_call_offer_schema(offer))
        summary_session_date = summary_session_date or boundary.session_date
        summary_effective_stop_ms = (
            boundary.effective_stop_ms
            if summary_effective_stop_ms is None
            else min(summary_effective_stop_ms, boundary.effective_stop_ms)
        )

    return BotRollCallResponse(
        summary=roll_call_summary_from_rows(rows, now_ms=now_ms).model_copy(
            update={
                "ready": len(offers),
                "retired": retired_count,
                "session_date": summary_session_date,
                "effective_stop_ms": summary_effective_stop_ms,
            }
        ),
        offers=offers,
    )


def _live_config_for_run_dir(run_dir: Path) -> Mapping[str, object] | None:
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, json.JSONDecodeError):
        return None
    live_config = ledger.get("live_config")
    return live_config if isinstance(live_config, Mapping) else None


def _is_retired_bot(root: Path, sid: str) -> bool:
    lifecycle_state = _resolve_bot_lifecycle_state(root, sid)
    return lifecycle_state is not None and lifecycle_state.phase == BotLifecyclePhase.RETIRED


@router.delete("/{strategy_instance_id}", response_model=BotDeleteResponse)
async def delete_instance(
    strategy_instance_id: str,
    body: Annotated[BotDeleteRequest | None, Body()] = None,
) -> BotDeleteResponse:
    """Soft-delete a stopped bot from operator catalog/control surfaces.

    The deletion marker is durable and run-id scoped. It hides every run that
    currently belongs to the instance while preserving the artifacts for audit.
    A later redeploy with a new run id is visible again.
    """
    sid = _validate_instance_id(strategy_instance_id)
    request = body or BotDeleteRequest()
    settings = get_settings()
    root = Path(settings.live_runs_root)

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    if daemon is None:
        if not _is_retired_bot(root, sid):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "HOST_SERVICE_OFFLINE",
                    "message": "Cannot delete this bot because the bot service is offline; stopped state is unproven.",
                },
            )
    else:
        process, _live_binding = _interpret_daemon_process(daemon, root)
        if process.state in _LIVE_STATES:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "BOT_PROCESS_ACTIVE",
                    "message": "Stop the bot process before deleting it from the catalog.",
                    "process_state": process.state,
                    "bound_run_id": process.bound_run_id,
                },
            )

    runs = _scan_runs_by_instance(root).get(sid, [])
    existing = _read_bot_deletion_for_endpoint(root.parent, sid)
    if not runs and existing is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"reason_code": "BOT_NOT_FOUND", "message": f"No bot exists for {sid}."},
        )

    run_ids = [str(run["run_id"]) for run in runs]
    try:
        record = soft_delete_bot_runs(
            root.parent,
            sid,
            run_ids=run_ids,
            deleted_by=request.deleted_by,
            reason=request.reason,
            now_ms=_now_ms(),
        )
    except (ValueError, BotDeletionCorruptError) as exc:
        logger.warning(
            "failed to soft-delete bot",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="bot deletion marker could not be written",
        ) from exc
    # Stop the producer before unregistering the publisher so an in-flight
    # snapshot observer cannot recreate publisher ownership after cleanup.
    for cleanup in (
        lambda: _SURFACE_HUBS.remove(sid),
        lambda: get_publisher_registry().unregister(sid),
    ):
        try:
            await cleanup()
        except Exception as cleanup_error:
            logger.error(
                "post-delete producer cleanup failed",
                extra={
                    "strategy_instance_id": sid,
                    "exception": repr(cleanup_error),
                },
            )
    return _bot_delete_response(root.parent, record)


def _read_bot_deletion_for_endpoint(artifacts_root: Path, sid: str) -> BotDeletionRecord | None:
    try:
        return read_bot_deletion(artifacts_root, sid)
    except (ValueError, BotDeletionCorruptError) as exc:
        logger.warning(
            "failed to read bot deletion marker",
            extra={"strategy_instance_id": sid, "exception": repr(exc)},
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="bot deletion marker could not be read",
        ) from exc


def _bot_delete_response(artifacts_root: Path, record: BotDeletionRecord) -> BotDeleteResponse:
    return BotDeleteResponse(
        strategy_instance_id=record.strategy_instance_id,
        deleted_at_ms=record.deleted_at_ms,
        deleted_by=record.deleted_by,
        reason=record.reason,
        deleted_run_ids=list(record.deleted_run_ids),
        marker_path=str(stable_bot_deletion_path(artifacts_root, record.strategy_instance_id)),
    )


def _raise_if_deploy_admission_blocks_start(
    live_runs_root: Path,
    body: LiveInstanceDeployRequest,
) -> None:
    if not body.start:
        return
    sid = body.strategy_instance_id.strip()
    visible_runs = _visible_runs_by_instance(live_runs_root).get(sid, []) if sid else []
    inherited_symbol = _resolve_symbol_resolution(live_runs_root, None, visible_runs) if sid else None
    broker = _instance_broker(live_runs_root, sid) if sid and visible_runs else None
    block = evaluate_deploy_start_admission(
        body=body,
        sid=sid,
        visible_runs=visible_runs,
        inherited_symbol=inherited_symbol,
        broker=broker,
    )
    if block is not None:
        raise HTTPException(block.status_code, detail=block.detail)


async def _host_deploy_request_from_public(
    body: LiveInstanceDeployRequest,
) -> HostRunnerDeployRequest:
    broker_account, broker_known = await _fetch_broker_connected_account()
    if not broker_known or not broker_account:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=("Connected broker account unavailable. Connect the broker session before deploying."),
        )
    client_account = body.client_supplied_account_id()
    if client_account is not None and client_account != broker_account:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                "Deploy account mismatch: the connected broker account is "
                f"{broker_account}, but the request contained {client_account}. "
                "Refresh broker state and deploy again."
            ),
        )
    payload = body.model_dump(
        exclude={
            "account_id",
            "inherited_symbol",
            "inherited_symbol_source",
            "identity_coherence_confirmation",
            "inherited_exposure_posture",
            "inherited_exposure_pending_order_count",
            "inherited_exposure_positions",
            "inherited_exposure_source",
            "exposure_coherence_confirmation",
        },
    )
    return HostRunnerDeployRequest.model_validate({**payload, "account_id": broker_account})


@router.get("/deploy-preflight", response_model=DeployPreflightResponse)
async def deploy_preflight(
    strategy_key: str,
    account_id: str,
    instance_id: str,
) -> DeployPreflightResponse:
    """Return backend-authored blockers standing between deploy and a running bot."""

    try:
        signals = await deploy_preflight_service.gather_deploy_preflight_signals(
            strategy_key.strip(),
            account_id.strip(),
            instance_id.strip(),
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StrategyValidationManifestError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    blockers = deploy_preflight_service.author_deploy_blockers(signals)
    return DeployPreflightResponse(
        ready=not any(blocker.severity == "blocking" for blocker in blockers),
        blockers=blockers,
    )


async def _raise_if_deploy_preflight_blocks_start(
    request: HostRunnerDeployRequest,
) -> None:
    if not request.start:
        return
    try:
        signals = await deploy_preflight_service.gather_deploy_preflight_signals(
            request.strategy_key.strip(),
            request.account_id.strip(),
            request.strategy_instance_id.strip(),
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StrategyValidationManifestError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    blockers = [
        blocker
        for blocker in deploy_preflight_service.author_deploy_blockers(signals)
        if blocker.severity == "blocking"
    ]
    if not blockers:
        return
    first = blockers[0]
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason_code": "DEPLOY_PREFLIGHT_BLOCKED",
            "message": f"Deploy preflight blocked deploy & run: {first.headline}.",
            "blockers": [blocker.model_dump(mode="json") for blocker in blockers],
        },
    )


@router.post("", response_model=HostRunnerDeployResponse, status_code=status.HTTP_201_CREATED)
async def deploy_instance(body: LiveInstanceDeployRequest, response: Response) -> HostRunnerDeployResponse:
    """Create a run (deploy a strategy) by forwarding to the host daemon (ADR 0006).

    Deploy is a host-daemon operation: ``init-ledger`` runs a git clean-tree
    check and hashes ``git HEAD`` into the content-addressed ``run_id``, and only
    the host has the working tree. This endpoint forwards (mirroring how
    Start/Stop forward) and propagates the daemon's structured precondition
    statuses: dirty tree / collision -> 409, missing spec or audit file -> 400,
    git unavailable / daemon unreachable -> 503.

    Idempotent on the ``run_id``: an identical re-deploy returns 200 with
    ``created=false`` rather than erroring (the run already exists).
    """
    settings = get_settings()
    daemon_request = await _host_deploy_request_from_public(body)
    account_freeze = read_account_freeze(
        Path(settings.live_runs_root).parent,
        daemon_request.account_id,
    )
    if account_freeze is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_FROZEN",
                "message": "This broker account is frozen until unresolved exposure is reconciled.",
                "gate_result": account_freeze.to_gate_result().model_dump(mode="json"),
            },
        )
    if daemon_request.start and daemon_request.strategy_instance_id:
        _raise_if_crash_recovery_blocks_start(
            Path(settings.live_runs_root).parent,
            account_id=daemon_request.account_id,
            strategy_instance_id=daemon_request.strategy_instance_id,
        )
    _raise_if_deploy_admission_blocks_start(
        Path(settings.live_runs_root),
        body,
    )
    await _raise_if_deploy_preflight_blocks_start(daemon_request)
    if daemon_request.start:
        verdict = start_boundary_verdict(_now_ms(), daemon_request.live_config)
        if not verdict.allowed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": verdict.reason_code,
                    "message": verdict.message,
                    "gate_id": "daily_lifecycle.effective_stop",
                    "strategy_instance_id": daemon_request.strategy_instance_id or None,
                    "session_date": verdict.session_date,
                    "effective_stop_ms": verdict.effective_stop_ms,
                },
            )
        daemon_request = daemon_request.model_copy(update={"start": False})
    try:
        result = await host_daemon_client.deploy(
            settings.live_runner_daemon_url,
            daemon_request.model_dump(),
        )
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("deploy", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc

    try:
        parsed = HostRunnerDeployResponse.model_validate(result)
    except ValidationError as exc:
        # Upstream (daemon) contract failure — surface as a gateway error, not a
        # 500 that makes the data plane look broken.
        logger.warning("invalid deploy payload from host daemon: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="host daemon returned an invalid deploy payload",
        ) from exc
    if not parsed.created:
        response.status_code = status.HTTP_200_OK
    if body.strategy_instance_id:
        await _ensure_surface_hub_started(body.strategy_instance_id)
    return parsed


def _parse_action_response(result: dict) -> HostRunnerActionResponse:
    """Validate a daemon start/stop body or surface a 502 gateway error."""
    try:
        return HostRunnerActionResponse.model_validate(result)
    except ValidationError as exc:
        logger.warning("invalid start/stop payload from host daemon: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="host daemon returned an invalid start/stop payload",
        ) from exc


async def _mutation_rung_receipts_from_process(
    sid: str,
    root: Path,
    settings: IbkrSettings,
    daemon_process: dict | None,
    *,
    mutation_key: str,
) -> tuple[MutationRungReceipt, list[MutationRungReceipt]]:
    status_view = await _resolve_instance_status_from_process(
        sid,
        root,
        settings,
        daemon_process,
        runs_by_instance=_visible_runs_by_instance(root),
    )
    return mutation_rung_receipts(status_view, mutation_key=mutation_key)


async def _mutation_rung_receipts_for_instance(
    sid: str,
    root: Path,
    settings: IbkrSettings,
    *,
    mutation_key: str,
) -> tuple[MutationRungReceipt, list[MutationRungReceipt]]:
    _result, daemon_process = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    return await _mutation_rung_receipts_from_process(
        sid,
        root,
        settings,
        daemon_process,
        mutation_key=mutation_key,
    )


def _strategy_instance_id_for_run(root: Path, run_id: str) -> str | None:
    for sid, runs in _scan_runs_by_instance(root).items():
        if any(run["run_id"] == run_id for run in runs):
            return sid
    return None


@router.post("/preview-action-plan", response_model=ActionPlanPreviewResponse)
async def preview_action_plan(plan: ActionPlan) -> ActionPlanPreviewResponse:
    """PRD #593 Slice 1D (#597) — non-blocking parity preview.

    Stateless, side-effect-free. Pydantic rejects malformed plans (422)
    at the body-validation step; semantically valid plans pass through
    to ``parity_diagnostics``. Always 200 OK regardless of warning
    count — submit-time gating is the operator's call (the deploy
    boundary enforces only the schema). ADR 0012 §"Architectural
    decisions" pins that this endpoint MUST NOT consult
    ``live_config.symbol``, the instance roster, or any other session
    context; the plan is the only input.
    """

    return ActionPlanPreviewResponse(warnings=parity_diagnostics(plan))


_START_DAEMON_STATE_REASON: dict[str, str] = {
    "running": "ALREADY_RUNNING",
    "stopping": "STOPPING",
}


def _bot_soft_deleted_detail(sid: str, run_id: str | None = None) -> dict[str, str]:
    detail = {
        "reason_code": "BOT_SOFT_DELETED",
        "message": f"{sid} has been deleted from the bot catalog.",
        "strategy_instance_id": sid,
    }
    if run_id is not None:
        detail["run_id"] = run_id
    return detail


def _raise_if_start_boundary_blocks(root: Path, sid: str, *, now_ms: int) -> None:
    runs = _visible_runs_by_instance(root).get(sid, [])
    if not runs:
        return
    run_dir = Path(runs[0]["run_dir"])
    verdict = start_boundary_verdict(now_ms, _live_config_for_run_dir(run_dir))
    if verdict.allowed:
        return
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason_code": verdict.reason_code,
            "message": verdict.message,
            "gate_id": "daily_lifecycle.effective_stop",
            "strategy_instance_id": sid,
            "session_date": verdict.session_date,
            "effective_stop_ms": verdict.effective_stop_ms,
        },
    )


def _assert_roll_call_offer_allows_start(
    root: Path,
    sid: str,
    run_id: str,
    body: HostRunnerStartRequest,
    *,
    now_ms: int,
) -> None:
    if body.roll_call_offer_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ROLL_CALL_OFFER_REQUIRED",
                "message": "Run roll call and start from the current offer.",
                "gate_id": "daily_lifecycle.roll_call_offer",
                "strategy_instance_id": sid,
            },
        )
    active = _active_roll_call_offer(root, sid, now_ms=now_ms)
    if active is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ROLL_CALL_OFFER_EXPIRED",
                "message": "The roll-call start offer is absent or expired. Run roll call again.",
                "gate_id": "daily_lifecycle.roll_call_offer",
                "strategy_instance_id": sid,
            },
        )
    if active.offer_id != body.roll_call_offer_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ROLL_CALL_OFFER_STALE",
                "message": "This start request does not match the current roll-call offer.",
                "gate_id": "daily_lifecycle.roll_call_offer",
                "strategy_instance_id": sid,
                "current_offer_id": active.offer_id,
            },
        )
    if active.run_id != run_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ROLL_CALL_OFFER_RUN_MISMATCH",
                "message": "This roll-call offer belongs to a different run. Run roll call again.",
                "gate_id": "daily_lifecycle.roll_call_offer",
                "strategy_instance_id": sid,
                "run_id": run_id,
                "offer_run_id": active.run_id,
            },
        )


def _raise_if_lifecycle_retired(root: Path, sid: str) -> None:
    try:
        record = _bot_lifecycle_state_repo(root, sid).read()
    except BotLifecycleStateCorruptError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "BOT_LIFECYCLE_STATE_UNREADABLE",
                "message": "The bot lifecycle state is unreadable. Repair it before starting.",
                "gate_id": "daily_lifecycle.phase",
                "strategy_instance_id": sid,
            },
        ) from exc
    if record is not None and record.phase == BotLifecyclePhase.RETIRED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "BOT_RETIRED",
                "message": "This bot is retired. Deploy a replacement bot before starting.",
                "gate_id": "daily_lifecycle.phase",
                "strategy_instance_id": sid,
            },
        )


def _start_intent_repo(root: Path, sid: str) -> DesiredStateRepo:
    artifacts_root = _desired_state_root(root)
    try:
        sidecar_path = stable_desired_state_path(artifacts_root, sid)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc
    return DesiredStateRepo(sidecar_path, trusted_root=artifacts_root / "live_state")


def _persist_start_intent(root: Path, sid: str) -> DesiredStateRecord | None:
    """Persist the single Start path's durable intent before daemon launch."""

    repo = _start_intent_repo(root, sid)
    try:
        previous = repo.read()
        repo.set(
            DesiredState.RUNNING,
            updated_by="system",
            reason="daily_lifecycle.start",
            now_ms=_now_ms(),
        )
    except DesiredStateCorruptError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "DESIRED_STATE_UNREADABLE",
                "message": "The durable desired-state sidecar is unreadable. Repair it before starting.",
                "gate_id": "desired_state.start",
                "strategy_instance_id": sid,
            },
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason_code": "DESIRED_STATE_WRITE_FAILED",
                "message": "Could not persist the start intent before launching the bot.",
                "gate_id": "desired_state.start",
                "strategy_instance_id": sid,
            },
        ) from exc
    return previous


def _restore_start_intent(
    root: Path,
    sid: str,
    previous: DesiredStateRecord | None,
) -> None:
    repo = _start_intent_repo(root, sid)
    try:
        if previous is None:
            repo.delete()
        else:
            repo.write(previous)
    except (OSError, ValueError) as exc:
        logger.warning(
            "failed to restore start intent after rejected start",
            extra={
                "strategy_instance_id": sid,
                "error": str(exc),
            },
        )


async def _assert_start_allowed(run_id: str, body: HostRunnerStartRequest, settings) -> None:
    """Re-evaluate the start gates before forwarding /runs/{run_id}/start.

    ADR 0013 amendment 2026-06-22 + design "Architectural permission for
    Start bot process": the cockpit's ``host_process.start_capability``
    is a status-time projection (polled every 4s). The data plane must
    re-check the gates that block enablement before forwarding so a
    stale ``enabled=true`` cannot bypass a STOPPED / poisoned / RUNNING
    transition that happened between the trader's last poll and click.

    Mirrors the projection's ``HostProcessStartDisabledReasonCode`` enum.
    Legacy runs without a ledger / strategy_instance_id are passed
    through — only the daemon is the gate for those.
    """
    root = Path(settings.live_runs_root)
    # Resolve (sid, run_dir) through a disk scan rather than ``root / run_id``.
    # ``_scan_runs_by_instance`` iterates ``root.iterdir()`` so every returned
    # ``run_dir`` is a path produced by the filesystem walk, never a join of
    # user-controlled input — this is what CodeQL traces as "untainted" (the
    # ``_validate_run_id`` sanitizer alone does not bridge the function-call
    # boundary in py/path-injection).
    sid: str | None = None
    run_dir: Path | None = None
    for candidate_sid, runs in _scan_runs_by_instance(root).items():
        for run in runs:
            if run["run_id"] == run_id:
                sid = candidate_sid
                run_dir = Path(run["run_dir"])
                break
        if run_dir is not None:
            break
    if sid is None or run_dir is None:
        return  # unknown run_id — daemon will 404; not our gate

    if _run_is_soft_deleted(root.parent, sid, run_id):
        raise HTTPException(
            status.HTTP_410_GONE,
            detail=_bot_soft_deleted_detail(sid, run_id),
        )
    _raise_if_lifecycle_retired(root, sid)

    account_freeze = _resolve_account_freeze(
        root.parent,
        [{"run_dir": str(run_dir)}],
    )
    if account_freeze is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_FROZEN",
                "message": "This broker account is frozen until unresolved exposure is reconciled.",
                "gate_result": account_freeze.to_gate_result().model_dump(mode="json"),
            },
        )
    account_id = _run_dir_account_id(run_dir)
    if account_id is not None:
        _raise_if_crash_recovery_blocks_start(
            root.parent,
            account_id=account_id,
            strategy_instance_id=sid,
        )

    if (run_dir / "poisoned.flag").exists():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "STOPPED_REQUIRES_REDEPLOY",
                "message": "This run is permanently retired. Redeploy the bot to trade again.",
            },
        )
    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    if daemon is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "HOST_SERVICE_OFFLINE",
                "message": "The bot service is offline. Start it on the host machine first.",
            },
        )
    daemon_state = str(daemon.get("state") or "idle")
    reason = _START_DAEMON_STATE_REASON.get(daemon_state)
    if reason is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": reason,
                "message": (
                    "The bot is already running."
                    if reason == "ALREADY_RUNNING"
                    else "The bot is shutting down. Wait for it to finish before starting again."
                ),
            },
        )
    # idle / exited / unrecognised -> proceed; daemon performs its own start gates.
    now_ms = _now_ms()
    _raise_if_start_boundary_blocks(root, sid, now_ms=now_ms)
    _assert_roll_call_offer_allows_start(root, sid, run_id, body, now_ms=now_ms)


@router.post("/runs/{run_id}/start", response_model=HostRunnerActionResponse)
async def start_run(run_id: str, body: HostRunnerStartRequest) -> HostRunnerActionResponse:
    """Launch the host runner for ``run_id`` by forwarding to the daemon (ADR 0007).

    Start/Stop are routed through the data plane — not called from the browser —
    because the daemon now enforces a mandatory ``X-Live-Runner-Token`` on every
    actuation route, and the browser must never hold that shared secret. The data
    plane reads the token from the artifacts bind mount and forwards it. The
    daemon's statuses propagate verbatim: bad ``strategy``/spec mismatch -> 400,
    missing run -> 404, subprocess/daemon unreachable -> 503.

    Before forwarding, the data plane re-evaluates the same start gates the
    cockpit's ``host_process.start_capability`` projection used (ADR 0013
    amendment 2026-06-22): poisoned-flag, account freeze, daemon ``running`` /
    ``stopping``, host service unreachable, roll-call offer, and session
    boundary. A stale ``enabled=true`` projection cannot bypass them — see
    ``_assert_start_allowed``.

    Slice 3 (ADR 0011 amendment) — broker-activity publisher start. After
    a successful start the broker-activity publisher is registered for
    the running instance. Failure to bootstrap (broker disconnected,
    envelope not yet visible) is logged but does NOT roll back the
    start: the lazy ``_ensure_publisher`` fallback in
    ``broker_activity.py`` re-attempts on the cockpit's first hit.
    """
    try:
        run_id = _validate_path_segment(run_id, field="run_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    settings = get_settings()
    root = Path(settings.live_runs_root)
    await _assert_start_allowed(run_id, body, settings)
    sid = _strategy_instance_id_for_run(root, run_id)
    previous_start_intent: DesiredStateRecord | None = None
    if sid is not None:
        previous_start_intent = _persist_start_intent(root, sid)
    try:
        result = await host_daemon_client.start_run(
            settings.live_runner_daemon_url,
            run_id,
            body.model_dump(exclude={"roll_call_offer_id"}),
        )
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("start_run", exc)
    except host_daemon_client.HostDaemonError as exc:
        if sid is not None:
            _restore_start_intent(root, sid, previous_start_intent)
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    response = _parse_action_response(result)
    if sid is not None and not response.accepted:
        _restore_start_intent(root, sid, previous_start_intent)
    await _maybe_start_broker_activity_publisher(response)
    sid = sid or response.process.strategy_instance_id
    if sid is not None:
        if response.accepted:
            if body.roll_call_offer_id is not None:
                bot_roll_call_offer_repo(root, sid).consume(body.roll_call_offer_id)
            _bot_lifecycle_state_repo(root, sid).set_phase(
                BotLifecyclePhase.ON_DUTY,
                now_ms=_now_ms(),
                updated_by="system",
                active_run_id=run_id,
                reason="start_accepted",
            )
        receipt, warnings = await _mutation_rung_receipts_from_process(
            sid,
            root,
            settings,
            response.process.model_dump(mode="json"),
            mutation_key="start",
        )
        response = response.model_copy(
            update={
                "rung_receipt": receipt,
                "rung_receipt_warnings": warnings,
            }
        )
        await _ensure_surface_hub_started(sid)
    return response


async def _maybe_start_broker_activity_publisher(
    response: HostRunnerActionResponse,
) -> None:
    """Best-effort deploy-time bootstrap of the broker-activity publisher.

    Slice 3 / ADR 0011 amendment. Called after a successful
    ``start_run`` so the publisher is up before the cockpit's first hit
    on the Activity tab — which both surfaces the reconnect-sweep state
    sooner and ensures the submission halt fires for orders placed in
    the first few seconds of a fresh run.

    The hook is fail-open: a bootstrap failure (envelope not yet
    visible, broker disconnected, etc.) is logged at WARNING and the
    start response is returned unchanged. The lazy fallback in
    ``broker_activity.py::_ensure_publisher`` recovers when the cockpit
    arrives.
    """
    if not response.accepted:
        return
    sid = response.process.strategy_instance_id
    if not sid:
        return
    # Local import keeps the live-instances router free of a top-level
    # dep on the broker-activity router (which imports the broker
    # singleton). The full import graph is tolerated; the per-call cost
    # is module-level cache after the first invocation.
    from app.routers.broker_activity import (
        PublisherBootstrapError,
        bootstrap_publisher_for_instance,
    )

    try:
        await bootstrap_publisher_for_instance(sid)
    except PublisherBootstrapError as exc:
        logger.warning(
            "deploy-time broker-activity publisher bootstrap deferred (%s): %s",
            exc.code,
            exc.detail,
            extra={
                "strategy_instance_id": sid,
                "bootstrap_error_code": exc.code,
            },
        )


@router.post("/runs/{run_id}/stop", response_model=HostRunnerActionResponse)
async def stop_run(run_id: str, body: HostRunnerStopRequest) -> HostRunnerActionResponse:
    """Stop the host runner for ``run_id`` by forwarding to the daemon (ADR 0007).

    Same token-forwarding rationale as :func:`start_run`.
    """
    try:
        run_id = _validate_path_segment(run_id, field="run_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    settings = get_settings()
    try:
        result = await host_daemon_client.stop_run(settings.live_runner_daemon_url, run_id, body.model_dump())
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("stop_run", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    response = _parse_action_response(result)
    root = Path(settings.live_runs_root)
    sid = _strategy_instance_id_for_run(root, run_id) or response.process.strategy_instance_id
    if sid is not None:
        if response.accepted:
            _bot_lifecycle_state_repo(root, sid).set_phase(
                BotLifecyclePhase.OFF_DUTY,
                now_ms=_now_ms(),
                updated_by="system",
                active_run_id=None,
                reason="stop_accepted",
            )
        receipt, warnings = await _mutation_rung_receipts_from_process(
            sid,
            root,
            settings,
            response.process.model_dump(mode="json"),
            mutation_key="stop",
        )
        response = response.model_copy(
            update={
                "rung_receipt": receipt,
                "rung_receipt_warnings": warnings,
            }
        )
    return response


@router.post("/{strategy_instance_id}/end-day-now", response_model=HostRunnerActionResponse)
async def end_day_now(
    strategy_instance_id: str,
    body: HostRunnerStopRequest | None = None,
) -> HostRunnerActionResponse:
    """Instance-addressed clean exit request for the daily lifecycle toolbar."""

    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)
    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    process, live_binding = _interpret_daemon_process(daemon, root)
    if live_binding is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "NO_LIVE_BINDING",
                "message": "No bot process is running for this instance.",
                "process_state": process.state,
            },
        )

    request = body or HostRunnerStopRequest()
    try:
        result = await host_daemon_client.stop_run(
            settings.live_runner_daemon_url,
            live_binding.run_id,
            request.model_dump(),
        )
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("end_day_now", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    response = _parse_action_response(result)
    if response.accepted:
        _bot_lifecycle_state_repo(root, sid).set_phase(
            BotLifecyclePhase.OFF_DUTY,
            now_ms=_now_ms(),
            updated_by="operator",
            active_run_id=None,
            reason="end_day_now",
        )
    receipt, warnings = await _mutation_rung_receipts_from_process(
        sid,
        root,
        settings,
        response.process.model_dump(mode="json"),
        mutation_key="stop",
    )
    return response.model_copy(
        update={
            "rung_receipt": receipt,
            "rung_receipt_warnings": warnings,
        }
    )


@router.post("/{strategy_instance_id}/lifecycle/roster", response_model=BotLifecycleMutationResponse)
async def set_lifecycle_roster(
    strategy_instance_id: str,
    body: BotLifecycleRosterRequest,
) -> BotLifecycleMutationResponse:
    """Add/remove a bot from the duty roster."""

    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)
    _bot_lifecycle_state_repo(root, sid).set_roster(
        body.on_roster,
        now_ms=_now_ms(),
        updated_by=body.updated_by,
        reason=body.reason,
    )
    return await _daily_lifecycle_mutation_response(sid, root, settings)


@router.post("/{strategy_instance_id}/retire-and-replace", response_model=BotLifecycleMutationResponse)
async def retire_and_replace(
    strategy_instance_id: str,
    body: BotRetireReplaceRequest,
) -> BotLifecycleMutationResponse:
    """Retire this bot's machinery before the UI continues to replacement deploy."""

    sid = _validate_instance_id(strategy_instance_id)
    if not body.confirm_account_flat:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_FLAT_ATTESTATION_REQUIRED",
                "message": "Confirm the broker account is flat before retiring and replacing this bot.",
            },
        )
    settings = get_settings()
    root = Path(settings.live_runs_root)
    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    process, _live_binding = _interpret_daemon_process(daemon, root)
    if process.state == "unreachable":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "HOST_SERVICE_OFFLINE",
                "message": "The bot service is unreachable. Reconnect it before retiring and replacing this bot.",
                "process_state": process.state,
            },
        )
    if process.state in {"running", "stopping"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "BOT_ON_DUTY",
                "message": "End the day before retiring and replacing this bot.",
                "process_state": process.state,
            },
        )
    _bot_lifecycle_state_repo(root, sid).retire(
        now_ms=_now_ms(),
        updated_by=body.updated_by,
        reason=body.reason,
    )
    return await _daily_lifecycle_mutation_response(sid, root, settings)


def _instance_last_exit(runs: list[dict]) -> InstanceLastExit | None:
    """Why the instance's newest run ended — for the console's STOPPED-reason
    surface. Returns None while a run is still live (no terminal ``ended_at_ms``)
    or when nothing was ever deployed.

    Reads the run's ``run_status.json`` for exit code/reason, and the
    indicator-state hydration receipt (``indicator_state_hydration.json``) when
    present so a cold-start rejection (``accepted=false`` /
    ``failure_reason="missing"``) is explained rather than left as a bare exit 4.
    """
    if not runs:
        return None
    latest = runs[0]
    run_dir = Path(latest["run_dir"])
    sidecar = _read_sidecar(run_dir)
    # Only surface a *terminated* run. A live run has ended_at_ms=None; showing a
    # stale exit for it would contradict the RUNNING badge.
    if sidecar is None or sidecar.ended_at_ms is None:
        return None

    hydration_accepted: bool | None = None
    hydration_failure_reason: str | None = None
    receipt_path = run_dir / "indicator_state_hydration.json"
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = None
        if isinstance(receipt, dict):
            # Type-guard before handing to the Pydantic model: a hand-edited or
            # corrupt receipt with a non-bool ``accepted`` would raise a
            # ValidationError and 500 the whole status endpoint. A bad receipt
            # should degrade to "unknown", not break status.
            accepted = receipt.get("accepted")
            if isinstance(accepted, bool):
                hydration_accepted = accepted
            validation = receipt.get("validation")
            if isinstance(validation, dict):
                reason = validation.get("failure_reason")
                if isinstance(reason, str):
                    hydration_failure_reason = reason

    # The specific safety trigger, when the run left a poisoned.flag (written on
    # both a fatal_halt and an operator MARK_POISONED). A corrupt flag degrades to
    # no detail rather than 500-ing status — the poison_sentinel gate still flags
    # the run as unsafe.
    halt_trigger: str | None = None
    halt_at_ms: int | None = None
    halt_detail: dict | None = None
    try:
        poison = read_poisoned_flag(run_dir)
    except ValueError as exc:
        # A corrupt poisoned.flag must not 500 the status call, but it is a
        # forensic signal during incident response — surface it, don't swallow.
        logger.warning(
            "Invalid poisoned.flag for run %s (%s): %s",
            sidecar.run_id,
            run_dir,
            exc,
        )
        poison = None
    if poison is not None:
        halt_trigger = poison.trigger.value
        halt_at_ms = poison.halted_at_ms
        halt_detail = dict(poison.details)

    return InstanceLastExit(
        run_id=sidecar.run_id,
        ended_at_ms=sidecar.ended_at_ms,
        exit_code=sidecar.exit_code,
        exit_reason=sidecar.exit_reason,
        exit_error_code=sidecar.exit_error_code,
        exit_error_message=sidecar.exit_error_message,
        exit_error_detail=sidecar.exit_error_detail,
        hydration_accepted=hydration_accepted,
        hydration_failure_reason=hydration_failure_reason,
        halt_trigger=halt_trigger,
        halt_at_ms=halt_at_ms,
        halt_detail=halt_detail,
    )


@router.get("/audit-copy-sizing-lookup", response_model=AuditCopySizingLookup)
async def get_audit_copy_sizing_lookup(
    audit_copy_path: str,
    proposed_sizing: str | None = None,
) -> AuditCopySizingLookup:
    """ADR 0009 § 3 — proxy the daemon's Reference parity gate to the cockpit.

    The deploy form calls this on (1) initial audit-copy pick (no
    ``proposed_sizing``, to learn the registered rule) and (2) on the
    Reference parity preset click (with ``proposed_sizing``). The daemon
    returns one of three verdicts (proven_match / proven_mismatch /
    cannot_prove); we propagate it verbatim.

    Fails closed when the daemon is unreachable — the response carries
    ``cannot_prove`` so the deploy form's gate banner reads "Reference
    parity unavailable" rather than silently enabling a preset that the
    operator believes is gated.
    """
    import json as _json

    settings = get_settings()
    sizing_payload: dict | None = None
    if proposed_sizing:
        try:
            parsed = _json.loads(proposed_sizing)
        except _json.JSONDecodeError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"proposed_sizing must be JSON: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail="proposed_sizing must be a JSON object",
            )
        sizing_payload = parsed
    _result, body = await host_daemon_client.fetch_audit_copy_sizing_lookup(
        settings.live_runner_daemon_url, audit_copy_path, sizing_payload
    )
    if body is None:
        return AuditCopySizingLookup(
            verdict="cannot_prove",
            detail="Reference parity gate unavailable: host daemon unreachable",
        )
    try:
        return AuditCopySizingLookup.model_validate(body)
    except ValidationError as exc:
        logger.warning("invalid audit-copy-sizing-lookup payload from daemon: %s", exc)
        return AuditCopySizingLookup(
            verdict="cannot_prove",
            detail=f"Reference parity gate unavailable: {exc}",
        )


@router.get("/qc-audit-copies", response_model=QcAuditCopyListing)
async def get_qc_audit_copies() -> QcAuditCopyListing:
    """List committed QC audit copies for the deploy form's picker (ADR 0006).

    Passthrough to the daemon (only the host sees ``references/qc-shadow``).
    Fails closed: an unreachable daemon yields an empty listing — the deploy
    form's connectivity strip is what surfaces "daemon down", not this endpoint.
    """
    settings = get_settings()
    _result, listing = await host_daemon_client.fetch_qc_audit_copies(settings.live_runner_daemon_url)
    if listing is None:
        return QcAuditCopyListing(scope_root="references/qc-shadow", entries=[])
    try:
        return QcAuditCopyListing.model_validate(listing)
    except ValidationError as exc:
        # A schema-invalid payload is an upstream contract failure, distinct from
        # an unreachable daemon (which fails closed to an empty listing above).
        # Surface it as a gateway error rather than 500 or a silently-empty list
        # that would read as "no committed QC copies".
        logger.warning("invalid qc-audit-copy listing from host daemon: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="host daemon returned an invalid QC audit-copy listing",
        ) from exc


@router.get("/daemon-diagnose", response_model=DaemonDiagnosticReport)
async def get_daemon_diagnostics() -> DaemonDiagnosticReport:
    """Backend-authored daemon diagnostics report.

    Unlike ``/daemon-health``, this endpoint always returns HTTP 200 with the
    failure explained inside the report body. It composes a fresh daemon probe,
    the process registry, the broker session mirror, and the folded connectivity
    monitor state.
    """
    return await get_daemon_diagnostics_service().report()


@router.get("/{strategy_instance_id}/daemon-diagnose", response_model=DaemonDiagnosticReport)
async def get_instance_daemon_diagnostics(
    strategy_instance_id: str,
) -> DaemonDiagnosticReport:
    """Project the daemon diagnostics report to one strategy instance."""
    report = await get_daemon_diagnostics_service().report(strategy_instance_id=strategy_instance_id)
    return project_daemon_diagnostic_report(report, strategy_instance_id)


@router.get("/daemon-health", response_model=HostRunnerHealth)
async def get_daemon_health() -> HostRunnerHealth:
    """Authenticated /health probe forwarded from the daemon (PRD #619-C P2).

    The browser cannot hit the daemon's /health directly any more because
    every daemon route now requires ``X-Live-Runner-Token`` (host_daemon.py
    docstring; ADR 0007: "the browser must never hold that shared secret").
    The data plane holds the token via the artifacts bind mount, so this
    route is the cockpit / deploy form's path to "is the daemon up?".

    Maps the typed daemon result to HTTP status so the frontend's existing
    resource error/value handling does the right thing without learning a
    new envelope:

    - CONNECTED   → 200 + HostRunnerHealth body (the deploy form reads
                    ``ok``, ``git_sha``, ``commits_behind``, …)
    - AUTH_FAILED → 502 ("daemon rejected our token")
    - UNREACHABLE → 503 (daemon process down or network error)
    - any other   → 502 (protocol / contract mismatch)
    """
    settings = get_settings()
    result, health = await host_daemon_client.fetch_health(settings.live_runner_daemon_url)
    if health is not None:
        return redact_host_runner_health(health)
    if result.kind == "AUTH_FAILED":
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="host daemon rejected the data plane's token",
        )
    if result.kind == "UNREACHABLE":
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=result.detail or "host daemon unreachable",
        )
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        detail=result.detail or f"host daemon returned {result.kind}",
    )


@router.post("/daemon-health/renew-lease", response_model=HostRunnerHealth)
async def renew_daemon_lease() -> HostRunnerHealth:
    """Ask the host daemon to write a fresh control-plane lease now.

    This is the cockpit recovery action for
    ``runtime.control_plane_lease_stale``. The data plane forwards the
    authenticated request so the browser never holds the daemon token.
    """
    settings = get_settings()
    try:
        result = await host_daemon_client.renew_control_plane_lease(settings.live_runner_daemon_url)
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("renew_daemon_lease", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    try:
        return redact_host_runner_health(HostRunnerHealth.model_validate(result))
    except ValidationError as exc:
        logger.warning("invalid renew-lease payload from host daemon: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail="host daemon returned an invalid renew-lease envelope",
        ) from exc


def _instance_ledger_account_id(
    root: Path,
    sid: str,
    *,
    runs_by_instance: dict[str, list[dict]] | None = None,
) -> str | None:
    """Latest ledger ``account_id`` for ``sid`` (``None`` when no ledger
    or the ledger pre-dates the field).  Pure read; used by the fleet
    account-identity aggregation."""
    if runs_by_instance is None:
        runs_by_instance = _scan_runs_by_instance(root)
    runs = runs_by_instance.get(sid, [])
    if not runs:
        return None
    try:
        ledger = _read_ledger(Path(runs[0]["run_dir"]))
    except (OSError, json.JSONDecodeError):
        return None
    value = ledger.get("account_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


async def _fetch_broker_connected_account(
    snapshot: BrokerRuntimeSnapshot | None = None,
) -> tuple[str | None, bool]:
    """Return ``(connected_account_id, known)``.

    ``known`` distinguishes "queried and got a value or definitive
    absence" from "could not query at all" (broker not wired) so the
    fleet account summary surfaces ``BROKER_ACCOUNT_UNAVAILABLE``
    only when honest.

    PRD #619-A: routed through the typed ``BrokerRuntimeSnapshot`` so
    the read uses public ``IbkrClient`` API only (``connected_account``
    property). The previous ``getattr(client, "account_id", None)``
    read a field that does not exist on the real client and silently
    degraded every call to ``known=False``.
    """
    snapshot = snapshot if snapshot is not None else snapshot_data_plane_broker()
    if not snapshot.client_available or not snapshot.connected:
        return None, False
    account = snapshot.connected_account
    if isinstance(account, str) and account.strip():
        return account.strip(), True
    return None, True


async def _compute_account_fleet_contamination(
    settings: IbkrSettings,
    root: Path,
) -> FleetContamination:
    return await fleet_contamination_service.compute_account_fleet_contamination(
        settings,
        root,
        fetch_positions=_fetch_net_positions,
    )


@router.get("/account", response_model=FleetContamination)
async def get_account_fleet() -> FleetContamination:
    """Account/fleet contamination: net account position vs the sum of every
    managed instance's namespace-attributed expected position (ADR 0005, #399).

    Retained as the legacy contamination-only endpoint.  PRD #616
    introduced ``GET /api/live-instances/account-summary`` which
    composes contamination with account identity into a single DTO.
    """
    settings = get_settings()
    root = Path(settings.live_runs_root)
    return await _compute_account_fleet_contamination(settings, root)


@router.get("/account-summary", response_model=FleetAccountSummary)
async def get_account_summary() -> FleetAccountSummary:
    """PRD #616 — server-authored account row.

    Composes position contamination with account-identity verification
    so the cockpit renders the account block from one DTO.  Account
    identity is separate from contamination: a CONFLICTING identity
    does not imply contamination, and vice versa.
    """
    settings = get_settings()
    root = Path(settings.live_runs_root)
    account_ids: dict[str, str | None] = {}
    for sid in _scan_runs_by_instance(root):
        account_ids[sid] = _instance_ledger_account_id(root, sid)
    net = await _fetch_net_positions()
    data_plane_snapshot = snapshot_data_plane_broker()
    broker_account, broker_known = await _fetch_broker_connected_account(data_plane_snapshot)
    payload = compute_fleet_account_summary(
        net_positions=net,
        explained_by_instance=collect_fleet_position_explanations(root),
        instance_account_ids=account_ids,
        broker_connected_account=broker_account,
        broker_account_known=broker_known,
        policy_blocks_starts=settings.fleet_dirty_blocks_starts,
    )
    payload["contamination"] = FleetContamination(**payload["contamination"])
    return FleetAccountSummary(**payload)


def _surface_snapshot_unavailable(strategy_instance_id: str) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "reason_code": "SURFACE_SNAPSHOT_UNAVAILABLE",
            "message": "The bot surface producer has not completed a successful refresh yet.",
            "strategy_instance_id": strategy_instance_id,
        },
    )


@router.get("/{strategy_instance_id}/status", response_model=LiveInstanceStatus)
async def get_instance_status(
    strategy_instance_id: str,
    refresh: bool = Query(default=False),
) -> LiveInstanceStatus:
    """Return the snapshot stored by this bot's producer-owned hub."""
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)
    if sid not in _visible_runs_by_instance(root) and _sid_has_soft_deletion_from_directory(
        root.parent,
        sid,
    ):
        raise HTTPException(
            status.HTTP_410_GONE,
            detail=_bot_soft_deleted_detail(sid),
        )
    hub = _SURFACE_HUBS.get(sid)
    if hub is None and refresh:
        hub = _surface_hub_for(sid)
    if hub is None:
        raise _surface_snapshot_unavailable(sid)
    try:
        return await hub.snapshot(refresh=refresh)
    except SnapshotUnavailableError as exc:
        raise _surface_snapshot_unavailable(sid) from exc


@router.post(
    "/{strategy_instance_id}/crash-recovery-override",
    response_model=CrashRecoveryOverrideResponse,
)
async def record_crash_recovery_override(
    strategy_instance_id: str,
    body: CrashRecoveryOverrideRequest,
) -> CrashRecoveryOverrideResponse:
    """Record audited evidence allowing a crash-retired runner to restart."""

    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)
    runs = _scan_runs_by_instance(root).get(sid, [])
    if not runs:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={
                "reason_code": "INSTANCE_RUN_NOT_FOUND",
                "message": f"No run directory was found for {sid}. Deploy before recording recovery evidence.",
            },
        )
    account_id = _run_dir_account_id(Path(runs[0]["run_dir"]))
    if account_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "ACCOUNT_ID_UNAVAILABLE",
                "message": "The latest run ledger does not contain an account_id.",
                "remediation": "Redeploy with broker account evidence before recording crash recovery.",
            },
        )
    try:
        override = record_crash_recovery_override_evidence(
            root.parent,
            account_id=account_id,
            strategy_instance_id=sid,
            request=body,
            now_ms=_now_ms(),
        )
    except CrashRecoveryNotRequiredError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "CRASH_RECOVERY_NOT_REQUIRED",
                "message": "This bot is not blocked by crash-retired recovery proof.",
                "remediation": "Refresh Bot Control and use the currently enabled action.",
            },
        ) from exc
    # The audited override is already durably recorded above. The receipt is a
    # convenience projection off the fresh ladder; if resolving it fails (daemon
    # unreachable, artifact read error) the mutation still succeeded, so degrade
    # to a receipt-less 200 rather than 500-ing a request whose retry would hit
    # CrashRecoveryNotRequiredError → 409 and never report success.
    try:
        receipt, warnings = await _mutation_rung_receipts_for_instance(
            sid,
            root,
            settings,
            mutation_key="crash_recovery_override",
        )
    except Exception as exc:
        # Post-commit projection must not mask the durable write.
        logger.warning(
            "crash-recovery override recorded, but post-commit receipt resolution failed",
            extra={"strategy_instance_id": sid, "override_id": override.override_id, "exception": repr(exc)},
        )
        return override
    return override.model_copy(
        update={
            "rung_receipt": receipt,
            "rung_receipt_warnings": warnings,
        }
    )


def _resolve_safety_verdict_final(
    configured_mode: Literal["paper", "live"] | None,
) -> Literal["paper-only", "unsafe", "unknown"]:
    """PRD #616 — derive ADR-0011's reactive ``BrokerSafetyVerdict.final_verdict``
    for the operator-surface projection.

    PRD #619-A: routed through the typed ``BrokerRuntimeSnapshot`` so
    the read uses only public ``IbkrClient`` API and a missing/torn-down
    singleton surfaces as a structured ``client_available=False``
    snapshot rather than being swallowed by a broad ``except Exception``.
    Per the ADR-0011 amendment, ``readonly_flag`` is no longer part of
    the identity derivation; the snapshot still carries it for the
    per-gate breakdown.

    The fleet-level singleton is the *data-plane* observation; for live
    runs the authoritative verdict comes from the child's own
    ``verdict_snapshot.json`` (see PRD #619-A §A3 — the engine writes
    that file via its ``verdict_provider`` closure).
    """
    from app.broker.runtime_snapshot import snapshot_data_plane_broker
    from app.broker.safety_verdict import derive_broker_safety_verdict

    snapshot = snapshot_data_plane_broker()
    # When the singleton is unavailable (broker disabled, lifespan has
    # not constructed it, or it has been torn down) the snapshot fields
    # are all ``None``. Feeding those Nones to the pure derivation yields
    # an honest ``unknown`` driven by the cockpit's own ``configured_mode``.
    verdict = derive_broker_safety_verdict(
        configured_mode=configured_mode,
        readonly_flag=snapshot.readonly,
        port=snapshot.port,
        connected_account=snapshot.connected_account,
    )
    return verdict.final_verdict


def _resolve_resume_guard_state_for(
    root: Path,
    live_binding: LiveBinding | None,
    runs: list[dict],
) -> ResumeGuardState:
    """Resolve the canonical ``ResumeGuardState`` for an instance.

    The bound run's artifacts are the truth; absent a binding the
    most recent evidence run is consulted (so an EXITED instance
    still surfaces its last verdict / WAL state).  When no run
    exists at all, ``empty_guard_state()`` is returned (nothing to
    safeguard yet).
    """
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return empty_guard_state()
    return resolve_guard_state_from_paths(
        verdict_snapshot_path=run_dir / "verdict_snapshot.json",
        run_status_path=run_dir / "run_status.json",
        run_dir_for_reconciliation=run_dir,
        intent_wal_path=run_dir / "intent_events.jsonl",
    )


async def _load_instance_context_for_router(sid: str) -> InstanceContext:
    """PRD #619-A §A4 — single-call assembly for the mutation endpoints.

    Wires the pure ``load_instance_context`` service to this router's
    helpers / settings. Each mutation endpoint calls this once for its
    pre-write capability gate; the post-write daemon revalidation is a
    *separate* fetch (the durable write may move daemon-side state).
    """
    settings = get_settings()
    root = Path(settings.live_runs_root)

    async def _fetch_daemon(_sid: str) -> dict | None:
        # C2: typed read returns ``(DaemonResult, dict | None)``; the
        # operator-surface ``control_plane`` section (C3) surfaces the
        # typed kind via the connectivity monitor's accumulated state, so
        # this per-call result is discarded here.
        _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, _sid)
        return daemon

    return await load_instance_context(
        sid,
        now_ms=_now_ms,
        fetch_daemon_process=_fetch_daemon,
        interpret_daemon_process=lambda daemon: _interpret_daemon_process(daemon, root),
        scan_runs_for_instance=lambda _sid: _scan_runs_by_instance(root).get(_sid, []),
        resolve_desired_state=lambda _sid: _resolve_desired_state(root, _sid),
        instance_last_exit=_instance_last_exit,
        instance_broker=lambda _sid: _instance_broker(root, _sid),
        resolve_guard_state_for=lambda live_binding, runs: _resolve_resume_guard_state_for(root, live_binding, runs),
        resolve_runtime_freshness_for=lambda live_binding, _runs, observed_at_ms: _resolve_runtime_freshness(
            root,
            live_binding,
            now_ms=observed_at_ms,
        ),
        resolve_latest_mutation_for=lambda _sid: _resolve_latest_mutation(root, _sid),
    )


# PRD #619-C5 — single-shot mutation OUTCOME_UNKNOWN surfacing.

_OUTCOME_UNKNOWN_RUNBOOK_HINTS: dict[str, str] = {
    "deploy": (
        "A deploy request was sent to the host runner daemon but the response "
        "was lost. The run may or may not have been created. Refresh the "
        "instance list and re-run with the same content-addressed run_id "
        "(deploy is idempotent on run_id) only after verifying the daemon's "
        "actual state."
    ),
    "start_run": (
        "A start request was sent to the host runner daemon but the response "
        "was lost. The run may or may not be running. Refresh the cockpit "
        "to read live state before deciding whether to retry."
    ),
    "stop_run": (
        "A stop request was sent to the host runner daemon but the response "
        "was lost. The run may or may not have stopped. Refresh the cockpit "
        "to read live state before deciding whether to retry."
    ),
    "emergency_flatten": (
        "An emergency-flatten request was sent to the host runner daemon "
        "but the response was lost. Broker positions may be in an "
        "intermediate state. Verify positions directly via the broker "
        "before deciding whether to retry."
    ),
    "renew_daemon_lease": (
        "A control-plane lease renewal request was sent to the host runner "
        "daemon but the response was lost. Refresh the cockpit to read the "
        "latest control-plane state before deciding whether to retry."
    ),
}


def _raise_outcome_unknown(
    endpoint: Literal[
        "deploy",
        "start_run",
        "stop_run",
        "emergency_flatten",
        "renew_daemon_lease",
    ],
    exc: host_daemon_client.HostDaemonOutcomeUnknownError,
) -> NoReturn:
    """Surface an ambiguous-outcome mutation failure as a typed 409 (PRD #619-C5).

    The body is :class:`MutationOutcomeUnknownResponse`; the cockpit
    renders the runbook hint verbatim and tells the operator to refresh
    state before retrying. Distinct from 503 ``host daemon unreachable``,
    which means the request was provably not sent.
    """
    body = MutationOutcomeUnknownResponse(
        error_category=exc.error_category,
        detail=exc.detail,
        endpoint=endpoint,
        occurred_at_ms=_now_ms(),
        runbook_hint=_OUTCOME_UNKNOWN_RUNBOOK_HINTS[endpoint],
    )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=body.model_dump(mode="json"),
    ) from exc


def _broker_connection_state_from_readiness(
    readiness: ReadinessVector | None,
) -> Literal["connected", "disconnected", "unknown"] | None:
    """Collapse the live readiness ``broker_connection`` gate into the
    operator-surface broker-connection-state enum.

    The live readiness vector today only emits pass/fail on
    ``broker_connection`` (see ``app/engine/live/readiness.py``).  When a
    richer ``BrokerConnectionState`` channel lands on the wire, this
    helper grows to read it; degraded transport states are unreachable from the
    current pass/fail signal, which is the honest answer.
    """
    if readiness is None:
        return None
    for gate in readiness.gates:
        if gate.name == "broker_connection":
            if gate.status == "pass":
                return "connected"
            if gate.status == "fail":
                return "disconnected"
            return "unknown"
    return None


def _read_parquet_rows(path: Path, since_ms: int | None = None, key: str = "ts_ms") -> list[dict]:
    """Read a Parquet artifact's rows; optionally filter by a ms cursor.

    A missing file legitimately returns ``[]`` (run had no trades yet). A
    read failure logs with context and still returns ``[]`` rather than
    500-ing the chart — but the warning makes corruption visible during
    incident response (PR #483 review).
    """
    rows = read_parquet_rows(path, on_error="warn_empty")
    if since_ms is not None:
        rows = [r for r in rows if int(r.get(key, 0)) > since_ms]
    return rows


def _filter_rows_to_utc_day(rows: list[dict], day: date, key: str = "ts_ms") -> list[dict]:
    """Keep only rows whose ``key`` (int64 ms UTC) falls within ``day``.

    Used by ``/chart-snapshot`` so a multi-day run doesn't project the
    other days' markers onto every per-date response (PR #483 review).
    """
    day_start_ms = int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
    day_end_ms = day_start_ms + 86_400_000
    return [r for r in rows if day_start_ms <= int(r.get(key, -1)) < day_end_ms]


def _filter_rows_to_window(rows: list[dict], start_ms: int, end_ms: int, key: str = "ts_ms") -> list[dict]:
    """Keep only rows whose ``key`` (int64 ms UTC) falls in ``[start_ms, end_ms)``."""
    return [r for r in rows if start_ms <= int(r.get(key, -1)) < end_ms]


def _runs_active_on(runs: list[dict], day: date, *, live_binding: LiveBinding | None) -> list[dict]:
    """Subset of ``runs`` whose session overlaps the given UTC date.

    A run "touches" the day when its ``started_at_ms`` ≤ end-of-day AND its
    ``ended_at_ms`` (or now) ≥ start-of-day. Live binding's run is always
    considered active on today regardless of sidecar contents.
    """
    day_start_ms = int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)
    day_end_ms = day_start_ms + 86_400_000
    out: list[dict] = []
    for run in runs:
        run_dir = Path(run["run_dir"])
        sidecar = _read_sidecar(run_dir)
        started = sidecar.started_at_ms if sidecar is not None else None
        ended = sidecar.ended_at_ms if sidecar is not None else None
        if started is None:
            # Fall back to created_at_ms when no sidecar yet.
            started = int(run.get("created_at_ms") or 0)
        effective_end = ended if ended is not None else day_end_ms
        if started < day_end_ms and effective_end >= day_start_ms:
            out.append({**run, "started_at_ms": started, "ended_at_ms": ended, "sidecar_started": sidecar is not None})
        elif live_binding is not None and run.get("run_id") == live_binding.run_id and day == _today_ny():
            out.append({**run, "started_at_ms": started, "ended_at_ms": None, "sidecar_started": False})
    return out


def _runs_active_in_window(
    runs: list[dict],
    start_ms: int,
    end_ms: int,
    *,
    live_binding: LiveBinding | None,
    now_ms: int,
) -> list[dict]:
    """Subset of ``runs`` whose lifecycle overlaps an explicit UTC-ms window."""
    out: list[dict] = []
    for run in runs:
        run_dir = Path(run["run_dir"])
        sidecar = _read_sidecar(run_dir)
        started = sidecar.started_at_ms if sidecar is not None else None
        ended = sidecar.ended_at_ms if sidecar is not None else None
        if started is None:
            started = int(run.get("created_at_ms") or 0)
        effective_end = ended if ended is not None else now_ms
        if started < end_ms and effective_end >= start_ms:
            out.append({**run, "started_at_ms": started, "ended_at_ms": ended, "sidecar_started": sidecar is not None})
        elif live_binding is not None and run.get("run_id") == live_binding.run_id and start_ms <= now_ms < end_ms:
            out.append({**run, "started_at_ms": started, "ended_at_ms": None, "sidecar_started": False})
    return out


# VCR-P3-I — Trading-day boundaries are America/New_York, not UTC. At
# the UTC boundary (00:00 UTC = 19:00 ET in winter / 20:00 ET in summer)
# bars from the ET trading session could fall on the wrong UTC date,
# making the chart-snapshot "today" view miss bars or show yesterday's
# bars under today's banner. The chart-snapshot is the only consumer in
# this module; everything else operates on bar-time milliseconds where
# the timezone is already explicit.
_ACTIVITY_EVIDENCE_BACKFILL_LIMIT = 10_000


def _today_ny() -> date:
    """Today as America/New_York date — the trading-day partition key.

    VCR-P3-I: at the UTC boundary, bars from the ET trading session
    can fall on the wrong UTC date. The chart-snapshot endpoint reads
    by trading day, so the "today" reference must be the NY trading
    date, not the UTC calendar date.
    """
    return datetime.now(_NY_TZ).date()


def _ny_session_bounds_ms(day: date) -> tuple[int, int]:
    """Return America/New_York calendar-day bounds as UTC ms.

    The Activity tab's selected date is the exchange/session date, not a
    UTC bucket. This includes PRE/RTH/POST/CLOSED broker events whose
    wall-clock belongs to that NY date.
    """
    return ny_session_bounds_ms(day)


def _activity_evidence_refs_for_session(
    *,
    sid: str,
    symbol: str | None,
    start_ms: int,
    end_ms: int,
) -> list[ActivityEvidenceRef]:
    refs: list[ActivityEvidenceRef] = []
    events = get_ibkr_api_evidence_recorder().backfill(
        after_seq=0,
        limit=_ACTIVITY_EVIDENCE_BACKFILL_LIMIT,
    )
    for event in events:
        if not (start_ms <= event.ts_ms < end_ms):
            continue
        if event.strategy_instance_id not in (None, sid):
            continue
        if symbol is not None and event.symbol not in (None, symbol):
            continue
        refs.append(activity_evidence_ref_from_event(event))
    return refs


def _activity_row_time_ms(row) -> int:
    return int(row.exec_ts_ms or row.ts_ms)


def _activity_order_key(row) -> str:
    if row.perm_id is not None:
        return f"perm:{row.perm_id}"
    if row.order_ref:
        return f"ref:{row.order_ref}"
    if row.exec_id:
        return f"exec:{row.exec_id}"
    return f"row:{row.seq}"


def _activity_fill_key(row) -> str:
    if row.exec_id:
        return f"exec:{row.exec_id}"
    return _activity_order_key(row)


def _position_effect_for_fill(
    *, prior: float, side: Literal["BUY", "SELL"], quantity: float
) -> tuple[str, float, str | None]:
    signed = quantity if side == "BUY" else -quantity
    next_position = prior + signed

    if abs(prior) <= 1e-9 and next_position > 0:
        return "Open long", next_position, "OPEN"
    if abs(prior) <= 1e-9 and next_position < 0:
        return "Open short", next_position, "OPEN"
    if prior > 0 and signed > 0:
        return "Add long", next_position, None
    if prior < 0 and signed < 0:
        return "Add short", next_position, None
    if prior > 0 and next_position > 0:
        return "Reduce long", next_position, None
    if prior < 0 and next_position < 0:
        return "Reduce short", next_position, None
    if prior > 0 and abs(next_position) <= 1e-9:
        return "Close long", next_position, "CLOSE"
    if prior < 0 and abs(next_position) <= 1e-9:
        return "Close short", next_position, "CLOSE"
    return "Reverse", next_position, "REVERSE"


def _read_activity_wal_rows(*, artifacts_root: Path, sid: str, start_ms: int, end_ms: int) -> list:
    wal = BrokerActivityWal(
        instance_broker_activity_wal_path(artifacts_root, sid),
        trusted_root=artifacts_root / "live_instances",
    )
    rows = wal.read_all()
    return [row for row in rows if start_ms <= _activity_row_time_ms(row) < end_ms]


def _latest_activity_wal_day(*, artifacts_root: Path, sid: str) -> date | None:
    wal = BrokerActivityWal(
        instance_broker_activity_wal_path(artifacts_root, sid),
        trusted_root=artifacts_root / "live_instances",
    )
    rows = wal.read_all()
    if not rows:
        return None
    latest_ms = max(_activity_row_time_ms(row) for row in rows)
    return datetime.fromtimestamp(latest_ms / 1000, tz=UTC).astimezone(_NY_TZ).date()


def _build_activity_projection(
    *,
    sid: str,
    day: date,
    symbol: str | None,
    resolution: str,
    wal_rows: list,
    evidence_refs: list[ActivityEvidenceRef],
) -> LiveInstanceActivityProjection:
    by_fill_key: dict[str, list] = {}
    for row in wal_rows:
        if row.verdict == "engine_only_pending" or row.price is None or row.exec_ts_ms is None:
            continue
        by_fill_key.setdefault(_activity_fill_key(row), []).append(row)

    fill_markers: list[ActivityFillMarker] = []
    annotations: list[ActivityPositionAnnotation] = []
    warnings: list[ActivityReconciliationWarning] = []
    net_by_symbol: dict[str, float] = {}
    selected_fill_rows: dict[str, object] = {}

    for fill_key, fill_rows in sorted(
        by_fill_key.items(), key=lambda item: min(_activity_row_time_ms(row) for row in item[1])
    ):
        row = sorted(fill_rows, key=lambda r: (0 if r.verdict != "unexpected" else 1, r.seq))[0]
        selected_fill_rows[fill_key] = row
        prior = net_by_symbol.get(row.symbol, 0.0)
        effect, next_position, lifecycle_label = _position_effect_for_fill(
            prior=prior,
            side=row.side,
            quantity=float(row.quantity),
        )
        net_by_symbol[row.symbol] = next_position
        marker = ActivityFillMarker(
            id=fill_key,
            row_seq=row.seq,
            order_key=_activity_order_key(row),
            symbol=row.symbol,
            side=row.side,
            quantity=float(row.quantity),
            price=float(row.price),
            chart_ts_ms=int(row.ts_ms),
            exec_ts_ms=int(row.exec_ts_ms),
            position_effect=effect,
            replay_count=len(fill_rows),
            evidence=matching_evidence_refs(
                row,
                evidence_refs,
                request_calls={"placeOrder", "reqExecutionsAsync", "reqAllOpenOrders"},
            ),
        )
        fill_markers.append(marker)
        if len(fill_rows) > 1:
            warnings.append(
                ActivityReconciliationWarning(
                    code="broker_replay_collapsed",
                    message=(
                        f"Broker execution {fill_key} was observed {len(fill_rows)} times; "
                        "the chart and Orders Today count it once."
                    ),
                    row_ids=[str(r.seq) for r in fill_rows],
                )
            )
        if lifecycle_label is not None:
            annotations.append(
                ActivityPositionAnnotation(
                    id=f"{lifecycle_label.lower()}:{fill_key}",
                    ts_ms=int(row.ts_ms),
                    symbol=row.symbol,
                    label=lifecycle_label,
                    net_position=next_position,
                )
            )

    orders_today: list[ActivityOrderRow] = []
    for order_key, rows in sorted(
        _group_rows_by_order_key(wal_rows).items(),
        key=lambda item: max(_activity_row_time_ms(row) for row in item[1]),
        reverse=True,
    ):
        ordered = sorted(rows, key=_activity_row_time_ms)
        first = ordered[0]
        fill_rows = [
            row
            for row in ordered
            if row.verdict != "engine_only_pending" and row.price is not None and row.exec_ts_ms is not None
        ]
        unique_fill_rows = {_activity_fill_key(row): row for row in fill_rows}
        filled_quantity = sum(float(row.quantity) for row in unique_fill_rows.values())
        avg_fill = (
            sum(float(row.price) * float(row.quantity) for row in unique_fill_rows.values()) / filled_quantity
            if filled_quantity > 0
            else None
        )
        has_pending = any(row.verdict == "engine_only_pending" for row in ordered)
        has_terminal = bool(fill_rows) or any(
            code.value in {"cancellation", "rejection"} for row in ordered for code in row.reason_codes
        )
        group: Literal["active", "resolved", "engine_pending"]
        if has_terminal:
            group = "resolved"
            status_label = "filled" if fill_rows else "resolved"
        elif has_pending:
            group = "engine_pending"
            status_label = "engine pending"
        else:
            group = "active"
            status_label = "working"
        latest = ordered[-1]
        orders_today.append(
            ActivityOrderRow(
                order_key=order_key,
                symbol=first.symbol,
                side=first.side,
                quantity=float(first.quantity),
                order_type=first.order_type,
                status=status_label,
                group=group,
                chart_ts_ms=int(first.ts_ms),
                submitted_ts_ms=int(first.ts_ms),
                last_update_ts_ms=_activity_row_time_ms(latest),
                filled_quantity=filled_quantity,
                avg_fill_price=avg_fill,
                position_effect=(next((m.position_effect for m in fill_markers if m.order_key == order_key), None)),
                replay_count=max(1, len(fill_rows) - len(unique_fill_rows) + 1),
                evidence=matching_evidence_refs(
                    first,
                    evidence_refs,
                    request_calls={"placeOrder", "reqAllOpenOrders"},
                ),
            )
        )

    broker_events: list[ActivityBrokerEventRow] = []
    for marker in fill_markers:
        selected_row = selected_fill_rows[marker.id]
        source = (
            "activity_repair_projection"
            if selected_row.recovery_provenance == "reconstructed"
            else "broker_activity_wal"
        )
        broker_events.append(
            ActivityBrokerEventRow(
                id=marker.id,
                visible_row_id=f"fill:{marker.id}",
                ts_ms=marker.chart_ts_ms,
                row_type="fill",
                display_type="Broker fill",
                source=source,
                source_label=(
                    "Repaired trade evidence" if source == "activity_repair_projection" else "Broker activity stream"
                ),
                symbol=marker.symbol,
                side=marker.side,
                quantity=marker.quantity,
                price=marker.price,
                status=marker.position_effect,
                summary=(
                    f"{marker.side} {marker.quantity:g} {marker.symbol} @ {marker.price:.2f} · {marker.position_effect}"
                ),
                verdict=selected_row.verdict.value,
                replay_count=marker.replay_count,
                cluster_key=marker.order_key,
                cluster_label=activity_cluster_label(selected_row),
                evidence=marker.evidence,
            )
        )
    for row in wal_rows:
        if row.verdict == "engine_only_pending":
            broker_events.append(
                ActivityBrokerEventRow(
                    id=f"pending:{row.seq}",
                    visible_row_id=f"order_intent:{row.seq}",
                    ts_ms=int(row.ts_ms),
                    row_type="order_intent",
                    display_type="Order intent",
                    source="broker_activity_wal",
                    source_label="Broker activity stream",
                    symbol=row.symbol,
                    side=row.side,
                    quantity=float(row.quantity),
                    status="engine pending",
                    summary=row.headline,
                    verdict=row.verdict.value,
                    evidence=matching_evidence_refs(
                        row,
                        evidence_refs,
                        request_calls={"placeOrder"},
                    ),
                )
            )
        elif row.price is None and any(code.value in {"cancellation", "rejection"} for code in row.reason_codes):
            broker_events.append(
                ActivityBrokerEventRow(
                    id=f"terminal:{row.seq}",
                    visible_row_id=f"order_terminal:{row.seq}",
                    ts_ms=_activity_row_time_ms(row),
                    row_type="order_terminal",
                    display_type="Order terminal state",
                    source="broker_activity_wal",
                    source_label="Broker activity stream",
                    symbol=row.symbol,
                    side=row.side,
                    quantity=float(row.quantity),
                    status="/".join(code.value for code in row.reason_codes),
                    summary=row.headline,
                    verdict=row.verdict.value,
                    evidence=matching_evidence_refs(
                        row,
                        evidence_refs,
                        request_calls={"reqAllOpenOrders"},
                    ),
                )
            )
    for ref in evidence_refs:
        narrative = activity_evidence_narrative(ref)
        broker_events.append(
            ActivityBrokerEventRow(
                id=f"evidence:{ref.seq}",
                visible_row_id=f"evidence:{ref.seq}",
                ts_ms=ref.ts_ms,
                row_type="broker_evidence",
                display_type=narrative["display_type"],
                source=ref.source,
                source_label=narrative["source_label"],
                status=narrative["status"],
                summary=narrative["summary"],
                verdict="evidence",
                fold_key=narrative["fold_key"],
                evidence=[ref],
            )
        )
    broker_events = fold_activity_event_rows(sorted(broker_events, key=lambda row: row.ts_ms, reverse=True))

    if not any(ref.request_call == "reqPositionsAsync" for ref in evidence_refs):
        warnings.append(
            ActivityReconciliationWarning(
                code="broker_position_snapshot_unavailable",
                message=(
                    "No full broker positions snapshot was captured for this selected session date; "
                    "position lifecycle annotations are derived from fills and should be treated as explanatory."
                ),
            )
        )

    return LiveInstanceActivityProjection(
        strategy_instance_id=sid,
        session_date=day.isoformat(),
        symbol=symbol or "",
        resolution=resolution,
        has_bars=False,
        now_ms=_now_ms(),
        bars=[],
        fill_markers=fill_markers,
        position_annotations=annotations,
        order_overlays=[],
        orders_today=orders_today,
        broker_activity_summary=_broker_activity_summary(broker_events),
        broker_activity_rows=broker_events,
        position_snapshot=[
            ActivityPositionSnapshot(
                symbol=sym,
                quantity=qty,
                source="unavailable",
                as_of_ms=None,
            )
            for sym, qty in sorted(net_by_symbol.items())
            if abs(qty) > 1e-9
        ],
        reconciliation_warnings=warnings,
        evidence=evidence_refs,
    )


def _activity_lifecycle_consistency_warnings(
    *,
    artifacts_root: Path,
    sid: str,
    day: date,
    runs: list[dict],
    live_binding: LiveBinding | None,
    activity_rows: list,
) -> list[ActivityReconciliationWarning]:
    start_ms, end_ms = _ny_session_bounds_ms(day)
    lifecycle_refs = _lifecycle_order_refs_for_activity(
        artifacts_root=artifacts_root,
        sid=sid,
        day=day,
        runs=runs,
        live_binding=live_binding,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    activity_refs = activity_order_refs_for_session(
        activity_rows,
        start_ms=start_ms,
        end_ms=end_ms,
        row_time_ms=_activity_row_time_ms,
    )
    return compare_activity_lifecycle_refs(lifecycle_refs=lifecycle_refs, activity_refs=activity_refs)


def _lifecycle_order_refs_for_activity(
    *,
    artifacts_root: Path,
    sid: str,
    day: date,
    runs: list[dict],
    live_binding: LiveBinding | None,
    start_ms: int,
    end_ms: int,
) -> set[str]:
    refs: set[str] = set()
    live_run_id = live_binding.run_id if live_binding is not None else None
    for run in runs_active_in_window(
        runs,
        start_ms=start_ms,
        end_ms=end_ms,
        live_run_id=live_run_id,
        force_include_live_run=day == _today_ny(),
        read_sidecar=_read_sidecar,
    ):
        run_dir = run.run_dir
        run_id = run.run_id
        intent_events = _read_intent_events_for_activity(run_dir, sid=sid)
        namespace = next((event.bot_order_namespace for event in intent_events), None)
        for event in intent_events:
            ts_ms = event.appended_at_ms if event.appended_at_ms is not None else event.ts_ms
            if event.event_type in _LIFECYCLE_ACTIVITY_ORDER_EVENT_TYPES and _ts_in_window(ts_ms, start_ms, end_ms):
                refs.add(event.order_ref)

        account_id = _run_account_id(run_dir)
        for event in _project_instance_account_lifecycle_events(
            artifacts_root,
            account_id=account_id,
            sid=sid,
            run_id=run_id,
            bot_order_namespace=namespace,
        ):
            if event.category != "order" or not _ts_in_window(event.ts_ms, start_ms, end_ms):
                continue
            order_ref = _order_ref_from_lifecycle_payload(event.payload)
            if order_ref is not None:
                refs.add(order_ref)
    return refs


def _read_intent_events_for_activity(run_dir: Path, *, sid: str) -> list[IntentEvent]:
    wal_path = run_dir / "intent_events.jsonl"
    if not wal_path.exists():
        return []
    try:
        return IntentWal(wal_path).read_tail()
    except IntentWalCorruptError as exc:
        logger.warning(
            "failed to read intent WAL while checking activity consistency",
            extra={"strategy_instance_id": sid, "path": str(wal_path), "exception": repr(exc)},
        )
        return []


def _run_account_id(run_dir: Path) -> str | None:
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, json.JSONDecodeError):
        return None
    return _nonempty_str(ledger.get("account_id"))


def _ts_in_window(ts_ms: int | None, start_ms: int, end_ms: int) -> bool:
    return ts_ms is not None and start_ms <= ts_ms < end_ms


def _order_ref_from_lifecycle_payload(payload: Mapping[str, object]) -> str | None:
    order_ref = _nonempty_str(payload.get("order_ref"))
    if order_ref is not None:
        return order_ref
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, Mapping):
        return _nonempty_str(diagnostics.get("order_ref"))
    return None


def _group_rows_by_order_key(rows: list) -> dict[str, list]:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(_activity_order_key(row), []).append(row)
    return groups


class _BrokerActivitySummaryBucket(TypedDict):
    label: str
    kind: Literal["order", "heartbeat", "evidence"]
    event_count: int
    last_event_ts_ms: int | None
    row_ids: list[str]


def _broker_activity_summary(rows: list[ActivityBrokerEventRow]) -> list[ActivityBrokerCategorySummary]:
    groups: dict[str, _BrokerActivitySummaryBucket] = {}
    for row in rows:
        category_id, label, kind = _broker_activity_category(row)
        bucket = groups.setdefault(
            category_id,
            {
                "label": label,
                "kind": kind,
                "event_count": 0,
                "last_event_ts_ms": None,
                "row_ids": [],
            },
        )
        bucket["event_count"] += max(1, int(row.fold_count))
        last_event_ts_ms = bucket["last_event_ts_ms"]
        if last_event_ts_ms is None or row.ts_ms > last_event_ts_ms:
            bucket["last_event_ts_ms"] = row.ts_ms
        bucket["row_ids"].append(row.visible_row_id or row.id)

    kind_rank = {"order": 0, "heartbeat": 1, "evidence": 2}
    out = [
        ActivityBrokerCategorySummary(
            category_id=category_id,
            label=bucket["label"],
            kind=bucket["kind"],
            event_count=bucket["event_count"],
            last_event_ts_ms=bucket["last_event_ts_ms"],
            row_ids=bucket["row_ids"],
        )
        for category_id, bucket in groups.items()
    ]
    return sorted(
        out,
        key=lambda item: (
            kind_rank[item.kind],
            -(item.last_event_ts_ms or 0),
            item.label,
        ),
    )


def _broker_activity_category(
    row: ActivityBrokerEventRow,
) -> tuple[str, str, Literal["order", "heartbeat", "evidence"]]:
    if row.row_type == "fill":
        return "order_fill", "Broker fills", "order"
    if row.row_type == "order_intent":
        return "order_intent", "Order intents", "order"
    if row.row_type == "order_terminal":
        return "order_terminal", "Terminal order states", "order"
    display = row.display_type or row.row_type.replace("_", " ").title()
    text = f"{display} {row.source_label or row.source} {row.summary}".lower()
    kind: Literal["order", "heartbeat", "evidence"] = "heartbeat" if "heartbeat" in text else "evidence"
    category_id = re.sub(r"[^a-z0-9]+", "_", display.lower()).strip("_") or "broker_evidence"
    return f"evidence_{category_id}", display, kind


@router.get(
    "/{strategy_instance_id}/chart-snapshot",
    response_model=ChartSnapshotResponse,
)
async def get_chart_snapshot(
    strategy_instance_id: str,
    date_str: Annotated[str | None, Query(alias="date")] = None,
    resolution: Annotated[str, Query()] = "1m",
    timeframe: Annotated[str | None, Query()] = None,
    from_ms: Annotated[int | None, Query()] = None,
    to_ms: Annotated[int | None, Query()] = None,
) -> ChartSnapshotResponse:
    """Aggregated chart payload for one (instance, date, resolution) — Slice 5.

    Replaces the chart card's prior split between ``/bars/snapshot``,
    per-run ``/trades`` and per-run ``/executions`` calls. Returns the
    day's bars + every run of the instance that touched the day so the
    frontend renders per-run trade markers and inactive-interval shading
    without knowing how many runs exist.

    ``date`` defaults to today (UTC). A past date stops polling on the
    frontend; the absence of ``has_bars`` lets the UI surface a "bars
    unavailable" badge for pre-persistence dates (Slice 6).
    """
    sid = _validate_instance_id(strategy_instance_id)
    try:
        requested_timeframe = coerce_chart_timeframe(timeframe or resolution)
    except ChartWindowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    settings = get_settings()
    root = Path(settings.live_runs_root)
    now_ms = _now_ms()

    explicit_window = from_ms is not None or to_ms is not None
    if explicit_window:
        if from_ms is None or to_ms is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="from_ms and to_ms must be provided together")
        window_from_ms = int(from_ms)
        window_to_ms = min(int(to_ms), now_ms)
        day = datetime.fromtimestamp(window_from_ms / 1000, tz=UTC).astimezone(_NY_TZ).date()
    else:
        try:
            day = date.fromisoformat(date_str) if date_str else _today_ny()
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid date") from None
        window_from_ms, fallback_to_ms = _ny_session_bounds_ms(day)
        window_to_ms = min(fallback_to_ms, now_ms) if day == _today_ny() else fallback_to_ms

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    runs = _scan_runs_by_instance(root).get(sid, [])

    symbol = _resolve_symbol(root, live_binding, runs)

    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    # Nudge the live stream only when the requested window is at the live edge.
    live_edge_threshold_ms = 30_000 if requested_timeframe == "5s" else 180_000
    if symbol is not None and window_from_ms <= now_ms and window_to_ms >= now_ms - live_edge_threshold_ms:
        try:
            if requested_timeframe == "5s":
                await LIVE_BAR_AGGREGATOR.ensure_subscribed_5s(symbol)
            else:
                await LIVE_BAR_AGGREGATOR.ensure_subscribed(symbol)
        except Exception as exc:
            # The stream may legitimately be unavailable (broker offline,
            # subsystem disabled). Surface as a log; the response still
            # carries has_bars=false from persistence.
            logger.info("ensure_subscribed for %s/%s declined: %s", symbol, requested_timeframe, exc)

    try:
        chart_window = await resolve_chart_window(
            symbol=symbol,
            timeframe=requested_timeframe,
            from_ms=window_from_ms,
            to_ms=window_to_ms,
            now_ms=now_ms,
            polygon_api_key=app_settings.POLYGON_API_KEY,
            live_aggregator=LIVE_BAR_AGGREGATOR,
        )
    except ChartWindowError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None

    runs_in_window = _runs_active_in_window(
        runs,
        window_from_ms,
        window_to_ms,
        live_binding=live_binding,
        now_ms=now_ms,
    )
    snapshot_runs: list[ChartSnapshotRun] = []
    # Color index is assigned by sort order (oldest run first) so a fresh
    # deployment doesn't shift the color of older runs.
    for color_index, run in enumerate(sorted(runs_in_window, key=lambda r: r.get("started_at_ms") or 0)):
        run_dir = Path(run["run_dir"])
        is_current = live_binding is not None and run["run_id"] == live_binding.run_id
        trades = _filter_rows_to_window(
            _read_parquet_rows(run_dir / "trades.parquet"),
            window_from_ms,
            window_to_ms,
            key="entry_time_ms",
        )
        executions = _filter_rows_to_window(
            _read_parquet_rows(run_dir / "executions.parquet"),
            window_from_ms,
            window_to_ms,
            key="ts_ms",
        )
        snapshot_runs.append(
            ChartSnapshotRun(
                run_id=run["run_id"],
                started_at_ms=run.get("started_at_ms"),
                ended_at_ms=run.get("ended_at_ms"),
                is_current=is_current,
                color_index=color_index,
                trades=trades,
                executions=executions,
            )
        )

    return ChartSnapshotResponse(
        date=day.isoformat(),
        # Symbol is the resolved ticker for the day, or empty if unresolved
        # (legacy ledger, nothing deployed). The frontend treats both `null`
        # and empty as "unknown" with a single `||` fallback (PR #483 review).
        symbol=symbol or "",
        resolution=chart_window.resolution,
        timeframe=chart_window.timeframe,
        from_ms=window_from_ms,
        to_ms=window_to_ms,
        has_bars=bool(chart_window.bars),
        is_streaming=chart_window.is_streaming,
        now_ms=now_ms,
        bars=chart_window.bars,
        runs=snapshot_runs,
        overlay_notices=[
            ChartOverlayNotice(
                code=notice.code,
                message=notice.message,
                session_date=notice.session_date,
                source=notice.source,
            )
            for notice in chart_window.overlay_notices
        ],
    )


@router.get(
    "/{strategy_instance_id}/activity",
    response_model=LiveInstanceActivityProjection,
)
async def get_instance_activity(
    strategy_instance_id: str,
    session_date: Annotated[str | None, Query(alias="session_date")] = None,
    resolution: Annotated[str, Query()] = "1m",
) -> LiveInstanceActivityProjection:
    """Materialized Activity-tab projection for one exchange/session date.

    This is the canonical source for the Activity tab. The chart markers,
    Orders Today blotter, Broker Activity ledger, and attached raw IBKR
    endpoint evidence all come from this one response so the frontend cannot
    render a broker fill on the chart that is absent from the activity table.
    """
    sid = _validate_instance_id(strategy_instance_id)
    if resolution not in ("1m", "5s"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="resolution must be '1m' or '5s'")

    explicit_session_date = session_date is not None
    try:
        day = date.fromisoformat(session_date) if explicit_session_date else _today_ny()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid session_date") from None

    settings = get_settings()
    root = Path(settings.live_runs_root)
    artifacts_root = root.parent
    if not explicit_session_date:
        today_start_ms, today_end_ms = _ny_session_bounds_ms(day)
        today_wal_rows = _read_activity_wal_rows(
            artifacts_root=artifacts_root,
            sid=sid,
            start_ms=today_start_ms,
            end_ms=today_end_ms,
        )
        if not today_wal_rows:
            day = _latest_activity_wal_day(artifacts_root=artifacts_root, sid=sid) or day

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    runs = _scan_runs_by_instance(root).get(sid, [])
    symbol = _resolve_symbol(root, live_binding, runs)

    start_ms, end_ms = _ny_session_bounds_ms(day)
    wal_rows = _read_activity_wal_rows(
        artifacts_root=artifacts_root,
        sid=sid,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    repair_projection = load_activity_repair_projection(
        artifacts_root=artifacts_root,
        strategy_instance_id=sid,
        runs=runs,
        start_ms=start_ms,
        end_ms=end_ms,
        existing_rows=wal_rows,
    )
    evidence_refs = _activity_evidence_refs_for_session(
        sid=sid,
        symbol=symbol,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    activity_rows = [*wal_rows, *repair_projection.broker_rows]
    projection = _build_activity_projection(
        sid=sid,
        day=day,
        symbol=symbol,
        resolution=resolution,
        wal_rows=activity_rows,
        evidence_refs=evidence_refs,
    )
    consistency_warnings = _activity_lifecycle_consistency_warnings(
        artifacts_root=artifacts_root,
        sid=sid,
        day=day,
        runs=runs,
        live_binding=live_binding,
        activity_rows=activity_rows,
    )
    if consistency_warnings:
        projection = projection.model_copy(
            update={"reconciliation_warnings": [*projection.reconciliation_warnings, *consistency_warnings]}
        )
    broker_rows = fold_activity_event_rows(
        [
            *projection.broker_activity_rows,
            *repair_projection.closed_trade_rows,
        ]
    )
    return projection.model_copy(
        update={
            "broker_activity_summary": _broker_activity_summary(broker_rows),
            "broker_activity_rows": broker_rows,
        }
    )


@router.get(
    "/{strategy_instance_id}/active-dates",
    response_model=list[ActiveDateEntry],
)
async def get_active_dates(
    strategy_instance_id: str,
    resolution: Annotated[str, Query()] = "1m",
) -> list[ActiveDateEntry]:
    """All dates the operator can pick for this instance (Slice 6).

    Returns the union of:
      * dates with at least one run touching them (from the run-dir scan)
      * dates with persisted bars under the BarPersistence root
    so a date that has bars but no run-dir (rare; future seed-bar import)
    still appears, and a date the instance ran on but pre-dates
    persistence appears with ``has_bars=False``.
    """
    sid = _validate_instance_id(strategy_instance_id)
    if resolution not in ("1m", "5s"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="resolution must be '1m' or '5s'")

    settings = get_settings()
    root = Path(settings.live_runs_root)
    runs = _scan_runs_by_instance(root).get(sid, [])

    # Bar-bearing dates come from the live aggregator's persistence — the
    # one process-wide store. Symbol is sourced from the latest run's ledger.
    symbol = _resolve_symbol(root, None, runs)
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    bar_dates: set[date] = set()
    persistence = LIVE_BAR_AGGREGATOR._persistence
    if persistence is not None and symbol is not None:
        try:
            bar_dates = set(persistence.active_dates(symbol, resolution))
        except Exception as exc:
            logger.warning("active_dates lookup failed for %s/%s: %s", symbol, resolution, exc)

    # Run-bearing dates: count every UTC day a run overlaps (PR #483 review).
    # A run spanning midnight must appear on both UTC dates the picker
    # shows — anchoring only on started_at_ms hid the later days unless
    # persistence happened to carry bars for them.
    now_ms = _now_ms()
    runs_by_date: dict[date, int] = {}
    for run in runs:
        run_dir = Path(run["run_dir"])
        sidecar = _read_sidecar(run_dir)
        started = (
            sidecar.started_at_ms
            if sidecar is not None and sidecar.started_at_ms is not None
            else int(run.get("created_at_ms") or 0)
        )
        if started <= 0:
            continue
        # End at the sidecar's ended_at_ms when present (terminated run)
        # else now (still live). A live run's "last touched" day is today.
        ended = sidecar.ended_at_ms if sidecar is not None and sidecar.ended_at_ms is not None else now_ms
        start_day = datetime.fromtimestamp(started / 1000.0, tz=UTC).date()
        end_day = datetime.fromtimestamp(ended / 1000.0, tz=UTC).date()
        cursor_day = start_day
        while cursor_day <= end_day:
            runs_by_date[cursor_day] = runs_by_date.get(cursor_day, 0) + 1
            cursor_day = cursor_day + timedelta(days=1)

    all_dates = sorted(set(runs_by_date) | bar_dates)
    return [
        ActiveDateEntry(
            date=d.isoformat(),
            run_count=runs_by_date.get(d, 0),
            has_bars=d in bar_dates,
        )
        for d in all_dates
    ]


@router.post("/{strategy_instance_id}/desired-state", response_model=SetInstanceDesiredStateResponse)
async def set_instance_desired_state(
    strategy_instance_id: str, body: SetDesiredStateRequest
) -> SetInstanceDesiredStateResponse:
    """The single operator intent knob (ADR 0004).

    1. Write durable intent first (the crash-proof guarantee).
    2. If a live binding exists, enqueue the matching actuation command on the
       bound run so the running engine actuates immediately and acks.
    3. With no live binding, the durable write alone gates the next start.

    The engine command dispatcher persists intent as a *reconciling* writer, so
    live actuation leaves ``desired_state.json`` at the same semantic state —
    "paused-but-still-trading" is structurally hard to create.
    """
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)

    artifacts_root = _desired_state_root(root)
    try:
        sidecar_path = stable_desired_state_path(artifacts_root, sid)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc

    # PRD #616 / #619-A §A4 — re-run the shared capability evaluator
    # immediately before the durable write so a stale status snapshot
    # cannot drive this mutation past the Resume guards. ``load_instance_context``
    # is the canonical pre-write assembler; the projection and the CLI
    # consume the same composition.
    ctx = await _load_instance_context_for_router(sid)
    action_name = body.action.value  # "pause" | "resume" | "stop"
    account_freeze = _resolve_account_freeze(root.parent, ctx.runs)
    if action_name == "resume" and account_freeze is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "disabled_reason_code": "ACCOUNT_FROZEN",
                "disabled_reasons": ["ACCOUNT_FROZEN"],
                "gate_results": [account_freeze.to_gate_result().model_dump(mode="json")],
                "guard_state": ctx.guard_state.model_dump(mode="json"),
            },
        )
    gate = evaluate_action(
        action_name,  # type: ignore[arg-type]
        process=ctx.process,
        live_binding=ctx.live_binding,
        poisoned=ctx.poisoned,
        owned_positions_empty=ctx.owned_positions_empty,
        desired_state=ctx.desired_state,
        guard_state=ctx.guard_state,
        runtime_freshness=ctx.runtime_freshness,
        latest_mutation=ctx.latest_mutation,
    )
    if not gate.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "disabled_reason_code": gate.disabled_reason_code,
                "disabled_reasons": gate.disabled_reasons,
                "guard_state": ctx.guard_state.model_dump(mode="json"),
            },
        )

    repo = DesiredStateRepo(sidecar_path, trusted_root=artifacts_root / "live_state")
    record = repo.set(
        _ACTION_TO_STATE[body.action],
        updated_by=body.updated_by,
        reason=body.reason,
        now_ms=_now_ms(),
    )
    durable = DesiredStateRecordResponse(
        state=record.desired_state.value,
        updated_at_ms=record.updated_at_ms,
        updated_by=record.updated_by,
        reason=record.reason,
        version=record.version,
    )

    # Re-fetch the daemon state for the actuation step (the durable
    # write may have triggered a daemon-side response we should
    # reflect).  Using a fresh fetch keeps the actuation reasoning
    # against the latest binding.
    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    live_run_dir = _visible_live_run_dir(root, live_binding) if live_binding is not None else None
    if live_binding is None or live_run_dir is None:
        # No live binding, or the bound run dir is not visible under this
        # service's live_runs_root (root mismatch / missing artifacts). The
        # engine polls its real run dir, so a command written here would never
        # be seen or acked — stay durable-only rather than claim a phantom
        # actuation. `_interpret_daemon_process` only sets run_dir when the dir
        # actually exists locally.
        detail = (
            "durable only; will gate next start"
            if live_binding is None
            else f"durable only; bound run {live_binding.run_id} is not visible locally"
        )
        actuation = IntentActuation(actuated=False, detail=detail)
    else:
        verb = _ACTION_TO_VERB[body.action]
        try:
            command = CommandChannel(live_run_dir / "commands").write_from_operator(verb)
        except Exception as exc:
            actuation = IntentActuation(
                actuated=False,
                run_id=live_binding.run_id,
                detail=f"failed to enqueue live command: {exc}",
            )
        else:
            actuation = IntentActuation(
                actuated=True,
                run_id=live_binding.run_id,
                command_seq=command.seq,
                detail=f"{verb.value} queued on {live_binding.run_id}; awaiting ack",
            )

    receipt, warnings = await _mutation_rung_receipts_from_process(
        sid,
        root,
        settings,
        daemon,
        mutation_key=action_name,
    )
    return SetInstanceDesiredStateResponse(
        durable=durable,
        actuation=actuation,
        rung_receipt=receipt,
        rung_receipt_warnings=warnings,
    )


@router.post(
    "/{strategy_instance_id}/flatten-and-pause",
    response_model=SetInstanceDesiredStateResponse,
)
async def flatten_and_pause_instance(
    strategy_instance_id: str,
    body: SetDesiredStateRequest | None = None,
) -> SetInstanceDesiredStateResponse:
    """VCR-0007 / Phase 6A / ADR 0010 — composed panic-button endpoint.

    The cockpit's "Flatten and pause" affordance is the only path that
    composes durable PAUSE with a one-shot FLATTEN_NOW; the underlying
    primitives (``set_instance_desired_state`` and ``write_from_operator``)
    stay pure. Order is strictly:

    1. Write ``desired_state = PAUSED`` to the durable sidecar. If this
       fails, abort BEFORE enqueueing the one-shot — leaving a live FLATTEN
       behind an unpersisted PAUSE would re-open the bug VCR-0007 named.
    2. If a live binding exists, enqueue ``FLATTEN_NOW`` on the bound run.
       The bar loop honours ``desired_state = PAUSED`` and refuses new
       entries even if the one-shot fails to enqueue.

    The endpoint returns the structured response shape the existing
    desired-state endpoint uses so the cockpit can reuse its renderer.
    """
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)

    artifacts_root = _desired_state_root(root)
    try:
        sidecar_path = stable_desired_state_path(artifacts_root, sid)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc

    # PRD #607 / Slice 1 (#608) / #619-A §A4 — shared-capability gate.
    # The status endpoint's ``operator_surface.actions.flatten_and_pause``
    # is the cockpit's authority for when this keycap is enabled; the
    # mutation endpoint re-evaluates the same function via the canonical
    # ``load_instance_context`` assembler so a stale snapshot cannot
    # drive this past the same rule.
    ctx = await _load_instance_context_for_router(sid)
    gate = evaluate_action(
        "flatten_and_pause",
        process=ctx.process,
        live_binding=ctx.live_binding,
        owned_positions_empty=ctx.owned_positions_empty,
        runtime_freshness=ctx.runtime_freshness,
        latest_mutation=ctx.latest_mutation,
    )
    if not gate.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"disabled_reason_code": gate.disabled_reason_code},
        )

    payload = body or SetDesiredStateRequest(
        action=DesiredStateAction.pause,
        reason="flatten-and-pause",
        updated_by="operator",
    )
    repo = DesiredStateRepo(sidecar_path, trusted_root=artifacts_root / "live_state")
    try:
        record = repo.set(
            DesiredState.PAUSED,
            updated_by=payload.updated_by,
            reason=payload.reason or "flatten-and-pause",
            now_ms=_now_ms(),
        )
    except OSError as exc:
        # Step 1 failed — refuse the composition. The one-shot is NOT sent
        # because a flatten without a persisted PAUSE would still let the
        # next bar re-enter, re-opening VCR-0007.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"flatten_and_pause aborted before FLATTEN_NOW: durable PAUSE write failed: {exc}",
        ) from exc

    durable = DesiredStateRecordResponse(
        state=record.desired_state.value,
        updated_at_ms=record.updated_at_ms,
        updated_by=record.updated_by,
        reason=record.reason,
        version=record.version,
    )

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    live_run_dir = _visible_live_run_dir(root, live_binding) if live_binding is not None else None
    if live_binding is None or live_run_dir is None:
        detail = (
            "PAUSE persisted; no live binding so FLATTEN_NOW was not enqueued"
            if live_binding is None
            else (f"PAUSE persisted; bound run {live_binding.run_id} is not visible locally, FLATTEN_NOW not enqueued")
        )
        actuation = IntentActuation(actuated=False, detail=detail)
    else:
        try:
            command = CommandChannel(live_run_dir / "commands").write_from_operator(CommandVerb.FLATTEN)
        except Exception as exc:
            # PAUSE is already persisted — surface the failure honestly.
            # The durable PAUSE keeps the bar loop from re-entering even
            # though the flatten one-shot did not enqueue.
            actuation = IntentActuation(
                actuated=False,
                run_id=live_binding.run_id,
                detail=(
                    f"PAUSE persisted but FLATTEN_NOW failed to enqueue — retry the flatten one-shot manually: {exc}"
                ),
            )
        else:
            actuation = IntentActuation(
                actuated=True,
                run_id=live_binding.run_id,
                command_seq=command.seq,
                detail=(f"PAUSE persisted; FLATTEN_NOW queued on {live_binding.run_id}; awaiting flatten ack"),
            )

    receipt, warnings = await _mutation_rung_receipts_from_process(
        sid,
        root,
        settings,
        daemon,
        mutation_key="flatten_and_pause",
    )
    return SetInstanceDesiredStateResponse(
        durable=durable,
        actuation=actuation,
        rung_receipt=receipt,
        rung_receipt_warnings=warnings,
    )


@router.post(
    "/{strategy_instance_id}/reconcile",
    response_model=ReconcileAckResponse,
)
async def reconcile_instance(strategy_instance_id: str) -> ReconcileAckResponse:
    """Enqueue a RECONCILE command for the instance's live binding.

    Reconciliation PR 2 (runtime async). The data plane resolves the live
    binding through the daemon, writes a RECONCILE command into the run's
    ``commands/`` directory, and returns the ack envelope so the cockpit
    can render IN_PROGRESS while the engine's async control task probes
    the broker, runs the orchestrator, and overwrites the command ack
    with its verdict (Continue / Adopt / Adopt+Pause / Poison).

    The cockpit then polls ``operator_surface.reconciliation`` to observe
    IN_PROGRESS → CLEAN / ADOPTED / FAILED transitions. The receipt
    projection is the source of truth for state changes; this envelope
    just confirms the request was queued.

    Failure modes:

    - 409 NO_LIVE_BINDING when no bot process is running for the
      instance. Runtime reconciliation requires a live engine to acquire
      the submit lock and probe the broker — a durable-only enqueue
      would never be acted on.
    - 404 when the daemon reports a live binding but the bound run dir
      is not visible under this service's live_runs_root (root mismatch
      / missing artifacts).
    """
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    if live_binding is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "NO_LIVE_BINDING",
                "message": ("No bot process is running for this instance — reconciliation requires a live engine."),
            },
        )
    live_run_dir = _visible_live_run_dir(root, live_binding)
    if live_run_dir is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=(
                f"Bound run {live_binding.run_id} is not visible under this "
                "service's live_runs_root; cannot enqueue a runtime reconcile."
            ),
        )

    try:
        CommandChannel(live_run_dir / "commands").write_from_operator(CommandVerb.RECONCILE)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"failed to enqueue RECONCILE command: {exc}",
        ) from exc

    receipt, warnings = await _mutation_rung_receipts_from_process(
        sid,
        root,
        settings,
        daemon,
        mutation_key="reconcile",
    )
    return ReconcileAckResponse(
        request_id=mint_intent_id(),
        accepted_at_ms=_now_ms(),
        rung_receipt=receipt,
        rung_receipt_warnings=warnings,
    )


@router.post(
    "/{strategy_instance_id}/reconcile-mutation",
    response_model=ReconcileMutationResponse,
)
async def reconcile_instance_mutation(
    strategy_instance_id: str,
) -> ReconcileMutationResponse:
    """PRD #619-D3 — Reconcile the latest mutation_attempt for the instance.

    Read-only inspection that joins:

    - The latest persisted ``MutationAttempt`` (D1 repo).
    - Current daemon process state + binding (live snapshot, observed
      now — not the snapshot the original mutation acted on).
    - Child ``engine_runtime.json`` ``command_loop.state`` if present.
    - Durable ``desired_state`` sidecar.
    - Broker view's owned-positions emptiness.

    Calls the pure ``reconcile_mutation_effect`` classifier on the
    assembled evidence, advances the attempt to the resulting
    terminal state via ``transition_attempt``, persists, and returns
    the outcome.

    **The endpoint never replays the original mutation.** If the
    operator wants to retry, the matrix surfaces the next allowed
    action through ``operator_surface.actions``; that is a separate
    UI step.

    Returns 404 when no mutation has been persisted for the instance.
    Returns 409 when the attempt is in a state that cannot be
    Reconciled (``PREPARED`` / ``DISPATCHING`` — still in flight)
    or when it is already terminal (the prior outcome is available
    via the status projection's mutation evidence).
    """
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)
    repo = MutationAttemptRepo(_mutation_attempt_root(root))
    attempt = repo.latest_for(sid)
    if attempt is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail="no mutation_attempt to reconcile for this instance",
        )
    if attempt.dispatch_state in TERMINAL_STATES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "mutation_attempt is already terminal",
                "mutation_attempt_id": attempt.mutation_attempt_id,
                "dispatch_state": attempt.dispatch_state,
            },
        )
    if attempt.dispatch_state in {"PREPARED", "DISPATCHING"}:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "error": "mutation_attempt is still in flight; wait for the response before reconciling",
                "mutation_attempt_id": attempt.mutation_attempt_id,
                "dispatch_state": attempt.dispatch_state,
            },
        )

    evidence = await _assemble_reconciliation_evidence(sid, root, daemon_url=settings.live_runner_daemon_url)
    outcome = reconcile_mutation_effect(attempt, evidence)
    transitioned_at_ms = _now_ms()
    advanced = transition_attempt(
        attempt,
        outcome,
        transitioned_at_ms=transitioned_at_ms,
        evidence=evidence.model_dump(),
    )
    repo.write(advanced)
    return ReconcileMutationResponse(
        mutation_attempt_id=advanced.mutation_attempt_id,
        action=advanced.action,
        outcome=outcome,
        dispatch_state=advanced.dispatch_state,  # type: ignore[arg-type]
        evidence=advanced.evidence or {},
        reconciled_at_ms=transitioned_at_ms,
    )


async def _assemble_reconciliation_evidence(sid: str, root: Path, *, daemon_url: str) -> ReconciliationEvidence:
    """Read every evidence source the Reconcile classifier consumes.

    Each read is fail-soft: missing daemon / runtime / desired-state
    surfaces as ``None`` on the corresponding field rather than
    raising.  ``daemon_reachable=False`` only when the typed daemon
    fetch itself fails — distinct from "daemon reachable but reports
    the instance as ``unreachable``".
    """
    observed_at_ms = _now_ms()
    result, daemon = await host_daemon_client.fetch_instance_process(daemon_url, sid)
    daemon_reachable = result.kind == "CONNECTED"
    process_state = None
    bound_run_id = None
    if daemon is not None:
        process, live_binding = _interpret_daemon_process(daemon, root)
        process_state = process.state  # type: ignore[assignment]
        if live_binding is not None:
            bound_run_id = live_binding.run_id

    desired_state = _read_desired_state_literal(root, sid)
    engine_runtime_state = _read_engine_runtime_state(root, sid)
    broker_owned_positions_empty = _read_owned_positions_empty(root, sid)

    return ReconciliationEvidence(
        daemon_reachable=daemon_reachable,
        process_state=process_state,
        bound_run_id=bound_run_id,
        desired_state=desired_state,
        engine_runtime_state=engine_runtime_state,
        broker_owned_positions_empty=broker_owned_positions_empty,
        observed_at_ms=observed_at_ms,
    )


def _read_desired_state_literal(root: Path, sid: str) -> str | None:
    """Return the durable desired_state as one of RUNNING / PAUSED /
    STOPPED, or ``None`` when the sidecar is missing / unreadable.
    """
    view = _resolve_desired_state(root, sid)
    state = getattr(view, "state", None)
    if state in {"RUNNING", "PAUSED", "STOPPED"}:
        return state
    return None


def _read_engine_runtime_state(root: Path, sid: str) -> str | None:
    """Return the latest engine_runtime ``command_loop.state`` or
    ``None`` when no runtime artifact is present / readable.
    """
    runs = _scan_runs_by_instance(root).get(sid, [])
    if not runs:
        return None
    run_dir = Path(runs[0]["run_dir"])
    snapshot = read_engine_runtime_snapshot(run_dir / ENGINE_RUNTIME_FILENAME)
    if snapshot is None:
        return None
    return snapshot.command_loop.state


def _read_owned_positions_empty(root: Path, sid: str) -> bool | None:
    """Return ``True`` iff broker view exists and every non-zero owned
    position is empty; ``False`` when at least one is non-zero;
    ``None`` when no broker view exists.
    """
    broker = _instance_broker(root, sid)
    if broker is None:
        return None
    return not any(qty != 0 for qty in broker.owned_positions.values())


@router.get("/{strategy_instance_id}/commands", response_model=CommandsTimeline)
async def get_instance_commands(strategy_instance_id: str) -> CommandsTimeline:
    """Unified one-shot command timeline for the instance's bound run (#397).

    Commands route to the live binding only, so the timeline is empty when no
    live binding is visible.
    """
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    if live_binding is not None:
        run_dir = _visible_live_run_dir(root, live_binding)
        if run_dir is not None:
            return build_command_timeline(_confine(run_dir, "commands"))
    return CommandsTimeline(entries=[], poll_interval_ms=COMMAND_POLL_INTERVAL_MS)


@router.post("/{strategy_instance_id}/commands", response_model=CommandView)
async def issue_instance_command(strategy_instance_id: str, body: EnqueueCommandRequest) -> CommandView:
    """Enqueue a one-shot command on the instance's bound run (#397).

    Reserved to FLATTEN / RECONCILE / MARK_POISONED — PAUSE/RESUME/STOP are the
    durable intent knob, not commands. Requires a live binding.
    """
    sid = _validate_instance_id(strategy_instance_id)
    try:
        verb = CommandVerb(body.verb)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid command verb") from exc
    if verb not in _ONE_SHOT_VERBS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"{verb.value} is not a one-shot command; use the desired-state intent knob",
        )

    settings = get_settings()
    root = Path(settings.live_runs_root)
    # PRD #619-A §A4 — single-call assembly. The post-write-style
    # actuation here still needs ``run_dir`` (filesystem confine), so
    # we resolve it after the gate from the same observation.
    ctx = await _load_instance_context_for_router(sid)
    run_dir = _visible_live_run_dir(root, ctx.live_binding) if ctx.live_binding is not None else None

    # PRD #607 / Slice 1 (#608) — shared-capability gate for the cockpit
    # actions that flow through this endpoint.  Currently only
    # ``MARK_POISONED`` has a Slice-1 capability rule (NO_LIVE_BINDING /
    # ALREADY_POISONED); other one-shots fall back to the legacy
    # binding check below.
    if verb is CommandVerb.MARK_POISONED:
        capability = evaluate_action(
            "mark_poisoned",
            process=ctx.process,
            live_binding=ctx.live_binding,
            poisoned=ctx.poisoned,
            runtime_freshness=ctx.runtime_freshness,
            latest_mutation=ctx.latest_mutation,
        )
        if not capability.enabled:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={"disabled_reason_code": capability.disabled_reason_code},
            )

    if run_dir is None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="no live run bound to this instance to command")
    command = CommandChannel(run_dir / "commands").write_from_operator(verb)
    receipt, warnings = await _mutation_rung_receipts_for_instance(
        sid,
        root,
        settings,
        mutation_key=verb.value.lower(),
    )
    return CommandView(
        seq=command.seq,
        verb=command.verb.value,
        rung_receipt=receipt,
        rung_receipt_warnings=warnings,
    )


@router.post("/{strategy_instance_id}/emergency-flatten", response_model=HostRunnerActionResponse)
async def emergency_flatten_instance(
    strategy_instance_id: str, body: EmergencyFlattenRequest
) -> HostRunnerActionResponse:
    """Account-wide emergency flatten (§ 7.2 #6), independent of a live binding.

    The console FLATTEN *command* needs a live binding (it writes to the run's
    command channel for the engine to drain) — but after a halt/poison the
    binding is gone, exactly when an operator most wants to flatten. This reaches
    the daemon's one-shot ``emergency-flatten`` on the instance's latest run,
    reusing the existing paper-guarded CLI. It connects its own broker session,
    so it works with no live process. Account-wide only; namespace-attributed
    reconciliation stays fail-closed. The operator must echo the account id
    (defense-in-depth, mirrors the CLI ``--account`` gate).
    """
    sid = _validate_instance_id(strategy_instance_id)
    if not body.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="emergency-flatten requires confirm=true")
    settings = get_settings()
    root = Path(settings.live_runs_root)
    runs = _scan_runs_by_instance(root).get(sid, [])
    if not runs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"no run found for instance {sid!r} to flatten")
    run_id = str(runs[0]["run_id"])
    try:
        body_json = await host_daemon_client.emergency_flatten_run(
            settings.live_runner_daemon_url,
            run_id,
            {"account": body.account, "confirm": True},
        )
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("emergency_flatten", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    response = HostRunnerActionResponse.model_validate(body_json)
    receipt, warnings = await _mutation_rung_receipts_from_process(
        sid,
        root,
        settings,
        response.process.model_dump(mode="json"),
        mutation_key="emergency_flatten",
    )
    return response.model_copy(
        update={
            "rung_receipt": receipt,
            "rung_receipt_warnings": warnings,
        }
    )
