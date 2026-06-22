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
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import ValidationError

from app.broker.ibkr.config import get_settings
from app.engine.action_plan.parity import parity_diagnostics
from app.engine.live import host_daemon_client
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.daemon_connectivity_monitor import (
    get_monitor as get_daemon_connectivity_monitor,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRepo
from app.engine.live.engine_runtime import (
    ENGINE_RUNTIME_FILENAME,
    read_engine_runtime_snapshot,
)
from app.engine.live.fleet import (
    compute_fleet_account_summary,
    compute_fleet_contamination,
)
from app.engine.live.halt import read_poisoned_flag
from app.engine.live.intent_events import IntentEventType
from app.engine.live.intent_wal import IntentWal, IntentWalCorruptError
from app.engine.live.live_state_sidecar import LiveStateSidecarCorruptError, LiveStateSidecarRepo
from app.engine.live.nyse_calendar import nyse_session_state_at_ms
from app.engine.live.readiness import build_start_readiness
from app.engine.live.readiness_sidecar import read_readiness
from app.engine.strategy.spec.descriptors import decision_column_descriptors
from app.engine.strategy.spec.schema import load_spec_from_path
from app.routers.live_runs import (
    _ACTION_TO_STATE,
    COMMAND_POLL_INTERVAL_MS,
    _confine,
    _desired_state_root,
    _now_ms,
    _read_ledger,
    _read_parquet_tail,
    _read_sidecar,
    _resolve_desired_state,
    _validate_path_segment,
    build_command_timeline,
)
from app.schemas.action_plan import ActionPlan, ActionPlanPreviewResponse
from app.schemas.live_runs import (
    ActiveDateEntry,
    AuditCopySizingLookup,
    ChartSnapshotResponse,
    ChartSnapshotRun,
    CommandsTimeline,
    CommandView,
    DesiredStateAction,
    DesiredStateRecordResponse,
    EmergencyFlattenRequest,
    EnqueueCommandRequest,
    EvidenceBinding,
    FleetAccountSummary,
    FleetContamination,
    HostRunnerActionResponse,
    HostRunnerDeployRequest,
    HostRunnerDeployResponse,
    HostRunnerStartRequest,
    HostRunnerStopRequest,
    InstanceBrokerView,
    InstanceLastExit,
    InstanceProcessView,
    InstanceProvenance,
    InstanceSizing,
    InstanceStartDefaults,
    IntentActuation,
    LiveBinding,
    LiveInstanceStatus,
    LiveInstanceSummary,
    MutationOutcomeUnknownResponse,
    QcAuditCopyListing,
    ReadinessVector,
    SetDesiredStateRequest,
    SetInstanceDesiredStateResponse,
    SizingAuditRow,
)
from app.services.instance_context import InstanceContext, load_instance_context
from app.services.mutation_attempt import MutationAttempt, MutationAttemptRepo
from app.services.operator_capability import evaluate_action
from app.services.operator_surface import compute_operator_surface
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

# Filename of the durable desired-state sidecar (the stable
# <artifacts>/live_state/<sid>/ layout owned by desired_state.py).
_DESIRED_STATE_FILE = "desired_state.json"

logger = logging.getLogger(__name__)

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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc
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


def _strategy_state(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> tuple[dict | None, list[dict]]:
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


def _resolve_latest_mutation(
    live_runs_root: Path, strategy_instance_id: str
) -> MutationAttempt | None:
    """Read the most recent ``MutationAttempt`` for the instance.

    Returns ``None`` when no attempts have been persisted (typical for
    a freshly-deployed instance) or when the storage root is absent.
    Malformed and forward-incompatible artifacts are also surfaced as
    ``None`` per ``MutationAttemptRepo.latest_for`` semantics — the
    action-conflict matrix treats absence and corruption identically:
    no prior unresolved mutation to consider.
    """
    return MutationAttemptRepo(_mutation_attempt_root(live_runs_root)).latest_for(
        strategy_instance_id
    )


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
    if live_binding is None:
        return None
    run_dir = _visible_live_run_dir(root, live_binding)
    if run_dir is None:
        return unavailable_runtime_freshness("ENGINE_RUNTIME_MISSING")
    path = run_dir / ENGINE_RUNTIME_FILENAME
    if not path.is_file():
        return unavailable_runtime_freshness("ENGINE_RUNTIME_MISSING")
    snapshot = read_engine_runtime_snapshot(path)
    if snapshot is None:
        return unavailable_runtime_freshness(
            "ENGINE_RUNTIME_INVALID_OR_INCOMPATIBLE"
        )
    return evaluate_runtime_freshness(
        snapshot,
        now_ms=now_ms,
        session_state=nyse_session_state_at_ms(now_ms),
    )


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


def _resolve_symbol(root: Path, live_binding: LiveBinding | None, runs: list[dict]) -> str | None:
    """Traded symbol for the instance.

    Resolution order:
      1. ``ledger.live_config.symbol`` (operator-set, hashed into run_id)
      2. ``strategy_spec.symbols[0]`` (the spec the ledger is reconciled to —
         the canonical "this is what the algorithm trades")

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
    run_dir = _resolve_evidence_run_dir(root, live_binding, runs)
    if run_dir is None:
        return None
    try:
        ledger = _read_ledger(run_dir)
    except (OSError, ValueError, KeyError):
        return None
    live_config = ledger.get("live_config") or {}
    symbol = live_config.get("symbol") if isinstance(live_config, dict) else None
    if isinstance(symbol, str) and symbol:
        return symbol
    # Spec fallback — read the strategy spec the ledger pins and pick the
    # first symbol. A multi-symbol strategy (none in the fleet today) would
    # surface here as the first one; the per-day TODO above lays out the
    # eventual generalization.
    spec_path = ledger.get("strategy_spec_path")
    if isinstance(spec_path, str) and spec_path:
        for candidate in _container_resolve_repo_path(spec_path):
            try:
                spec = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            symbols = spec.get("symbols")
            if isinstance(symbols, list) and symbols:
                first = symbols[0]
                if isinstance(first, str) and first:
                    return first
            break
    return None


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

    # Local import — keeps the routers package free of a top-level dep
    # on the CLI entrypoint module, which has a much larger import graph.
    from app.engine.live.run import _latest_run_dir_for_instance

    run_dir = _latest_run_dir_for_instance(artifacts_root, strategy_instance_id)
    if run_dir is not None:
        wal_rows = _fold_wal_sizing_audit(run_dir)
        if wal_rows:
            return wal_rows

    sidecar_path = artifacts_root / "live_state" / strategy_instance_id / "live_state.json"
    if not sidecar_path.is_file():
        return []
    try:
        envelope = LiveStateSidecarRepo(sidecar_path).read()
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
                    "reference_price": event.reference_price or "",
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
    from app.routers.engine import _STRATEGY_REGISTRY

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


@router.get("", response_model=list[LiveInstanceSummary])
async def list_live_instances() -> list[LiveInstanceSummary]:
    """Account fleet overview: every known strategy instance, live or not."""
    settings = get_settings()
    root = Path(settings.live_runs_root)
    by_instance = _scan_runs_by_instance(root)

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


@router.post("", response_model=HostRunnerDeployResponse, status_code=status.HTTP_201_CREATED)
async def deploy_instance(body: HostRunnerDeployRequest, response: Response) -> HostRunnerDeployResponse:
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
    try:
        result = await host_daemon_client.deploy(settings.live_runner_daemon_url, body.model_dump())
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


@router.post("/runs/{run_id}/start", response_model=HostRunnerActionResponse)
async def start_run(run_id: str, body: HostRunnerStartRequest) -> HostRunnerActionResponse:
    """Launch the host runner for ``run_id`` by forwarding to the daemon (ADR 0007).

    Start/Stop are routed through the data plane — not called from the browser —
    because the daemon now enforces a mandatory ``X-Live-Runner-Token`` on every
    actuation route, and the browser must never hold that shared secret. The data
    plane reads the token from the artifacts bind mount and forwards it. The
    daemon's statuses propagate verbatim: bad ``strategy``/spec mismatch -> 400,
    missing run -> 404, subprocess/daemon unreachable -> 503.
    """
    try:
        run_id = _validate_path_segment(run_id, field="run_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    settings = get_settings()
    try:
        result = await host_daemon_client.start_run(settings.live_runner_daemon_url, run_id, body.model_dump())
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        _raise_outcome_unknown("start_run", exc)
    except host_daemon_client.HostDaemonError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
    return _parse_action_response(result)


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
    return _parse_action_response(result)


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
        hydration_accepted=hydration_accepted,
        hydration_failure_reason=hydration_failure_reason,
        halt_trigger=halt_trigger,
        halt_at_ms=halt_at_ms,
        halt_detail=halt_detail,
    )


def _instance_broker(root: Path, sid: str) -> InstanceBrokerView | None:
    """The instance's namespace-attributed broker slice from its live-state
    sidecar (ADR 0005, #398). Ownership is keyed on bot_order_namespace;
    owned_positions is the engine's running tally of its own namespace fills,
    never decomposed from the net account snapshot.
    """
    artifacts_root = _desired_state_root(root)
    try:
        # Confine the sidecar path on the validated id (same barrier as the
        # desired-state write) so the read can't escape live_state/.
        sidecar_dir = _confine(artifacts_root / "live_state", sid)
    except ValueError:
        return None
    repo = LiveStateSidecarRepo(sidecar_dir / "live_state.json")
    try:
        envelope = repo.read()
    except LiveStateSidecarCorruptError:
        return None
    if envelope is None:
        return None
    return InstanceBrokerView(
        bot_order_namespace=envelope.bot_order_namespace,
        owned_positions=dict(envelope.expected_position_by_symbol),
        pending_order_count=len(envelope.pending_intents),
    )


async def _fetch_net_positions() -> dict[str, int] | None:
    """Best-effort net account position by symbol from the broker; ``None`` when
    the broker is unavailable — the fleet view then reports residual unknown.

    Fail-open boundary: any broker/connection error resolves to ``None`` (logged,
    not silent) rather than failing the fleet endpoint.
    """
    try:
        from app.broker.ibkr import account as ibkr_account
        from app.routers.broker import _require_connected_or_503

        client = _require_connected_or_503()
        snapshot = await ibkr_account.fetch_positions(client)
    except Exception as exc:
        logger.info("fleet net-position fetch unavailable: %s", exc)
        return None
    net: dict[str, int] = {}
    for pos in snapshot.positions:
        symbol = str(pos.symbol).upper()
        net[symbol] = net.get(symbol, 0) + int(pos.quantity)
    return net


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


def _instance_ledger_account_id(root: Path, sid: str) -> str | None:
    """Latest ledger ``account_id`` for ``sid`` (``None`` when no ledger
    or the ledger pre-dates the field).  Pure read; used by the fleet
    account-identity aggregation."""
    runs = _scan_runs_by_instance(root).get(sid, [])
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


async def _fetch_broker_connected_account() -> tuple[str | None, bool]:
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
    from app.broker.runtime_snapshot import snapshot_data_plane_broker

    snapshot = snapshot_data_plane_broker()
    if not snapshot.client_available or not snapshot.connected:
        return None, False
    account = snapshot.connected_account
    if isinstance(account, str) and account.strip():
        return account.strip(), True
    return None, True


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
    explained: dict[str, dict[str, int]] = {}
    for sid in _scan_runs_by_instance(root):
        broker = _instance_broker(root, sid)
        if broker is not None and broker.owned_positions:
            explained[sid] = broker.owned_positions
    net = await _fetch_net_positions()
    result = compute_fleet_contamination(net, explained, policy_blocks_starts=settings.fleet_dirty_blocks_starts)
    return FleetContamination(**result)


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
    explained: dict[str, dict[str, int]] = {}
    account_ids: dict[str, str | None] = {}
    for sid in _scan_runs_by_instance(root):
        broker = _instance_broker(root, sid)
        if broker is not None and broker.owned_positions:
            explained[sid] = broker.owned_positions
        account_ids[sid] = _instance_ledger_account_id(root, sid)
    net = await _fetch_net_positions()
    broker_account, broker_known = await _fetch_broker_connected_account()
    payload = compute_fleet_account_summary(
        net_positions=net,
        explained_by_instance=explained,
        instance_account_ids=account_ids,
        broker_connected_account=broker_account,
        broker_account_known=broker_known,
        policy_blocks_starts=settings.fleet_dirty_blocks_starts,
    )
    payload["contamination"] = FleetContamination(**payload["contamination"])
    return FleetAccountSummary(**payload)


@router.get("/{strategy_instance_id}/status", response_model=LiveInstanceStatus)
async def get_instance_status(strategy_instance_id: str) -> LiveInstanceStatus:
    """Instance control-room status: live binding (registry) + evidence + intent."""
    sid = _validate_instance_id(strategy_instance_id)
    settings = get_settings()
    root = Path(settings.live_runs_root)

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    process, live_binding = _interpret_daemon_process(daemon, root)

    runs = _scan_runs_by_instance(root).get(sid, [])
    evidence = EvidenceBinding(run_id=runs[0]["run_id"]) if runs else None
    desired = _resolve_desired_state(root, sid)
    latest_decision, decision_columns = _strategy_state(root, live_binding, runs)
    last_exit = _instance_last_exit(runs)
    readiness = _resolve_readiness(root, live_binding, runs, desired.state)
    _raw_mode = getattr(settings, "mode", None)
    configured_mode = _raw_mode if _raw_mode in ("paper", "live") else None
    safety_verdict_final = _resolve_safety_verdict_final(configured_mode)
    broker_connection_state = _broker_connection_state_from_readiness(readiness)
    broker_view = _instance_broker(root, sid)
    start_defaults = _start_defaults(root, live_binding, runs, readonly_default=_resolve_readonly_default(settings))
    sizing = _sizing(root, live_binding, runs, sid)
    action_plan = _resolve_action_plan(root, live_binding, runs)
    poisoned = bool(last_exit and last_exit.halt_trigger is not None)
    # PRD #619-C3 — daemon connectivity monitor state (619-C2) surfaces
    # via operator_surface.control_plane. ``None`` when the lifespan has
    # not installed a monitor (test mode, no daemon URL configured).
    _daemon_monitor = get_daemon_connectivity_monitor()
    control_plane_state = _daemon_monitor.state if _daemon_monitor is not None else None
    guard_state = _resolve_resume_guard_state_for(root, live_binding, runs)
    observed_at_ms = _now_ms()
    runtime_freshness = _resolve_runtime_freshness(
        root,
        live_binding,
        now_ms=observed_at_ms,
    )
    latest_mutation = _resolve_latest_mutation(root, sid)

    return LiveInstanceStatus(
        strategy_instance_id=sid,
        process=process,
        live_binding=live_binding,
        evidence_binding=evidence,
        desired_state=desired,
        readiness=readiness,
        latest_decision=latest_decision,
        decision_columns=decision_columns,
        broker=broker_view,
        start_defaults=start_defaults,
        provenance=_provenance(root, live_binding, runs),
        sizing=sizing,
        last_exit=last_exit,
        symbol=_resolve_symbol(root, live_binding, runs),
        action_plan=action_plan,
        instrument_surface=_resolve_instrument_surface(root, live_binding, runs),
        lineage=_resolve_lineage(root, live_binding, runs),
        operator_surface=compute_operator_surface(
            process=process,
            last_exit=last_exit,
            safety_verdict_final=safety_verdict_final,
            broker_connection_state=broker_connection_state,
            broker=broker_view,
            readiness=readiness,
            action_plan=action_plan,
            start_defaults=start_defaults,
            sizing=sizing,
            instance_broker_self_consistent=None,
            live_binding=live_binding,
            poisoned=poisoned,
            desired_state=desired,
            guard_state=guard_state,
            runtime_freshness=runtime_freshness,
            control_plane_state=control_plane_state,
            latest_mutation=latest_mutation,
            now_ms=observed_at_ms,
        ),
        fetched_at_ms=observed_at_ms,
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
        _result, daemon = await host_daemon_client.fetch_instance_process(
            settings.live_runner_daemon_url, _sid
        )
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
        resolve_guard_state_for=lambda live_binding, runs: _resolve_resume_guard_state_for(
            root, live_binding, runs
        ),
        resolve_runtime_freshness_for=lambda live_binding, _runs, observed_at_ms: (
            _resolve_runtime_freshness(
                root,
                live_binding,
                now_ms=observed_at_ms,
            )
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
}


def _raise_outcome_unknown(
    endpoint: Literal["deploy", "start_run", "stop_run", "emergency_flatten"],
    exc: host_daemon_client.HostDaemonOutcomeUnknownError,
) -> None:
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
) -> Literal["connected", "disconnected", "degraded", "unknown"] | None:
    """Collapse the live readiness ``broker_connection`` gate into the
    operator-surface broker-connection-state enum.

    The live readiness vector today only emits pass/fail on
    ``broker_connection`` (see ``app/engine/live/readiness.py``).  When a
    richer ``BrokerConnectionState`` channel lands on the wire, this
    helper grows to read it; ``DEGRADED`` is unreachable from the
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
    if not path.is_file():
        return []
    try:
        rows = pq.read_table(path).to_pylist()
    except (OSError, pq.lib.ArrowIOError, pq.lib.ArrowInvalid) as exc:
        logger.warning("parquet read failed for %s: %s", path, exc, exc_info=True)
        return []
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


# VCR-P3-I — Trading-day boundaries are America/New_York, not UTC. At
# the UTC boundary (00:00 UTC = 19:00 ET in winter / 20:00 ET in summer)
# bars from the ET trading session could fall on the wrong UTC date,
# making the chart-snapshot "today" view miss bars or show yesterday's
# bars under today's banner. The chart-snapshot is the only consumer in
# this module; everything else operates on bar-time milliseconds where
# the timezone is already explicit.
_NY_TZ = ZoneInfo("America/New_York")


def _today_ny() -> date:
    """Today as America/New_York date — the trading-day partition key.

    VCR-P3-I: at the UTC boundary, bars from the ET trading session
    can fall on the wrong UTC date. The chart-snapshot endpoint reads
    by trading day, so the "today" reference must be the NY trading
    date, not the UTC calendar date.
    """
    return datetime.now(_NY_TZ).date()


def _bar_to_dict(bar) -> dict:
    """Serialize an ``IbkrMinuteBar`` for the chart payload (Decimal → str)."""
    return bar.model_dump(mode="json")


def _resolve_chart_bars(
    *,
    symbol: str | None,
    resolution: str,
    day: date,
    is_today: bool,
) -> list[dict]:
    """Resolve the bars for a chart snapshot.

    Today's path consults the live aggregator (which itself replays from
    persistence on subscribe) for the freshest buffer, then falls back to
    persistence when the buffer is empty. Past-date paths consult
    persistence only — they ignore the aggregator entirely so a stopped
    daemon doesn't make yesterday's chart disappear.
    """
    if symbol is None:
        return []
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    if is_today:
        bars = LIVE_BAR_AGGREGATOR.snapshot(symbol) if resolution == "1m" else LIVE_BAR_AGGREGATOR.snapshot_5s(symbol)
        if bars:
            return [_bar_to_dict(b) for b in bars]

    persistence = LIVE_BAR_AGGREGATOR._persistence
    if persistence is None:
        return []
    # Past dates: prefer compacted Parquet; fall back to JSONL when the
    # day's first compaction hasn't run yet (still streaming or the
    # nightly compaction job hasn't fired).
    bars = persistence.read_parquet(symbol, resolution, day)
    if not bars:
        bars = persistence.replay(symbol, resolution, day)
    return [_bar_to_dict(b) for b in bars]


@router.get(
    "/{strategy_instance_id}/chart-snapshot",
    response_model=ChartSnapshotResponse,
)
async def get_chart_snapshot(
    strategy_instance_id: str,
    date_str: Annotated[str | None, Query(alias="date")] = None,
    resolution: Annotated[str, Query()] = "1m",
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
    if resolution not in ("1m", "5s"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="resolution must be '1m' or '5s'")

    settings = get_settings()
    root = Path(settings.live_runs_root)

    try:
        day = date.fromisoformat(date_str) if date_str else _today_ny()
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid date") from None

    _result, daemon = await host_daemon_client.fetch_instance_process(settings.live_runner_daemon_url, sid)
    _process, live_binding = _interpret_daemon_process(daemon, root)
    runs = _scan_runs_by_instance(root).get(sid, [])

    symbol = _resolve_symbol(root, live_binding, runs)
    is_today = day == _today_ny()

    # Today's path: nudge the live aggregator so a fresh operator session
    # after a deploy/restart starts the IBKR stream — the chart card no
    # longer hits the legacy /api/broker/bars*/snapshot endpoints that
    # used to lazily call ensure_subscribed (PR #483 review).
    if is_today and symbol is not None:
        from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

        try:
            if resolution == "1m":
                await LIVE_BAR_AGGREGATOR.ensure_subscribed(symbol)
            else:
                await LIVE_BAR_AGGREGATOR.ensure_subscribed_5s(symbol)
        except Exception as exc:
            # The stream may legitimately be unavailable (broker offline,
            # subsystem disabled). Surface as a log; the response still
            # carries has_bars=false from persistence.
            logger.info("ensure_subscribed for %s/%s declined: %s", symbol, resolution, exc)

    bars = _resolve_chart_bars(symbol=symbol, resolution=resolution, day=day, is_today=is_today)

    runs_today = _runs_active_on(runs, day, live_binding=live_binding)
    snapshot_runs: list[ChartSnapshotRun] = []
    # Color index is assigned by sort order (oldest run first) so a fresh
    # deployment doesn't shift the color of older runs.
    for color_index, run in enumerate(sorted(runs_today, key=lambda r: r.get("started_at_ms") or 0)):
        run_dir = Path(run["run_dir"])
        is_current = live_binding is not None and run["run_id"] == live_binding.run_id
        # Filter to the requested UTC day — a multi-day run otherwise leaks
        # other days' markers onto every per-date response (PR #483 review).
        trades = _filter_rows_to_utc_day(_read_parquet_rows(run_dir / "trades.parquet"), day, key="entry_time_ms")
        executions = _filter_rows_to_utc_day(_read_parquet_rows(run_dir / "executions.parquet"), day, key="ts_ms")
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
        resolution=resolution,
        has_bars=bool(bars),
        now_ms=_now_ms(),
        bars=bars,
        runs=snapshot_runs,
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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id") from exc

    # PRD #616 / #619-A §A4 — re-run the shared capability evaluator
    # immediately before the durable write so a stale status snapshot
    # cannot drive this mutation past the Resume guards. ``load_instance_context``
    # is the canonical pre-write assembler; the projection and the CLI
    # consume the same composition.
    ctx = await _load_instance_context_for_router(sid)
    action_name = body.action.value  # "pause" | "resume" | "stop"
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

    return SetInstanceDesiredStateResponse(durable=durable, actuation=actuation)


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
        sidecar_dir = _confine(artifacts_root / "live_state", sid)
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
    repo = DesiredStateRepo(sidecar_dir / _DESIRED_STATE_FILE)
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

    return SetInstanceDesiredStateResponse(durable=durable, actuation=actuation)


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
    run_dir = (
        _visible_live_run_dir(root, ctx.live_binding) if ctx.live_binding is not None else None
    )

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
    return CommandView(seq=command.seq, verb=command.verb.value)


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
    return HostRunnerActionResponse.model_validate(body_json)
