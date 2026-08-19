"""Retained runner boundaries, fleet summary, and daemon diagnostics.

The deprecated IBKR bot catalog and per-instance operator-control projections
have been removed. Canonical bot control lives under the Alpaca Broker V2 API;
this router retains the generic deploy/start/stop adapter and shared operational
diagnostics still used outside that retired UI.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.broker.ibkr.config import IbkrSettings, get_settings
from app.broker.runtime_snapshot import BrokerRuntimeSnapshot, snapshot_data_plane_broker
from app.engine.action_plan.parity import parity_diagnostics
from app.engine.live import host_daemon_client
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountFreezeEvidence,
    read_account_freeze,
)
from app.engine.live.account_identity import InvalidAccountIdError, normalize_account_id
from app.engine.live.bot_lifecycle_evaluator import (
    BotLifecycleEvaluator,
    LifecycleStartAdmissionEvidence,
    LifecycleTransitionRefusedError,
)
from app.engine.live.command_channel import CommandVerb
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRecord,
)
from app.engine.live.readiness import build_start_readiness
from app.engine.live.readiness_sidecar import read_readiness
from app.routers.broker_dependencies import require_connected_client
from app.routers.live_runs import (
    _confine,
    _now_ms,
    _read_ledger,
    _resolve_desired_state,
    _validate_path_segment,
)
from app.schemas.action_plan import ActionPlan, ActionPlanPreviewResponse
from app.schemas.broker_bots import BotStatusView
from app.schemas.daemon_diagnostics import DaemonDiagnosticReport
from app.schemas.live_runs import (
    AuditCopySizingLookup,
    FleetAccountSummary,
    FleetContamination,
    FleetRosterSnapshot,
    HostRunnerActionResponse,
    HostRunnerDeployRequest,
    HostRunnerDeployResponse,
    HostRunnerHealth,
    HostRunnerStartRequest,
    HostRunnerStopRequest,
    InstanceProcessView,
    LiveBinding,
    LiveInstanceDeployRequest,
    LiveInstanceSummary,
    MutationOutcomeUnknownResponse,
    MutationRungReceipt,
    QcAuditCopyListing,
    ReadinessVector,
    SetInstanceDesiredStateResponse,
)
from app.schemas.operator_blocker import (
    SURFACE_ANCHOR,
    DeployPreflightResponse,
    NavigateAction,
    OperatorBlocker,
    OperatorMove,
)
from app.services import deploy_preflight as deploy_preflight_service
from app.services import fleet_contamination as fleet_contamination_service
from app.services.account_crash_recovery import (
    crash_recovery_block_detail,
    crash_recovery_blocking_binding,
)
from app.services.account_start_gate import AccountStartGateError, ensure_account_start_gate
from app.services.account_truth_snapshot import get_account_truth_snapshot_provider
from app.services.bot_deletion import (
    BotDeletionCorruptError,
    bot_has_soft_deletion,
    bot_run_is_soft_deleted,
)
from app.services.bot_roll_call import bot_roll_call_offer_repo, safe_active_roll_call_offer
from app.services.broker_free_fleet_reads import (
    BrokerFreeFleetReadDependencies,
    BrokerFreeFleetReadService,
)
from app.services.broker_v2_panel.sqlite_panel_source import read_sqlite_roster_statuses
from app.services.daemon_diagnostics import (
    DaemonHealthPayloadError,
    DaemonHealthProbeError,
    get_daemon_diagnostics_service,
    project_daemon_diagnostic_report,
)
from app.services.daily_session_schedule import start_boundary_verdict
from app.services.deploy_admission import (
    SymbolResolution,
    evaluate_deploy_start_admission,
    resolve_symbol_from_ledger,
)
from app.services.fleet_contamination import (
    fetch_net_positions as _fetch_net_positions,
)
from app.services.fleet_contamination import (
    instance_broker as _instance_broker,
)
from app.services.fleet_contamination import (
    scan_runs_by_instance as _scan_runs_by_instance,
)
from app.services.fleet_daemon_snapshot_provider import (
    FleetDaemonObservation,
    FleetDaemonSnapshotProvider,
)
from app.services.ibkr_lifecycle_guard import ibkr_lifecycle_capability
from app.services.live_instance_config import live_config_for_run_dir
from app.services.mutation_attempt import (
    MutationAttempt,
    MutationAttemptRepo,
    MutationAttemptScope,
)
from app.services.mutation_rung_receipts import accepted_mutation_receipts
from app.services.presented_lifecycle_http import require_presented_lifecycle_action
from app.services.risk_reducing_lifecycle_intent import (
    RiskReducingIntentRefusedError,
    persist_risk_reducing_intent_response,
)
from app.services.start_admission_policy import StartAdmissionDependencies, StartAdmissionService
from app.services.strategy_validation_manifest import (
    StrategyValidationManifestError,
)
from app.services.surface_hub import SurfaceHub

logger = logging.getLogger(__name__)

router = APIRouter(tags=["live-instances"])
_FLEET_DAEMON_PROVIDER: FleetDaemonSnapshotProvider | None = None
_FLEET_ROSTER_HUB: SurfaceHub[FleetRosterSnapshot] | None = None

# Process states that mean a run is being actively written right now.
_LIVE_STATES = frozenset({"running", "stopping"})


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
        try:
            canonical_account_id = normalize_account_id(account_id)
        except InvalidAccountIdError:
            continue
        account_freeze = read_account_freeze(artifacts_root, canonical_account_id)
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
    try:
        return normalize_account_id(value)
    except InvalidAccountIdError:
        return None


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


def _resolve_evidence_run_dir(
    root: Path,
    live_binding: LiveBinding | None,
    runs: list[dict],
    *,
    require_started: bool = False,
) -> Path | None:
    """The run dir the status view describes: the visible live run, else the
    latest evidence run, else None (nothing deployed). Shared by start-defaults
    and provenance so they always read the same ledger.

    ``require_started`` (resume-guard only) skips never-started runs absent a
    binding, so a failed-``Deploy & run`` ledger-only child (``run_ledger.json``
    only, no ``run_status.json``) cannot — sorted newest-first — shadow a valid
    stopped run's resume evidence and wrongly refuse Resume; falls back to the
    newest run so a genuinely never-started instance still resolves a dir."""
    if live_binding is not None:
        run_dir = _visible_live_run_dir(root, live_binding)
        if run_dir is not None:
            return run_dir
    if require_started:
        started = next(
            (r for r in runs if (Path(r["run_dir"]) / "run_status.json").exists()),
            None,
        )
        if started is not None:
            return Path(started["run_dir"])
    if runs:
        return Path(runs[0]["run_dir"])
    return None


def _mutation_attempt_root(live_runs_root: Path) -> Path:
    """Artifact root for durable ``mutation_attempt`` records.

    Sibling to ``live_runs/`` and ``live_state/`` under the artifacts
    parent — same layout as the rest of 619's per-instance evidence.
    """
    return live_runs_root.parent / "mutation_attempts"


def _operator_mutation_scope(
    root: Path,
    *,
    instance_id: str,
    action: Literal["start", "stop", "flatten", "resume", "pause"],
    run_id: str | None,
) -> MutationAttemptScope:
    repo = MutationAttemptRepo(_mutation_attempt_root(root))
    return MutationAttemptScope.begin(
        repo,
        instance_id=instance_id,
        action=action,
        requested_at_ms=_now_ms(),
        run_id=run_id,
        now_ms=_now_ms,
    )


def _mutation_error_detail(detail: object, attempt: MutationAttempt) -> dict[str, object]:
    body = dict(detail) if isinstance(detail, dict) else {"message": str(detail)}
    body.update(
        mutation_attempt_id=attempt.mutation_attempt_id,
        mutation_dispatch_state=attempt.dispatch_state,
    )
    return body


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


def _container_resolve_repo_path(path: str) -> list[Path]:
    """Resolve a host-recorded repo path inside the data-plane container."""

    candidates = [Path(path)]
    for marker, container_root in (
        ("PythonDataService/app/", "/app/app/"),
        ("PythonDataService/", "/app/"),
        ("references/", "/app/references/"),
    ):
        idx = path.find(marker)
        if idx >= 0:
            candidates.append(Path(container_root + path[idx + len(marker) :]))
    return [candidate for candidate in candidates if candidate.is_file()] or candidates


async def _build_live_instance_summaries(
    settings: IbkrSettings,
    root: Path,
    *,
    fleet_observation: FleetDaemonObservation | None = None,
) -> list[LiveInstanceSummary]:
    sqlite_statuses = read_sqlite_roster_statuses("alpaca")
    if sqlite_statuses is not None:
        return await _sqlite_live_instance_summaries(
            settings,
            sqlite_statuses,
            fleet_observation=fleet_observation,
        )

    by_instance = _visible_runs_by_instance(root)

    if fleet_observation is None:
        result, daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
        daemon_reachable = result.kind == "CONNECTED" and daemon is not None
    else:
        daemon = fleet_observation.payload
        daemon_reachable = fleet_observation.is_current
    daemon_by_sid: dict[str, dict] = {}
    daemon_sids: set[str] = set()
    if daemon:
        for inst in daemon.get("instances", []):
            sid = inst.get("strategy_instance_id")
            if sid:
                daemon_sids.add(sid)
                if daemon_reachable:
                    daemon_by_sid[sid] = inst

    summaries: list[LiveInstanceSummary] = []
    for sid in sorted(set(by_instance) | daemon_sids):
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
                blockers=_fleet_roster_blockers(
                    strategy_instance_id=sid,
                    process_state=proc_state,
                    readiness_verdict=readiness_verdict,
                    readiness_as_of_ms=readiness_as_of_ms,
                ),
            )
        )
    return summaries


async def _sqlite_live_instance_summaries(
    settings: IbkrSettings,
    statuses: list[BotStatusView],
    *,
    fleet_observation: FleetDaemonObservation | None,
) -> list[LiveInstanceSummary]:
    """Join the SQLite roster to daemon process facts without artifact scans."""
    if fleet_observation is None:
        result, daemon = await host_daemon_client.fetch_instances(
            settings.live_runner_daemon_url
        )
        daemon_reachable = result.kind == "CONNECTED" and daemon is not None
    else:
        daemon = fleet_observation.payload
        daemon_reachable = fleet_observation.is_current

    daemon_by_sid = {
        str(item["strategy_instance_id"]): item
        for item in (daemon or {}).get("instances", [])
        if item.get("strategy_instance_id") and daemon_reachable
    }
    summaries: list[LiveInstanceSummary] = []
    for item in statuses:
        managed = daemon_by_sid.get(item.strategy_instance_id)
        process_state = (
            str(managed.get("process", {}).get("state") or "idle")
            if managed is not None
            else "offline"
            if daemon_reachable
            else "unreachable"
        )
        bound_run_id = (
            str(managed["run_id"])
            if managed is not None
            and process_state in _LIVE_STATES
            and managed.get("run_id")
            else None
        )
        summaries.append(
            LiveInstanceSummary(
                strategy_instance_id=item.strategy_instance_id,
                process_state=process_state,
                bound_run_id=bound_run_id,
                latest_run_id=item.active_run_id,
                desired_state=item.desired_state,
                readiness_verdict="UNKNOWN",
                readiness_as_of_ms=item.last_transition_at_ms,
            )
        )
    return summaries


def _fleet_roster_blockers(
    *,
    strategy_instance_id: str,
    process_state: str,
    readiness_verdict: Literal["READY", "BLOCKED", "DEGRADED", "UNKNOWN"],
    readiness_as_of_ms: int | None,
) -> list[OperatorBlocker]:
    if readiness_verdict == "READY" and process_state != "unreachable":
        return []

    if process_state == "unreachable":
        condition_id = "fleet_member_unreachable"
        headline = f"{strategy_instance_id} host is unreachable"
        detail = "Open Broker V2 to inspect host-process recovery before starting more account work."
        severity: Literal["blocking", "warning"] = "blocking"
    elif readiness_verdict == "BLOCKED":
        condition_id = "fleet_member_blocked"
        headline = f"{strategy_instance_id} is blocked"
        detail = "Open Broker V2 for the backend-authored blocker and recovery move."
        severity = "blocking"
    elif readiness_verdict == "DEGRADED":
        condition_id = "fleet_member_degraded"
        headline = f"{strategy_instance_id} is degraded"
        detail = "Open Broker V2 to review degraded readiness before fleet operations."
        severity = "warning"
    else:
        condition_id = "fleet_member_readiness_unknown"
        headline = f"{strategy_instance_id} readiness is unknown"
        detail = "Open Broker V2 to refresh readiness evidence before fleet operations."
        severity = "warning"

    return [
        OperatorBlocker.for_host(
            condition_id=condition_id,
            scope="fleet",
            host="fleet_roster",
            anchor=SURFACE_ANCHOR,
            audience="operator",
            disposition="fix_elsewhere",
            headline=headline,
            detail=detail,
            primary_move=OperatorMove(
                label="Open Broker V2",
                action=NavigateAction(
                    kind="navigate",
                    route="/brokers/alpaca/bots",
                    fragment=None,
                ),
            ),
            applies_to="both",
            severity=severity,
            evidence={
                "strategy_instance_id": strategy_instance_id,
                "process_state": process_state,
                "readiness_verdict": readiness_verdict,
                "readiness_as_of_ms": readiness_as_of_ms,
            },
        )
    ]


@router.get("", response_model=list[LiveInstanceSummary])
async def list_live_instances() -> list[LiveInstanceSummary]:
    """Account fleet overview: every known strategy instance, live or not."""
    hub = _FLEET_ROSTER_HUB
    if hub is not None and hub.latest is not None:
        return hub.latest.instances
    settings = get_settings()
    root = Path(settings.live_runs_root)
    return await _build_live_instance_summaries(settings, root)


def _broker_free_fleet_read_service() -> BrokerFreeFleetReadService:
    return BrokerFreeFleetReadService(
        BrokerFreeFleetReadDependencies(
            visible_runs_by_instance=_visible_runs_by_instance,
            run_dir_account_id=_run_dir_account_id,
            get_account_truth_snapshot_provider=get_account_truth_snapshot_provider,
            now_ms=_now_ms,
            fetch_broker_connected_account=_fetch_broker_connected_account,
        )
    )


async def _assemble_fleet_roster_snapshot() -> FleetRosterSnapshot:
    settings = get_settings()
    root = Path(settings.live_runs_root)
    provider = _FLEET_DAEMON_PROVIDER
    observation = await provider.observation() if provider is not None else None
    return FleetRosterSnapshot(
        fetched_at_ms=_now_ms(),
        daemon_fetched_at_ms=(
            observation.source_fetched_at_ms if observation is not None else None
        ),
        instances=await _build_live_instance_summaries(
            settings,
            root,
            fleet_observation=observation,
        ),
    )


def _fleet_roster_hub_for() -> SurfaceHub[FleetRosterSnapshot]:
    global _FLEET_ROSTER_HUB

    if _FLEET_ROSTER_HUB is None:
        _FLEET_ROSTER_HUB = SurfaceHub(
            strategy_instance_id="__fleet_roster__",
            assemble=_assemble_fleet_roster_snapshot,
        )
    return _FLEET_ROSTER_HUB


async def start_surface_hubs() -> None:
    """Start the one shared fleet producer at data-plane boot."""

    global _FLEET_DAEMON_PROVIDER

    settings = get_settings()
    root = Path(settings.live_runs_root)
    MutationAttemptRepo(_mutation_attempt_root(root)).recover_inflight(transitioned_at_ms=_now_ms())
    provider = _FLEET_DAEMON_PROVIDER
    if provider is None:
        provider = FleetDaemonSnapshotProvider(
            daemon_url=settings.live_runner_daemon_url,
            fetch_instances=host_daemon_client.fetch_instances,
            poll_interval_seconds=(settings.live_runner_fleet_poll_interval_seconds),
            breaker_initial_backoff_seconds=(settings.live_runner_daemon_breaker_initial_backoff_seconds),
            breaker_max_backoff_seconds=(settings.live_runner_daemon_breaker_max_backoff_seconds),
            now_ms=_now_ms,
        )
        _FLEET_DAEMON_PROVIDER = provider
    if not provider.is_running:
        await provider.start()
    await _fleet_roster_hub_for().start()


async def stop_surface_hubs() -> None:
    """Stop the shared fleet producer during data-plane shutdown."""

    global _FLEET_DAEMON_PROVIDER, _FLEET_ROSTER_HUB

    fleet_hub = _FLEET_ROSTER_HUB
    _FLEET_ROSTER_HUB = None
    if fleet_hub is not None:
        await fleet_hub.stop()
    provider = _FLEET_DAEMON_PROVIDER
    _FLEET_DAEMON_PROVIDER = None
    if provider is not None:
        await provider.stop()


async def _refresh_fleet_roster_after_mutation(
    strategy_instance_id: str,
) -> None:
    try:
        provider = _FLEET_DAEMON_PROVIDER
        if provider is not None:
            # Accepted daemon mutations are the authoritative out-of-band
            # invalidation signal. Refresh the shared fleet observation once;
            # normal client refreshes still obey the fleet cadence.
            await provider.refresh(force=True)
        if _FLEET_ROSTER_HUB is not None:
            await _FLEET_ROSTER_HUB.refresh()
    except Exception:
        logger.exception(
            "fleet roster refresh deferred after mutation",
            extra={"strategy_instance_id": strategy_instance_id},
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
            "presented_action",
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
    blockers = deploy_preflight_service.author_deploy_blockers(
        signals,
        account_id=account_id.strip(),
    )
    return DeployPreflightResponse(
        ready=not any(blocker.condition.severity == "blocking" for blocker in blockers),
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
            live_config=request.live_config,
        )
    except AccountArtifactError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StrategyValidationManifestError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    blockers = [
        blocker
        for blocker in deploy_preflight_service.author_deploy_blockers(signals)
        if blocker.condition.severity == "blocking"
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
    root = Path(settings.live_runs_root)
    daemon_request = await _host_deploy_request_from_public(body)
    require_presented_lifecycle_action(
        root.parent, daemon_request.account_id, daemon_request.strategy_instance_id, None, "deploy", body.presented_action
    )
    account_freeze = read_account_freeze(
        root.parent,
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
    await _raise_if_fleet_contamination_blocks_start(
        root,
        account_id=daemon_request.account_id,
    )
    if daemon_request.start and daemon_request.strategy_instance_id:
        _raise_if_crash_recovery_blocks_start(
            root.parent,
            account_id=daemon_request.account_id,
            strategy_instance_id=daemon_request.strategy_instance_id,
        )
    _raise_if_deploy_admission_blocks_start(
        root,
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
    if daemon_request.strategy_instance_id:
        await _refresh_fleet_roster_after_mutation(daemon_request.strategy_instance_id)
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
    del sid, root, settings, daemon_process
    return accepted_mutation_receipts(
        mutation_key=mutation_key,
        occurred_at_ms=_now_ms(),
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


def _bot_soft_deleted_detail(sid: str, run_id: str | None = None) -> dict[str, str]:
    detail = {
        "reason_code": "BOT_SOFT_DELETED",
        "message": f"{sid} has been deleted from the bot catalog.",
        "strategy_instance_id": sid,
    }
    if run_id is not None:
        detail["run_id"] = run_id
    return detail


async def _ensure_account_observation_lease_allows_start(
    artifacts_root: Path,
    account_id: str,
    settings: IbkrSettings,
    *,
    now_ms: int,
) -> None:
    try:
        await ensure_account_start_gate(
            artifacts_root,
            account_id=account_id,
            daemon_url=settings.live_runner_daemon_url,
            requested_authority=settings.account_gate_authority,
            client=require_connected_client(),
            now_ms=now_ms,
            current_now_ms=_now_ms,
        )
    except AccountStartGateError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc


def _start_request_with_ledger_strategy_default(
    root: Path,
    run_id: str,
    body: HostRunnerStartRequest,
) -> HostRunnerStartRequest:
    """Hydrate omitted Start strategy from the run ledger.

    ``HostRunnerStartRequest`` still has a legacy schema default so direct
    daemon callers remain backward compatible. The data-plane Start route has
    stronger evidence: it knows the run directory and must forward the same
    strategy key the ledger was reconciled against when the browser sends only
    a roll-call offer id.
    """

    if "strategy" in body.model_fields_set:
        return body
    try:
        ledger = _read_ledger(root / run_id)
    except (OSError, ValueError, KeyError):
        return body
    strategy_key = ledger.get("strategy_key")
    if not isinstance(strategy_key, str) or not strategy_key.strip():
        return body
    try:
        return HostRunnerStartRequest.model_validate(
            {**body.model_dump(), "strategy": strategy_key.strip()}
        )
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "START_SETTINGS_INCOMPLETE",
                "message": "The run ledger strategy key is not valid for Start.",
                "gate_id": "start.strategy",
                "run_id": run_id,
            },
        ) from exc


def _persist_start_intent(root: Path, sid: str) -> DesiredStateRecord | None:
    """Read the Start latch without letting Start itself clear STOPPED."""

    try:
        previous = BotLifecycleEvaluator(root.parent, sid).assert_start_latch_allows_start()
        if previous is not None and previous.desired_state is DesiredState.STOPPED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "STOPPED_REQUIRES_RESUME",
                    "message": "This bot is durably STOPPED. Resume it before starting.",
                    "gate_id": "desired_state.start",
                    "strategy_instance_id": sid,
                },
            )
    except LifecycleTransitionRefusedError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "STOPPED_REQUIRES_RESUME",
                "message": "This bot is durably STOPPED. Resume it before starting.",
                "gate_id": "desired_state.start",
                "strategy_instance_id": sid,
            },
        ) from None
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
                "message": "Could not read the start latch before launching the bot.",
                "gate_id": "desired_state.start",
                "strategy_instance_id": sid,
            },
        ) from exc
    return previous


async def _interactive_start_observation_guard(
    artifacts_root: Path,
    account_id: str,
    settings: IbkrSettings,
    now_ms: int,
) -> None:
    await _ensure_account_observation_lease_allows_start(
        artifacts_root,
        account_id,
        settings,
        now_ms=now_ms,
    )


async def _interactive_start_fleet_guard(root: Path, account_id: str) -> None:
    await _raise_if_fleet_contamination_blocks_start(root, account_id=account_id)


async def _recover_prepared_start_from_daemon_observation(
    settings: IbkrSettings,
    *,
    artifacts_root: Path,
    strategy_instance_id: str,
    run_id: str,
) -> None:
    """Resolve a response-loss Start only from the daemon's current process fact."""

    _result, process = await host_daemon_client.fetch_instance_process(
        settings.live_runner_daemon_url,
        strategy_instance_id,
    )
    if process is None:
        return
    daemon_state = process.get("state")
    if not isinstance(daemon_state, str):
        return
    ibkr_lifecycle_capability(artifacts_root, strategy_instance_id).recover_prepared_start(
        run_id=run_id,
        daemon_state=daemon_state,
        observed_at_ms=_now_ms(),
    )


