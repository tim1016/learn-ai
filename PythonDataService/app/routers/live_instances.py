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

import json
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import ValidationError

from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.desired_state import DesiredStateRepo
from app.engine.live.readiness import build_start_readiness
from app.engine.live.readiness_sidecar import read_readiness
from app.engine.strategy.spec.descriptors import decision_column_descriptors
from app.engine.strategy.spec.schema import load_spec_from_path
from app.routers.live_runs import (
    _ACTION_TO_STATE,
    _confine,
    _desired_state_root,
    _now_ms,
    _read_ledger,
    _read_parquet_tail,
    _resolve_desired_state,
    _validate_path_segment,
)
from app.schemas.live_runs import (
    DesiredStateAction,
    DesiredStateRecordResponse,
    EvidenceBinding,
    InstanceProcessView,
    IntentActuation,
    LiveBinding,
    LiveInstanceStatus,
    LiveInstanceSummary,
    ReadinessVector,
    SetDesiredStateRequest,
    SetInstanceDesiredStateResponse,
)

# Durable intent action -> live-actuation command verb. PAUSE/RESUME/STOP are the
# only verbs the durable knob actuates; the engine persists them as reconciling
# writers, so live actuation leaves desired_state.json at the same semantic state.
_ACTION_TO_VERB = {
    DesiredStateAction.pause: CommandVerb.PAUSE,
    DesiredStateAction.resume: CommandVerb.RESUME,
    DesiredStateAction.stop: CommandVerb.STOP,
}

# Filename of the durable desired-state sidecar (the stable
# <artifacts>/live_state/<sid>/ layout owned by desired_state.py).
_DESIRED_STATE_FILE = "desired_state.json"

router = APIRouter(tags=["live-instances"])

# strategy_instance_id flows into a daemon URL and a filesystem path; confine it
# to a single safe segment at the boundary.
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Process states that mean a run is being actively written right now.
_LIVE_STATES = frozenset({"running", "stopping"})


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
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id"
        ) from exc
    if _INSTANCE_ID_RE.fullmatch(safe) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid strategy_instance_id: {strategy_instance_id!r}",
        )
    return safe


def _scan_runs_by_instance(root: Path) -> dict[str, list[dict]]:
    """Group run dirs by ``strategy_instance_id`` from their ledgers, newest first.

    Legacy runs with no binding are skipped — they are not instances.
    """
    out: dict[str, list[dict]] = {}
    if not root.is_dir():
        return out
    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        try:
            ledger = _read_ledger(run_dir)
        except (OSError, json.JSONDecodeError):
            continue
        sid = ledger.get("strategy_instance_id") or ""
        if not sid:
            continue
        out.setdefault(sid, []).append(
            {
                "run_id": ledger.get("run_id") or run_dir.name,
                "run_dir": str(run_dir),
                "created_at_ms": ledger.get("created_at_ms") or 0,
            }
        )
    for runs in out.values():
        runs.sort(key=lambda r: r["created_at_ms"], reverse=True)
    return out


def _interpret_daemon_process(
    daemon: dict | None, root: Path
) -> tuple[InstanceProcessView, LiveBinding | None]:
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
    if state in _LIVE_STATES and run_id:
        run_dir = root / run_id
        binding = LiveBinding(run_id=run_id, run_dir=str(run_dir) if run_dir.is_dir() else None)
        view = InstanceProcessView(state=state, pid=pid, bound_run_id=run_id, started_at_ms=started)
        return view, binding
    # exited / idle: a run id may be present (the run that just exited) but it is
    # not a live binding.
    return InstanceProcessView(state=state, pid=pid, bound_run_id=run_id, started_at_ms=started), None


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


