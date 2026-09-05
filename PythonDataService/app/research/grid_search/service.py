"""Grid Search orchestration — the callable interface Walk-Forward invokes per fold.

Four verbs, each usable from a FastAPI handler or a job worker:

* :func:`preflight` — validate, size, plan the run-up, and estimate; no side
  effects. Everything that can refuse a launch refuses here, before a
  durable record exists.
* :func:`launch` — preflight, freeze the data snapshot and executable
  identity into a receipt, and write the ``queued`` record. Durable the
  moment it returns.
* :func:`execute` — claim an attempt, run every cell that has no row, persist
  each batch, rank, and reach a terminal status. Cancellation keeps finished
  cells and marks the search incomplete; a stale attempt cannot write.
* :func:`presented_status` / :func:`resume_refusal` — how a stored record
  reads back (``running`` with no live job is ``interrupted``) and whether
  Finish may run it (identity and snapshot unchanged, tree not dirty).

Reference: PRD https://github.com/tim1016/learn-ai/issues/1926.
Canonical implementation: this file.
Validated against: tests/research/grid_search/test_service.py.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.engine.data.availability import check_availability
from app.engine.data.policy_store import resolve_data_roots
from app.engine.strategy.registry import _STRATEGY_REGISTRY, StrategyRegistration, public_params_schema
from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.grid_search import repository as repo
from app.research.grid_search.models import (
    CellResult,
    CellRow,
    GridSearchSpec,
    NewSearch,
    SearchOwner,
    SearchRow,
    SearchStatus,
)
from app.research.grid_search.runner import GridRunSummary, run_grid
from app.research.persistence import lifecycle
from app.research.persistence.db import run_sync, with_connection
from app.research.persistence.fence import StaleAttemptError
from app.research.sweep.eligibility import sweep_eligibility
from app.research.sweep.grid import (
    ParamRange,
    RunSpec,
    StrategyGridConfig,
    ValueListRange,
    expand_grid,
)
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from app.research.sweep.ranking import leader
from app.research.sweep.snapshot import (
    DataSnapshot,
    DataSnapshotIncompleteError,
    capture_data_snapshot,
    verify_data_snapshot,
)
from app.research.sweep.validation import GridInvalidError, WorkloadLimitError, validate_grid
from app.research.sweep.warmup import (
    RunUpExceedsRangeError,
    RunUpPlan,
    SlowestProbe,
    WarmupProbeError,
    plan_run_up,
    slowest_warmup_probe,
)
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms

logger = logging.getLogger(__name__)

MAX_TOTAL_BACKTESTS = 5_000
# Measured on the target machine (rsi_mean_reversion, SPY, 15-minute bars):
# 3 months 2.3 s, 6 months 3.2 s -> ~1.4 s fixed + 0.3 s per month read. The
# figure still included the study save this feature suppresses, so it is an
# estimate that errs long, and it is labelled one on the form.
ESTIMATE_FIXED_SECONDS = 1.4
ESTIMATE_SECONDS_PER_MONTH = 0.3
RECEIPT_SCHEMA_VERSION = 1


class GridSearchRefusal(ValueError):
    """A launch or Finish is refused for a reason the researcher can act on."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# ── Window translation (one documented rule) ─────────────────────────────


def window_dates(start_ms: int, end_ms: int) -> tuple[date, date]:
    """``[start_ms, end_ms)`` in ms UTC -> inclusive ET trading dates the engine reads.

    The start date is the ET calendar date of ``start_ms``; the end date is
    the ET calendar date of the last millisecond before ``end_ms``, so a
    window ending exactly at an ET midnight excludes that day. Expressed
    either way, the same bars are selected (pinned by test).
    """
    return et_date_at_ms(start_ms), et_date_at_ms(end_ms - 1)


def _registration(strategy_key: str) -> StrategyRegistration:
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        raise GridSearchRefusal(f"unknown strategy {strategy_key!r}", code="UNKNOWN_STRATEGY")
    eligibility = sweep_eligibility(registration)
    if not eligibility.eligible:
        raise GridSearchRefusal(
            f"strategy {strategy_key!r} cannot be swept: {', '.join(eligibility.reason_codes)}"
            + (f" ({', '.join(eligibility.offending_parameters)})" if eligibility.offending_parameters else ""),
            code="STRATEGY_NOT_SWEEPABLE",
        )
    return registration