def _start_admission_service(settings: IbkrSettings) -> StartAdmissionService:
    root = Path(settings.live_runs_root)
    return StartAdmissionService(
        artifacts_root=root.parent,
        live_runs_root=root,
        settings=settings,
        dependencies=StartAdmissionDependencies(
            scan_runs_by_instance=_scan_runs_by_instance,
            run_is_soft_deleted=_run_is_soft_deleted,
            soft_deleted_detail=_bot_soft_deleted_detail,
            account_freeze=_resolve_account_freeze,
            run_account_id=_run_dir_account_id,
            interactive_observation_guard=_interactive_start_observation_guard,
            interactive_fleet_guard=_interactive_start_fleet_guard,
            fetch_instance_process=host_daemon_client.fetch_instance_process,
            active_roll_call_offer=lambda live_root, strategy_instance_id, now_ms: safe_active_roll_call_offer(
                live_root,
                strategy_instance_id,
                now_ms=now_ms,
            ),
            live_config_for_run=live_config_for_run_dir,
            start_boundary_allowed=start_boundary_verdict,
            now_ms=_now_ms,
        ),
    )


@router.post("/runs/{run_id}/start", response_model=HostRunnerActionResponse)
async def start_run(run_id: str, body: HostRunnerStartRequest) -> HostRunnerActionResponse:
    """Launch the host runner for ``run_id`` by forwarding to the daemon."""
    try:
        run_id = _validate_path_segment(run_id, field="run_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    settings = get_settings()
    root = Path(settings.live_runs_root)
    existing_sid = _strategy_instance_id_for_run(root, run_id)
    account_id = _run_dir_account_id(root / run_id)
    presented = require_presented_lifecycle_action(
        root.parent, "" if account_id is None else account_id, "" if existing_sid is None else existing_sid, run_id, "start", body.presented_action
    )
    if presented is not None:
        body = body.model_copy(update={"idempotency_key": presented.idempotency_key})
    if existing_sid is not None:
        await _recover_prepared_start_from_daemon_observation(
            settings,
            artifacts_root=root.parent,
            strategy_instance_id=existing_sid,
            run_id=run_id,
        )
    admission = await _start_admission_service(settings).admit(run_id, body)
    if admission.refusal is not None:
        raise HTTPException(admission.refusal.status_code, detail=admission.refusal.detail)
    body = _start_request_with_ledger_strategy_default(root, run_id, body)
    sid = admission.strategy_instance_id or _strategy_instance_id_for_run(root, run_id)
    if sid is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="run has no strategy_instance_id ledger binding",
        )
    scope = _operator_mutation_scope(root, instance_id=sid, action="start", run_id=run_id)
    with scope:
        scope.stage = "persist_start_intent"
        _persist_start_intent(root, sid)
        admitted_at_ms = _now_ms()
        start_admission = LifecycleStartAdmissionEvidence(
            policy=admission.policy,
            strategy_instance_id=sid,
            run_id=run_id,
            roll_call_offer_id=body.roll_call_offer_id,
            admitted_at_ms=admitted_at_ms,
        )
        lifecycle_capability = ibkr_lifecycle_capability(root.parent, sid)
        scope.stage = "prepare_start_evaluator"
        lifecycle_capability.prepare_start_actuation(
            run_id=run_id,
            now_ms=admitted_at_ms,
            updated_by="system",
            reason="start_actuation_prepared",
            admission=start_admission,
        )
        scope.stage = "idempotent_start" if admission.idempotent_process is not None else "daemon_start"
        try:
            result = (
                {"accepted": True, "process": admission.idempotent_process}
                if admission.idempotent_process is not None
                else await host_daemon_client.start_run(
                    settings.live_runner_daemon_url,
                    run_id,
                    scope.daemon_payload(body, exclude={"roll_call_offer_id", "presented_action"}),
                )
            )
        except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
            unknown = scope.unknown(error=exc)
            await _refresh_fleet_roster_after_mutation(sid)
            _raise_outcome_unknown(
                "start_run",
                exc,
                mutation_attempt_id=unknown.mutation_attempt_id,
            )
        except host_daemon_client.HostDaemonError as exc:
            lifecycle_capability.abort_start_actuation(
                run_id=run_id,
                failure="daemon_start_rejected",
            )
            rejected = scope.reject_not_observed(
                outcome={"accepted": False, "status_code": exc.status_code},
            )
            await _refresh_fleet_roster_after_mutation(sid)
            raise HTTPException(
                exc.status_code,
                detail=_mutation_error_detail(exc.detail, rejected),
            ) from exc
        scope.stage = "start_response_assembly"
        response = _parse_action_response(result)
        await _maybe_start_broker_activity_publisher(response)
        if response.accepted:
            if body.roll_call_offer_id is not None:
                bot_roll_call_offer_repo(root, sid).consume(body.roll_call_offer_id)
            lifecycle_capability.record_start(
                run_id=run_id,
                now_ms=admitted_at_ms,
                updated_by="system",
                reason="start_accepted",
                admission=start_admission,
            )
        receipt, warnings = await _mutation_rung_receipts_from_process(
            sid,
            root,
            settings,
            response.process.model_dump(mode="json"),
            mutation_key="start",
        )
        confirmed = scope.confirm(outcome={"accepted": response.accepted, "run_id": run_id})
        response = response.model_copy(
            update={
                "rung_receipt": receipt,
                "rung_receipt_warnings": warnings,
                "mutation_attempt_id": confirmed.mutation_attempt_id,
                "mutation_dispatch_state": confirmed.dispatch_state,
            }
        )
    await _refresh_fleet_roster_after_mutation(sid)
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