def _strategy_state(
    root: Path, live_binding: LiveBinding | None, runs: list[dict]
) -> tuple[dict | None, list[dict]]:
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
        return None, []

    decisions_path = run_dir / "decisions.parquet"
    # Guard existence: _read_parquet_tail's except tuple references a pyarrow
    # symbol absent in this version, so it raises on a missing file rather than
    # returning []. A run with no decisions yet is normal (pre-warmup).
    rows = _read_parquet_tail(decisions_path, 1) if decisions_path.is_file() else []
    latest_decision = rows[0] if rows else None

    descriptors: list[dict] = []
    try:
        ledger = _read_ledger(run_dir)
        spec = load_spec_from_path(ledger["strategy_spec_path"])
        descriptors = decision_column_descriptors(spec)
    except (OSError, ValueError, KeyError):
        descriptors = []
    return latest_decision, descriptors


@router.get("", response_model=list[LiveInstanceSummary])
async def list_live_instances() -> list[LiveInstanceSummary]:
    """Account fleet overview: every known strategy instance, live or not."""
    settings = get_settings()
    root = Path(settings.live_runs_root)
    by_instance = _scan_runs_by_instance(root)

    daemon = await host_daemon_client.fetch_instances(settings.live_runner_daemon_url)
    daemon_reachable = daemon is not None
    daemon_by_sid: dict[str, dict] = {}
    if daemon:
        for inst in daemon.get("instances", []):
            sid = inst.get("strategy_instance_id")
            if sid:
                daemon_by_sid[sid] = inst

    summaries: list[LiveInstanceSummary] = []
    for sid in sorted(set(by_instance) | set(daemon_by_sid)):
        managed = daemon_by_sid.get(sid)
        runs = by_instance.get(sid, [])
        if managed is not None:
            proc_state = str(managed.get("process", {}).get("state") or "idle")
            bound = managed.get("run_id") if proc_state in _LIVE_STATES else None
        else:
            proc_state = "offline" if daemon_reachable else "unreachable"
            bound = None
        desired = _resolve_desired_state(root, sid)
        summaries.append(
            LiveInstanceSummary(
                strategy_instance_id=sid,
                process_state=proc_state,
                bound_run_id=bound,
                latest_run_id=runs[0]["run_id"] if runs else None,
                desired_state=desired.state,
            )
        )
    return summaries


@router.get("/{strategy_instance_id}/status", response_model=LiveInstanceStatus)
async def get_instance_status(strategy_instance_id: str) -> LiveInstanceStatus:
    """Instance control-room status: live binding (registry) + evidence + intent."""
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)

    daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    process, live_binding = _interpret_daemon_process(daemon, root)

    runs = _scan_runs_by_instance(root).get(sid, [])
    evidence = EvidenceBinding(run_id=runs[0]["run_id"]) if runs else None
    desired = _resolve_desired_state(root, sid)
    latest_decision, decision_columns = _strategy_state(root, live_binding, runs)

    return LiveInstanceStatus(
        strategy_instance_id=sid,
        process=process,
        live_binding=live_binding,
        evidence_binding=evidence,
        desired_state=desired,
        readiness=_resolve_readiness(root, live_binding, runs, desired.state),
        latest_decision=latest_decision,
        decision_columns=decision_columns,
        fetched_at_ms=_now_ms(),
    )


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

    # The id is a remote (URL) value flowing into a filesystem write. Build the
    # sidecar path through `_confine` (resolve + relative_to on the validated
    # literal, return used) exactly as `_validate_run_id` does for the
    # CodeQL-clean command-channel write — this is the form the scanner
    # recognizes as breaking py/path-injection. `_safe_desired_state_path`
    # discards `_confine`'s confined return, so the scanner can't see it.
    artifacts_root = _desired_state_root(root)
    try:
        sidecar_dir = _confine(artifacts_root / "live_state", sid)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id"
        ) from exc
    repo = DesiredStateRepo(sidecar_dir / _DESIRED_STATE_FILE)
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

    daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
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

    return SetInstanceDesiredStateResponse(durable=durable, actuation=actuation)