def complete_param_ranges(registration: StrategyRegistration, ranges: Mapping[str, ParamRange]) -> dict[str, ParamRange]:
    """Every public parameter gets a range; an unticked one stays fixed at its default.

    The parameter hash is an identity over the FULL assignment, so a cell's
    identity cannot depend on which parameters the form happened to send.
    """
    schema = public_params_schema(registration)
    completed: dict[str, ParamRange] = {}
    for name, property_schema in schema.get("properties", {}).items():
        if name == "symbol":
            continue
        if name in ranges:
            completed[name] = ranges[name]
            continue
        if "default" not in property_schema:
            raise GridSearchRefusal(f"parameter {name!r} has no range and no default", code="GRID_INVALID")
        completed[name] = ValueListRange((float(property_schema["default"]),))
    unknown = sorted(set(ranges) - set(completed))
    if unknown:
        raise GridSearchRefusal(f"unknown parameter(s) for {registration.display_name}: {unknown}", code="GRID_INVALID")
    return completed


# ── Preflight ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Preflight:
    spec: GridSearchSpec
    registration: StrategyRegistration
    param_ranges: dict[str, ParamRange]
    combinations: int
    total_backtests: int
    run_up: RunUpPlan
    expected_sessions: int
    estimated_seconds: float
    roots: list[Path]
    warmup: SlowestProbe

    @property
    def data_start(self) -> date:
        return self.run_up.data_start

    @property
    def evaluation_start(self) -> date:
        return self.run_up.evaluation_start

    @property
    def evaluation_end(self) -> date:
        return self.run_up.evaluation_end


def _estimate_seconds(cells: int, data_start: date, data_end: date, workers: int = 8) -> float:
    months = max(1.0, (data_end - data_start).days / 30.4)
    per_cell = ESTIMATE_FIXED_SECONDS + ESTIMATE_SECONDS_PER_MONTH * months
    return round(cells * per_cell / workers, 1)


def preflight(spec: GridSearchSpec, *, backtests_per_combination: int = 1, roots: Sequence[Path] | None = None) -> Preflight:
    """Validate and size a search with no side effects; raise :class:`GridSearchRefusal` on anything refused."""
    registration = _registration(spec.strategy_key)
    if spec.resolution not in registration.supported_resolutions:
        raise GridSearchRefusal(
            f"{spec.strategy_key} does not support {spec.resolution!r} data", code="RESOLUTION_UNSUPPORTED"
        )
    ranges = complete_param_ranges(registration, spec.param_ranges)
    try:
        validated = validate_grid(
            registration,
            strategy_key=spec.strategy_key,
            symbol=spec.symbol,
            param_ranges=ranges,
            limit=MAX_TOTAL_BACKTESTS,
            multiplier=backtests_per_combination,
        )
    except WorkloadLimitError as exc:
        raise GridSearchRefusal(str(exc), code="WORKLOAD_LIMIT") from exc
    except GridInvalidError as exc:
        raise GridSearchRefusal(str(exc), code="GRID_INVALID") from exc

    resolved_roots = list(roots) if roots is not None else resolve_data_roots(source="polygon", adjusted=True)
    try:
        warmup = slowest_warmup_probe(spec.strategy_key, spec.symbol, ranges)
    except WarmupProbeError as exc:
        raise GridSearchRefusal(str(exc), code="WARMUP_UNMEASURABLE") from exc
    slowest = warmup.probe
    start, end = window_dates(spec.start_ms, spec.end_ms)
    try:
        run_up = plan_run_up(
            symbol=spec.symbol,
            requested_start=start,
            requested_end=end,
            required_samples=slowest.required_samples,
            bar_span_ms=slowest.bar_span_ms,
            roots=resolved_roots,
            resolution=spec.resolution,
        )
    except RunUpExceedsRangeError as exc:
        raise GridSearchRefusal(str(exc), code="RUN_UP_EXCEEDS_RANGE") from exc

    availability = check_availability(resolved_roots, spec.symbol, run_up.data_start, run_up.evaluation_end, resolution=spec.resolution)
    if not availability.is_complete:
        shown = ", ".join(day.isoformat() for day in availability.missing_days[:10])
        more = f" (+{len(availability.missing_days) - 10} more)" if len(availability.missing_days) > 10 else ""
        raise GridSearchRefusal(
            f"the lake is missing {len(availability.missing_days)} trading session(s) for {spec.symbol}: {shown}{more}; "
            "backfill them and launch again",
            code="DATA_MISSING",
        )
    total = validated.combinations * max(1, backtests_per_combination)
    return Preflight(
        spec=spec,
        registration=registration,
        param_ranges=ranges,
        combinations=validated.combinations,
        total_backtests=total,
        run_up=run_up,
        expected_sessions=availability.expected_days,
        estimated_seconds=_estimate_seconds(total, run_up.data_start, run_up.evaluation_end),
        roots=resolved_roots,
        warmup=warmup,
    )