@router.post("/runs/{run_id}/stop", response_model=SetInstanceDesiredStateResponse)
async def stop_run(run_id: str, body: HostRunnerStopRequest) -> SetInstanceDesiredStateResponse:
    """Retire direct daemon Stop in favour of durable, instance-scoped intent."""
    try:
        run_id = _validate_path_segment(run_id, field="run_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if body.force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "FORCE_STOP_RETIRED",
                "message": "Force stop is not an operator control. Use the durable Stop action instead.",
            },
        )

    settings = get_settings()
    root = Path(settings.live_runs_root)
    sid = _strategy_instance_id_for_run(root, run_id)
    if sid is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="run has no strategy_instance_id ledger binding",
        )
    account_id = _run_dir_account_id(root / run_id)
    presented = require_presented_lifecycle_action(
        root.parent, "" if account_id is None else account_id, sid, run_id, "stop", body.presented_action
    )
    try:
        response = await persist_risk_reducing_intent_response(
            mutation_scope=_operator_mutation_scope(root, instance_id=sid, action="stop", run_id=run_id),
            rung_receipts=lambda daemon: _mutation_rung_receipts_from_process(
                sid,
                root,
                settings,
                daemon,
                mutation_key="stop",
            ),
            artifacts_root=root.parent,
            strategy_instance_id=sid,
            desired_state=DesiredState.STOPPED,
            command_verb=CommandVerb.STOP,
            updated_by="operator",
            reason="stop-run",
            idempotency_key=None if presented is None else presented.idempotency_key,
            now_ms=_now_ms,
            daemon_url=settings.live_runner_daemon_url,
            live_binding_from_process=lambda daemon: _interpret_daemon_process(daemon, root)[1],
            visible_live_run_dir=lambda live_binding: _visible_live_run_dir(root, live_binding),
        )
    except RiskReducingIntentRefusedError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "LIFECYCLE_TRANSITION_REFUSED",
                "refusal_code": exc.refusal_code,
                "mutation_attempt_id": exc.mutation_attempt_id,
                "mutation_dispatch_state": exc.dispatch_state,
            },
        ) from exc
    await _refresh_fleet_roster_after_mutation(sid)
    return response


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
    try:
        return await get_daemon_diagnostics_service().health()
    except DaemonHealthProbeError as exc:
        result = exc.result
        if result.kind == "AUTH_FAILED":
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail="host daemon rejected the data plane's token",
            ) from exc
        if result.kind == "UNREACHABLE":
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=result.detail or "host daemon unreachable",
            ) from exc
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