# ── Launch ───────────────────────────────────────────────────────────────


def build_receipt(pre: Preflight, snapshot: DataSnapshot, identity: CodeIdentity) -> dict[str, Any]:
    """The immutable record a later re-run is compared against."""
    spec = pre.spec
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "execution_contract": {
            "strategy_name": spec.strategy_key,
            "symbol": spec.symbol,
            "resolution": spec.resolution,
            "fill_mode": spec.fill_mode,
            "commission_per_order": spec.commission_per_order,
            "slippage_per_share": spec.slippage_per_share,
            "initial_cash": spec.initial_cash,
            "data_policy": {"source": "polygon", "adjusted": True, "session": "regular"},
            "save_study": False,
        },
        "interval_table": {
            "requested_start_ms": spec.start_ms,
            "requested_end_ms": spec.end_ms,
            "data_start_ms": et_midnight_ms(pre.data_start),
            "evaluation_start_ms": et_midnight_ms(pre.evaluation_start),
            "evaluation_end_ms": et_day_end_ms(pre.evaluation_end),
            "warmup_policy": "uniform_run_up",
            "required_samples": pre.run_up.required_samples,
            "bar_span_ms": pre.run_up.bar_span_ms,
            "run_up_sessions": pre.run_up.run_up_sessions,
            "carved_from_range": pre.run_up.carved_from_range,
            "probed_candidates": pre.warmup.probed_candidates,
            "probe_bounded": pre.warmup.bounded,
        },
        "parameter_schema": public_params_schema(pre.registration),
        "code_identity": identity.as_dict(),
        "data_snapshot": snapshot.as_dict(),
        "data_snapshot_digest": snapshot.digest(),
        "estimated_seconds": pre.estimated_seconds,
    }


def prepare_launch(
    spec: GridSearchSpec,
    *,
    job_id: str | None,
    owner: SearchOwner | None = None,
    roots: Sequence[Path] | None = None,
    search_id: str | None = None,
    backtests_per_combination: int = 1,
    snapshot: DataSnapshot | None = None,
    identity: CodeIdentity | None = None,
) -> NewSearch:
    """Preflight and freeze the receipt — blocking disk and CPU work, no database.

    A FastAPI handler runs this off its loop and then awaits :func:`create`;
    a worker thread calls :func:`launch`, which does both. An owner that has
    already frozen a wider ``snapshot`` (a walk-forward study) passes it in,
    together with the ``identity`` it recorded: the sweep then binds its reads
    to that manifest instead of hashing its own and carries the owner's code
    identity, so every fold provably ran on the study's bytes under the
    study's code.
    """
    pre = preflight(spec, roots=roots, backtests_per_combination=backtests_per_combination)
    if snapshot is None:
        try:
            snapshot = capture_data_snapshot(
                roots=pre.roots,
                symbol=spec.symbol,
                resolution=spec.resolution,
                data_start=pre.data_start,
                data_end=pre.evaluation_end,
            )
        except DataSnapshotIncompleteError as exc:
            raise GridSearchRefusal(str(exc), code="DATA_MISSING") from exc
    else:
        _assert_snapshot_covers(snapshot, pre)
    if identity is None:
        identity = resolve_code_identity()
    return NewSearch(
        id=search_id or uuid.uuid4().hex,
        strategy_key=spec.strategy_key,
        symbol=spec.symbol,
        request=_spec_with_completed_ranges(spec, pre.param_ranges).as_request_dict(),
        receipt=build_receipt(pre, snapshot, identity),
        expected_cells=pre.combinations,
        job_id=job_id,
        owner=owner or SearchOwner(),
    )


def _assert_snapshot_covers(snapshot: DataSnapshot, pre: Preflight) -> None:
    """A supplied snapshot must be the same symbol and resolution and hold every session the sweep reads."""
    spec = pre.spec
    if (snapshot.symbol, snapshot.resolution) != (spec.symbol, spec.resolution):
        raise GridSearchRefusal(
            f"the supplied data snapshot is for {snapshot.symbol}/{snapshot.resolution}, not {spec.symbol}/{spec.resolution}",
            code="SNAPSHOT_COVERAGE",
        )
    held = set(snapshot.sessions)
    missing = [day for day in expected_sessions(pre.data_start, pre.evaluation_end) if day not in held]
    if missing:
        raise GridSearchRefusal(
            f"the supplied data snapshot does not cover {len(missing)} session(s) this sweep reads, from {missing[0].isoformat()}",
            code="SNAPSHOT_COVERAGE",
        )


def launch(spec: GridSearchSpec, **kwargs: Any) -> SearchRow:
    """Blocking :func:`prepare_launch` + :func:`create` for worker threads (Walk-Forward's per-fold sweeps)."""
    return run_sync(create(prepare_launch(spec, **kwargs)))


def _spec_with_completed_ranges(spec: GridSearchSpec, ranges: Mapping[str, ParamRange]) -> GridSearchSpec:
    return GridSearchSpec(**{**spec.__dict__, "param_ranges": dict(ranges)})


async def create(record: NewSearch) -> SearchRow:
    """Write the durable ``queued`` record on the calling loop."""
    return await with_connection(repo.create_search, record)


# ── Execute ──────────────────────────────────────────────────────────────


def load_search(search_id: str) -> tuple[SearchRow, GridSearchSpec]:
    """The stored record and its parsed spec, for a worker thread."""
    row = run_sync(with_connection(repo.get_search, search_id))
    if row is None:
        raise GridSearchRefusal(f"search {search_id} not found", code="NOT_FOUND")
    return row, GridSearchSpec.from_request_dict(row.request)


@dataclass(frozen=True)
class ExecutionOutcome:
    search_id: str
    status: str
    leader_params_hash: str | None
    summary: GridRunSummary


def execute(
    search_id: str,
    *,
    job_id: str | None,
    execute_cell: Callable[[RunSpec], CellResult],
    cancel_check: Callable[[], object] = lambda: None,
    on_phase: Callable[[str], None] = lambda phase: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_log: Callable[[str], None] = lambda message: None,
) -> ExecutionOutcome:
    """Run (or Finish) a launched search on the calling worker thread.

    ``execute_cell`` turns one candidate into a cell result; production passes
    ``engine_adapter.default_execute_cell(row, spec)`` and tests a fake, so
    this module never touches the engine HTTP layer.
    """
    row, spec = load_search(search_id)
    attempt = run_sync(with_connection(repo.claim_attempt, search_id, job_id=job_id))
    on_phase("preflight")
    existing = run_sync(with_connection(repo.existing_params_hashes, search_id))
    if existing:
        on_log(f"Finish: {len(existing)} of {row.expected_cells} cells already recorded; running the rest")
    config = StrategyGridConfig(strategy_key=spec.strategy_key, param_ranges=dict(spec.param_ranges))
    candidates = expand_grid([config], [spec.symbol])

    def _persist(cells: list[CellResult]) -> None:
        run_sync(with_connection(repo.write_cells, search_id, attempt, cells))

    on_phase("running")
    try:
        summary = run_grid(
            candidates,
            expected_cells=row.expected_cells,
            execute_cell=execute_cell,
            persist=_persist,
            cancel_check=cancel_check,
            on_progress=on_progress,
            on_cell_failed=lambda spec_, message: on_log(f"cell {spec_.params_hash[:8]} failed: {message}"),
            skip_params_hashes=frozenset(existing),
        )
    except JobCancelled:
        winner = leader(run_sync(with_connection(repo.list_all_cells, search_id)), spec.measure, min_trades=spec.min_trades)
        _finish(search_id, attempt, "cancelled", winner, True, None)
        raise
    except StaleAttemptError:
        logger.warning("grid search %s attempt %s superseded; leaving the newer attempt's record alone", search_id, attempt)
        raise
    except Exception as exc:
        _finish(search_id, attempt, "failed", None, True, f"{type(exc).__name__}: {exc}")
        raise

    on_phase("ranking")
    cells = run_sync(with_connection(repo.list_all_cells, search_id))
    winner = leader(cells, spec.measure, min_trades=spec.min_trades)
    all_failed = cells and all(cell.status == "failed" for cell in cells)
    status = "failed" if all_failed else "completed"
    reason = "every combination failed; see the cells for each error" if all_failed else None
    diagnostics = verify_data_snapshot(DataSnapshot.from_dict(row.receipt["data_snapshot"]), lifecycle.roots_for(row))
    if diagnostics:
        on_log(f"data snapshot diagnostic: {len(diagnostics)} artifact(s) changed during the search; reads were bound to receipted bytes")
    _finish(search_id, attempt, status, winner, False, reason)
    on_phase("completed")
    return ExecutionOutcome(search_id=search_id, status=status, leader_params_hash=winner.params_hash if winner else None, summary=summary)


def _finish(search_id: str, attempt: int, status: SearchStatus, winner: CellRow | None, incomplete: bool, reason: str | None) -> None:
    run_sync(
        with_connection(
            repo.finish_search,
            search_id,
            attempt,
            status=status,
            leader_params_hash=winner.params_hash if winner else None,
            leader_params=dict(winner.params) if winner else None,
            incomplete=incomplete,
            failure_reason=reason,
        )
    )