@router.post("/daemon-health/renew-lease", response_model=HostRunnerHealth)
async def renew_daemon_lease() -> HostRunnerHealth:
    """Ask the host daemon to write a fresh control-plane lease now.

    This is the cockpit recovery action for
    ``runtime.control_plane_lease_stale``. The data plane forwards the
    authenticated request so the browser never holds the daemon token.
    """
    try:
        return await get_daemon_diagnostics_service().renew_control_plane_lease()
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("renew_daemon_lease", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    except DaemonHealthPayloadError as exc:
        logger.warning("invalid renew-lease payload from host daemon: %s", exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc


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
    root: Path,
    *,
    account_id: str | None = None,
) -> FleetContamination:
    return await fleet_contamination_service.compute_account_fleet_contamination(
        root,
        fetch_positions=_fetch_net_positions,
        account_id=account_id,
    )


async def _raise_if_fleet_contamination_blocks_start(
    root: Path,
    *,
    account_id: str | None = None,
) -> None:
    """Make the Clerk-owned account verdict an authoritative start boundary."""

    if account_id is not None:
        broker_account, broker_known = await _fetch_broker_connected_account()
        if not broker_known or broker_account is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "BROKER_TRUTH_UNAVAILABLE",
                    "message": "Connected broker account identity is unavailable. Wait for a fresh broker observation before starting bots.",
                    "gate_id": "account.broker_truth",
                    "operator_next_step": "WAIT_FOR_BROKER_TRUTH",
                },
            )
        try:
            normalized_broker_account = normalize_account_id(broker_account)
            normalized_account_id = normalize_account_id(account_id)
        except InvalidAccountIdError:
            # An unreadable identity is no safer than a mismatch.  Preserve
            # the raw values for the operator, but never collapse two failed
            # normalizations into an apparent match.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "BROKER_TRUTH_UNAVAILABLE",
                    "message": "Connected broker account identity is unreadable. Wait for a fresh broker observation before starting bots.",
                    "gate_id": "account.broker_truth",
                    "operator_next_step": "WAIT_FOR_BROKER_TRUTH",
                    "expected_account_id": account_id,
                    "connected_account_id": broker_account,
                },
            ) from None
        if normalized_broker_account != normalized_account_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "reason_code": "BROKER_ACCOUNT_MISMATCH",
                    "message": "Connected broker account does not match this run's account identity.",
                    "gate_id": "account.broker_truth",
                    "operator_next_step": "WAIT_FOR_BROKER_TRUTH",
                    "expected_account_id": account_id,
                    "connected_account_id": broker_account,
                },
            )
    try:
        fleet = await _compute_account_fleet_contamination(
            root,
            account_id=normalized_account_id if account_id is not None else None,
        )
    except Exception as exc:
        logger.warning("fleet contamination gate unavailable", extra={"error": str(exc)})
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason_code": "FLEET_CONTAMINATION_UNAVAILABLE",
                "message": "Account exposure evidence is unavailable. Reconcile it before starting bots.",
                "gate_id": "account.fleet_contamination",
            },
        ) from exc
    if fleet.verdict == "unknown" and fleet.policy_blocks_starts:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "reason_code": "BROKER_TRUTH_UNAVAILABLE",
                "message": "Broker account position truth is unavailable. Wait for a fresh account sweep before starting bots.",
                "gate_id": "account.broker_truth",
                "operator_next_step": "WAIT_FOR_BROKER_TRUTH",
                "contamination": fleet.model_dump(mode="json"),
            },
        )
    # ``policy_blocks_starts`` is the server-owned admission policy for an
    # otherwise non-contaminated verdict.  An ``unknown`` observation with
    # that flag unset is intentionally advisory, not an implicit replacement
    # start gate; the actual fleet service sets it for this route.
    if fleet.verdict != "contaminated":
        return
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "reason_code": "FLEET_CONTAMINATED",
            "message": "Account exposure is contaminated. Clear the account fleet state before starting bots.",
            "gate_id": "account.fleet_contamination",
            "blockers": [
                deploy_preflight_service.author_fleet_contamination_blocker().model_dump(mode="json")
            ],
            "contamination": fleet.model_dump(mode="json"),
        },
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
    return (
        await _broker_free_fleet_read_service().account_summary(
            root,
            requested_account_id=None,
        )
    ).contamination


@router.get("/account-summary", response_model=FleetAccountSummary)
async def get_account_summary(account_id: str | None = Query(default=None)) -> FleetAccountSummary:
    """PRD #616 — server-authored account row.

    Composes position contamination with account-identity verification
    so the cockpit renders the account block from one DTO.  Account
    identity is separate from contamination: a CONFLICTING identity
    does not imply contamination, and vice versa.
    """
    settings = get_settings()
    root = Path(settings.live_runs_root)
    try:
        requested_account_id = normalize_account_id(account_id) if account_id is not None else None
    except InvalidAccountIdError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return await _broker_free_fleet_read_service().account_summary(
        root,
        requested_account_id=requested_account_id,
    )


@router.get(
    "/fleet/stream",
    summary="Latest-wins SSE stream of complete fleet roster snapshots",
)
async def stream_fleet_roster(
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Emit the current full fleet roster and every later semantic version."""

    del last_event_id  # State reconnect always sends current truth; no replay.
    hub = _FLEET_ROSTER_HUB
    if hub is None or hub.latest is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason_code": "FLEET_ROSTER_SNAPSHOT_UNAVAILABLE",
                "message": "The fleet roster producer has not completed a successful refresh yet.",
            },
        )

    async def event_source():
        queue = hub.subscribe()
        try:
            while True:
                snapshot = await queue.get()
                if snapshot is None:
                    yield "event: end\ndata: {}\n\n"
                    return
                event_id = f"{snapshot.stream_epoch}:{snapshot.surface_version}"
                yield (f"id: {event_id}\nevent: snapshot\ndata: {snapshot.model_dump_json()}\n\n")
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
        "was lost. The run may or may not be running. Refresh the Broker V2 panel "
        "to read live state before deciding whether to retry."
    ),
    "renew_daemon_lease": (
        "A control-plane lease renewal request was sent to the host runner "
        "daemon but the response was lost. Refresh the fleet roster to read the "
        "latest control-plane state before deciding whether to retry."
    ),
}


def _raise_outcome_unknown(
    endpoint: Literal[
        "deploy",
        "start_run",
        "renew_daemon_lease",
    ],
    exc: host_daemon_client.HostDaemonOutcomeUnknownError,
    *,
    mutation_attempt_id: str | None = None,
) -> NoReturn:
    """Surface an ambiguous-outcome mutation failure as a typed 409 (PRD #619-C5).

    The body is :class:`MutationOutcomeUnknownResponse`; the caller
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
    detail = body.model_dump(mode="json")
    if mutation_attempt_id is not None:
        detail["mutation_attempt_id"] = mutation_attempt_id
        detail["mutation_dispatch_state"] = "OUTCOME_UNKNOWN"
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    ) from exc
