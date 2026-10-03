"""Golden Search study service: the one interface the HTTP layer and the jobs boundary call (#2696, ADR 0074).

* :func:`capabilities`, :func:`defaults`, :func:`preflight` — read-only plan
  work; protocol problems come back as refusals in the answer, never errors.
* :func:`lock_study` — freeze a reviewed plan into a ``locked`` study,
  idempotently by its key.
* :func:`run_command` — every lifecycle command, under ``expected_revision``
  and an idempotency key; a command that authorizes a stage returns its
  ``dispatch``.
* :func:`bind_dispatch` / :func:`run_stage` — the jobs boundary binds a job
  to the authorized stage once, then runs it on a worker thread.
* :func:`detail` / :func:`summaries` / :func:`candidate` / :func:`evaluations`
  / :func:`hide` — reads and the soft delete.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import redis

from app.engine.strategy.registry import _STRATEGY_REGISTRY, SignalProgramContract
from app.research.golden_search import repository as repo
from app.research.golden_search.actions import (
    LIVE_PRESENTATIONS,
    STAGE_START_STATES,
    action_refusals,
    authorize,
    presented_status,
    unclaimed,
)
from app.research.golden_search.activity import DEFAULT_EXPECTED_TRADES_PER_YEAR, TradeFloors
from app.research.golden_search.declarations import declaration_for, point_hash, unavailable_reason
from app.research.golden_search.exposure_rules import EXPOSURE_EXPLANATIONS, claim_for, exposure_state
from app.research.golden_search.fold_charts import fold_charts
from app.research.golden_search.guidance import params_sentence, research_weakness
from app.research.golden_search.models import (
    CANDIDATE_KEYS,
    COMMANDS,
    RETAIN_KINDS,
    RUNNING_STATES,
    STAGE_STEPS,
    CommandName,
    GoldenSearchRefusal,
    StudyRow,
    require_mapping,
)
from app.research.golden_search.plan_charts import coverage_span, plan_charts
from app.research.golden_search.planning import (
    DEFAULT_FINAL_MONTHS,
    DEFAULT_TEST_MONTHS,
    DEFAULT_TRAINING_MONTHS,
    default_final_interval,
    default_protocol,
    preflight_view,
    prepare_lock,
    protocol_from_request,
    registry_incumbent,
    request_sha256,
    review_plan,
    study_id_for,
)
from app.research.golden_search.protocol import (
    DEFAULT_IMPORTANCE,
    IMPORTANCE_HIGH,
    IMPORTANCE_LOW,
    GoldenSearchProtocol,
    IncumbentRef,
)
from app.research.golden_search.search_charts import procedure_windows, search_charts
from app.research.golden_search.stages import ApprovalBinding, StageOutcome, execute_stage, stage_refusal, stage_total
from app.research.golden_search.views import candidate_detail, study_detail, study_summary
from app.research.persistence import lifecycle
from app.research.persistence.db import connection, with_connection
from app.research.sweep.identity import CodeIdentity
from app.schemas.grid_search import SYMBOL_PATTERN
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

NOUN = "study"
UNIT = "evaluation"
JOB_TYPE = "golden_search"
_SYMBOL = re.compile(SYMBOL_PATTERN)

Liveness = Callable[[str | None], bool | None]


def job_is_live(job_id: str | None) -> bool | None:
    """The default liveness: the job store, looked up on each call rather than bound when this module loads."""
    return lifecycle.job_is_live(job_id)


def _not_found(study_id: str) -> GoldenSearchRefusal:
    return GoldenSearchRefusal(f"Study {study_id} was not found.", code="NOT_FOUND", kind="not_found")


def _symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not _SYMBOL.match(symbol):
        raise GoldenSearchRefusal(f"{value!r} is not a ticker symbol.", code="SYMBOL_INVALID", field="symbol")
    return symbol


# ── Capabilities, exposure, incumbents and defaults ──────────────────────


def capabilities() -> list[dict[str, Any]]:
    """Every registered strategy with its Golden Search declaration, or the reason it has none."""
    rows = []
    for key, registration in sorted(_STRATEGY_REGISTRY.items()):
        declaration = declaration_for(key)
        reason = unavailable_reason(key)
        rows.append(
            {
                "strategy_key": key,
                "display_name": registration.display_name,
                "available": declaration is not None and reason is None,
                "reason": reason,
                "knobs": []
                if declaration is None
                else [
                    {
                        "name": knob.name,
                        "label": knob.label,
                        "unit": knob.unit,
                        "kind": knob.kind,
                        "domain_low": float(knob.domain_low),
                        "domain_high": float(knob.domain_high),
                        "quantum": float(knob.quantum),
                        "default_low": float(knob.default_low),
                        "default_high": float(knob.default_high),
                        "neighbor_step": float(knob.neighbor_step),
                        "default_step": float(knob.default_step),
                        "searchable_by_default": knob.searchable_by_default,
                        "warmup_dependent": knob.warmup_dependent,
                        "default_value": int(knob.default_value) if knob.kind == "integer" else float(knob.default_value),
                        "note": knob.note,
                    }
                    for knob in declaration.knobs
                ],
                "fixed": [] if declaration is None else [{"label": f.label, "value": f.value, "reason": f.reason} for f in declaration.fixed],
                "constraints": []
                if declaration is None
                else [{"left": c.left, "op": c.op, "right": c.right, "message": c.message} for c in declaration.constraints],
                "default_pair_audits": [] if declaration is None else [list(pair) for pair in declaration.default_pair_audits],
                "default_expected_trades_per_year": DEFAULT_EXPECTED_TRADES_PER_YEAR,
                "importance": {"low": IMPORTANCE_LOW, "high": IMPORTANCE_HIGH, "default": DEFAULT_IMPORTANCE},
            }
        )
    return rows


async def exposure_view(symbol: str, start_ms: int, end_ms: int, *, exclude_study_id: str | None = None) -> dict[str, Any]:
    """Recorded use of a final interval for a symbol, as the state, its counts and its explanation."""
    overlaps = await with_connection(
        repo.exposure_overlaps, symbol=symbol, start_ms=start_ms, end_ms=end_ms, exclude_study_id=exclude_study_id
    )
    state = exposure_state(ledger_overlaps=overlaps.ledger, outside_activity_overlaps=overlaps.outside)
    return {
        "state": state,
        "ledger_overlaps": overlaps.ledger,
        "outside_activity_overlaps": overlaps.outside,
        "explanation": EXPOSURE_EXPLANATIONS[state],
    }


def _running_artifact_digest(contract: SignalProgramContract) -> str:
    from app.services.signal_program_admission import running_artifact_digest

    return running_artifact_digest(contract)


@dataclass(frozen=True)
class IncumbentChoice:
    ref: IncumbentRef
    label: str


REGISTRY_LABEL = "Registry validated point"


async def resolve_incumbent(
    strategy_key: str,
    symbol: str,
    *,
    running_digest: Callable[[SignalProgramContract], str] = _running_artifact_digest,
) -> IncumbentChoice:
    """The active default qualification when it is ready for the running program, else the registry point.

    An unreadable qualification store falls back to the registry point and
    says so in the label; it never reports that no default exists.
    """
    from app.research.golden_search import qualifications

    registry = registry_incumbent(strategy_key, symbol)
    contract = _STRATEGY_REGISTRY[strategy_key].signal_program_contract
    if contract is None:
        return IncumbentChoice(ref=registry, label=REGISTRY_LABEL)
    try:
        async with connection() as conn:
            pointer = await qualifications.get_default(conn, strategy_key, symbol)
            if pointer is None or pointer.qualification_id is None:
                return IncumbentChoice(ref=registry, label=REGISTRY_LABEL)
            row = await qualifications.get_qualification(conn, pointer.qualification_id)
            events = (await qualifications.events_for(conn, [pointer.qualification_id])).get(pointer.qualification_id, [])
    except Exception:
        logger.warning(
            "golden search default unreadable; proposing the registry point",
            extra={"action": "golden_search_default_unreadable", "strategy_key": strategy_key, "symbol": symbol},
            exc_info=True,
        )
        return IncumbentChoice(ref=registry, label=f"{REGISTRY_LABEL} (the Golden Search default could not be read)")
    if row is None or qualifications.qualification_status(row, events, running_digest(contract)) != "ready":
        return IncumbentChoice(ref=registry, label=f"{REGISTRY_LABEL} (the Golden Search default is not ready)")
    return IncumbentChoice(
        ref=IncumbentRef(source="qualification", qualification_id=row.id, params=dict(row.params)),
        label=f"Golden configuration {row.id[:8]}",
    )


@dataclass(frozen=True)
class LakeSessions:
    """The first and last complete minute sessions the adjusted lake holds for a symbol."""

    first: date
    last: date


async def lake_sessions(symbol: str) -> LakeSessions | None:
    """The symbol's first and last complete minute sessions in the adjusted lake, or ``None`` when the catalog cannot say."""
    from app.data_lake import catalog_client
    from app.data_lake.types import polygon_mode_for
    from app.research.grid_search.service import SWEEP_DATA_POLICY

    try:
        await catalog_client.init_pool()
        spans = await catalog_client.select_symbol_coverage_spans(
            "usa", price_adjustment_mode=polygon_mode_for(adjusted=SWEEP_DATA_POLICY["adjusted"]), data_type="trade"
        )
    except Exception:
        logger.warning(
            "lake coverage unreadable; proposing the default intervals without it",
            extra={"action": "golden_search_lake_coverage_unreadable", "symbol": symbol},
            exc_info=True,
        )
        return None
    for span in spans:
        if span.symbol.upper() == symbol and span.first_trading_date_ms is not None and span.last_trading_date_ms is not None:
            return LakeSessions(first=et_date_at_ms(span.first_trading_date_ms), last=et_date_at_ms(span.last_trading_date_ms))
    return None


async def defaults(
    strategy_key: str,
    symbol: str,
    *,
    final_months: int = DEFAULT_FINAL_MONTHS,
    training_months: int = DEFAULT_TRAINING_MONTHS,
    test_months: int = DEFAULT_TEST_MONTHS,
    now_ms: int | None = None,
    lake_coverage: Callable[[str], Awaitable[LakeSessions | None]] = lake_sessions,
    running_digest: Callable[[SignalProgramContract], str] = _running_artifact_digest,
) -> dict[str, Any]:
    """A complete starting plan with the intervals computed here, the incumbent's name and settings, and the final interval's exposure."""
    symbol = _symbol(symbol)
    declaration = declaration_for(strategy_key)
    if declaration is None:
        reason = unavailable_reason(strategy_key) or "No Golden Search declaration exists for this strategy."
        raise GoldenSearchRefusal(reason, code="STRATEGY_UNAVAILABLE", field="strategy_key")
    incumbent = await resolve_incumbent(strategy_key, symbol, running_digest=running_digest)
    coverage = await lake_coverage(symbol)
    now = now_ms_utc() if now_ms is None else now_ms
    latest = None if coverage is None else coverage.last
    protocol = default_protocol(
        strategy_key,
        symbol,
        incumbent.ref,
        now_ms=now,
        earliest_session=None if coverage is None else coverage.first,
        latest_session=latest,
        final_months=final_months,
        training_months=training_months,
        test_months=test_months,
    )
    # The plan itself, plus its annotations: a client edits it and sends it back as a ProtocolRequest.
    return {
        **protocol.as_dict(),
        "final_months": final_months,
        "final_sessions_cut": default_final_interval(now, final_months, latest).sessions_cut,
        "incumbent_label": incumbent.label,
        "incumbent_sentence": params_sentence(declaration, incumbent.ref.params),
        "exposure": await exposure_view(symbol, protocol.final_start_ms, protocol.final_end_ms),
    }


async def preflight(request: Mapping[str, Any], *, roots: Sequence[Path] | None = None) -> dict[str, Any]:
    """Review a plan with no side effects: refusals, estimate, folds, exposure and run-up."""
    protocol = protocol_from_request(request)
    plan = await asyncio.to_thread(review_plan, protocol, roots=roots)
    exposure = (
        await exposure_view(protocol.symbol, protocol.final_start_ms, protocol.final_end_ms)
        if protocol.final_start_ms < protocol.final_end_ms
        else None
    )
    return preflight_view(plan, exposure)


# ── Lock ─────────────────────────────────────────────────────────────────


def _idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise GoldenSearchRefusal("An idempotency key of 1 to 200 characters is required.", code="IDEMPOTENCY_KEY_INVALID")
    return value


async def lock_study(
    request: Mapping[str, Any],
    *,
    idempotency_key: str,
    roots: Sequence[Path] | None = None,
    identity: CodeIdentity | None = None,
) -> StudyRow:
    """Lock a plan into a new study; the same key and plan return the study it already locked."""
    key = _idempotency_key(idempotency_key)
    protocol = protocol_from_request(request)
    # A retried lock finds the study its key already wrote before redoing the snapshot work.
    existing = await _locked_by(key, request_sha256(protocol.as_dict()))
    if existing is not None:
        return existing
    new = await asyncio.to_thread(prepare_lock, protocol, idempotency_key=key, roots=roots, identity=identity)
    row, command = await with_connection(repo.insert_study, new)
    if command.request_sha256 != new.request_sha256:
        raise _lock_conflict(row)
    logger.info("golden search study locked", extra={"action": "golden_search_study_locked", "study_id": row.id})
    return row


def _run_at_lock_key(lock_key: str) -> str:
    """The run command a lock-and-run records on the study its lock key locked: derived, so a retry finds the same
    run, and hashed into a namespace no 1-200 character caller key can equal (#2814 review)."""
    return "run-research:" + hashlib.sha256(lock_key.encode("utf-8")).hexdigest()


async def lock_and_run(
    request: Mapping[str, Any],
    *,
    idempotency_key: str,
    roots: Sequence[Path] | None = None,
    identity: CodeIdentity | None = None,
    liveness: Liveness = job_is_live,
) -> CommandOutcome:
    """Run research (#2811): lock the plan and authorize Search with the intent to run on to Compare.

    Both halves are idempotent: a retry after a lost response finds the study
    its key locked and the run already recorded, and re-offers the dispatch
    while no worker has claimed it.
    """
    row = await lock_study(request, idempotency_key=idempotency_key, roots=roots, identity=identity)
    return await run_command(
        row.id,
        command="run_research",
        expected_revision=0,
        idempotency_key=_run_at_lock_key(idempotency_key),
        roots=roots,
        liveness=liveness,
        identity=identity,
    )


def _lock_conflict(row: StudyRow | None) -> GoldenSearchRefusal:
    return GoldenSearchRefusal("This idempotency key already locked a different plan.", code="IDEMPOTENCY_CONFLICT", kind="conflict", study=row)


async def _locked_by(key: str, digest: str) -> StudyRow | None:
    study_id = study_id_for(key)
    async with connection() as conn:
        command = await repo.get_command(conn, study_id, key)
        if command is None:
            return None
        row = await repo.get_study(conn, study_id)
    if command.request_sha256 != digest:
        raise _lock_conflict(row)
    return row


# ── Reads ────────────────────────────────────────────────────────────────


async def get_row(study_id: str) -> StudyRow:
    row = await with_connection(repo.get_study, study_id)
    if row is None:
        raise _not_found(study_id)
    return row


def _live(row: StudyRow, liveness: Liveness) -> bool | None:
    return liveness(row.job_id) if row.status in LIVE_PRESENTATIONS and row.job_id is not None else False


def _resume_refusal(
    row: StudyRow, *, live: bool | None, identity: CodeIdentity | None = None, verify_data: bool = False
) -> str | None:
    return lifecycle.resume_refusal(row, noun=NOUN, unit=UNIT, live=live, identity=identity, verify_data=verify_data)


@dataclass(frozen=True)
class _Presentation:
    presented: str
    refusals: dict[CommandName, str | None]


def _present(row: StudyRow, *, liveness: Liveness, identity: CodeIdentity | None, verify_data: bool = False) -> _Presentation:
    """Blocking: Redis liveness and the code identity behind Finish and every command that starts a stage."""
    live = _live(row, liveness)
    presented = presented_status(row, live=live)
    stopped = row.state in RUNNING_STATES and presented in ("failed", "cancelled", "interrupted")
    resume = _resume_refusal(row, live=live, identity=identity, verify_data=verify_data) if stopped else None
    moved = stage_refusal(row, identity=identity) if row.state in STAGE_START_STATES else None
    return _Presentation(
        presented=presented, refusals=action_refusals(row, presented=presented, resume_refusal=resume, stage_refusal=moved)
    )


async def _progress(row: StudyRow, presented: str) -> dict[str, Any] | None:
    stage = row.pending_stage
    if stage is None or row.state not in RUNNING_STATES or presented not in LIVE_PRESENTATIONS:
        return None
    if stage == "qualification":
        completed = await with_connection(repo.consumed_outside_evaluator, row.id, "proof")
    else:
        completed = await with_connection(repo.count_recorded_evaluations, row.id, STAGE_STEPS[stage])
    return {"stage": stage, "completed": completed, "total_max": stage_total(row, stage)}


async def detail(
    row: StudyRow,
    *,
    dispatch: Mapping[str, Any] | None = None,
    liveness: Liveness = job_is_live,
    identity: CodeIdentity | None = None,
) -> dict[str, Any]:
    """The StudyDetail read model for one row."""
    presentation = await asyncio.to_thread(_present, row, liveness=liveness, identity=identity)
    return study_detail(
        row,
        presented=presentation.presented,
        refusals=presentation.refusals,
        progress=await _progress(row, presentation.presented),
        dispatch=dispatch,
        exposure_preview=await _exposure_preview(row),
    )


async def _exposure_preview(row: StudyRow) -> dict[str, Any] | None:
    """What opening the final test would record, while a candidate is being chosen; the exam records its own state."""
    if row.exam_locked or row.state not in ("awaiting_candidate", "candidate_locked"):
        return None
    protocol = row.protocol
    return await exposure_view(row.symbol, protocol["final_start_ms"], protocol["final_end_ms"], exclude_study_id=row.id)


async def summaries(
    *,
    strategy_key: str | None = None,
    symbol: str | None = None,
    include_hidden: bool = False,
    limit: int = 100,
    liveness: Liveness = job_is_live,
) -> list[dict[str, Any]]:
    rows = await with_connection(
        repo.list_studies,
        strategy_key=strategy_key,
        symbol=None if symbol is None else symbol.strip().upper(),
        include_hidden=include_hidden,
        limit=limit,
    )

    def present_all() -> list[str]:
        return [presented_status(row, live=_live(row, liveness)) for row in rows]

    presented = await asyncio.to_thread(present_all)
    return [study_summary(row, presented=status) for row, status in zip(rows, presented, strict=True)]


async def candidate(study_id: str, candidate_key: str) -> dict[str, Any]:
    """The candidate's development detail run, and its final-test run once the exam scored it."""
    row = await get_row(study_id)
    stored = next(
        (item for item in (row.results.get("evidence") or {}).get("candidates", []) if item["key"] == candidate_key), None
    )
    if stored is None:
        raise GoldenSearchRefusal(f"This study has no {candidate_key} candidate yet.", code="CANDIDATE_UNAVAILABLE", kind="not_found")
    protocol = row.protocol
    development = await with_connection(
        repo.find_detail_evaluation,
        row.id,
        point_hash=stored["point_hash"],
        window_start_ms=protocol["development_start_ms"],
        window_end_ms=protocol["development_end_ms"],
    )
    exam_record = None
    exam = row.results.get("exam") or {}
    examined = {exam.get("candidate_point_hash"), point_hash(row.strategy_key, protocol["incumbent"]["params"])}
    if exam.get("outcome") is not None and stored["point_hash"] in examined:
        exam_record = await with_connection(
            repo.find_detail_evaluation,
            row.id,
            point_hash=stored["point_hash"],
            window_start_ms=protocol["final_start_ms"],
            window_end_ms=protocol["final_end_ms"],
        )
    frozen = GoldenSearchProtocol.from_dict(protocol)
    return candidate_detail(
        stored,
        strategy_key=row.strategy_key,
        development=development,
        exam=exam_record,
        commission_per_order=protocol["execution"]["commission_per_order"],
        floors=TradeFloors(frozen, row.receipt),
        development_window=(frozen.development_start_ms, frozen.development_end_ms),
    )


LAKE_UNREADABLE = "The data lake catalog could not be read, so coverage is not shown."


async def _lake_statuses(row: StudyRow) -> dict[date, str] | str:
    """Each session's minute-bar artifact status over the study's data span, or why the catalog could not say."""
    from app.data_lake import catalog_client
    from app.data_lake.ensure_data import provider_for_data_type
    from app.data_lake.types import polygon_mode_for

    start, end = coverage_span(row)
    adjusted = bool(row.receipt["execution_contract"]["data_policy"]["adjusted"])
    try:
        await catalog_client.init_pool()
        rows = await catalog_client.select_artifact_coverage(
            market="usa",
            symbol=row.symbol,
            data_type="trade",
            provider=provider_for_data_type("trade"),
            price_adjustment_mode=polygon_mode_for(adjusted=adjusted),
            start_trading_date=start,
            end_trading_date=end,
        )
    except Exception:
        logger.warning(
            "lake coverage unreadable; the plan's coverage chart says so",
            extra={"action": "golden_search_plan_coverage_unreadable", "study_id": row.id},
            exc_info=True,
        )
        return LAKE_UNREADABLE
    return {item.trading_date: item.status for item in rows}


async def plan_step_charts(study_id: str) -> dict[str, Any]:
    """The Plan step's charts for a locked study: its frozen windows, search space, workload, trade minimums and data coverage."""
    row = await get_row(study_id)
    reserved = await with_connection(repo.reserved_by_stage, row.id)
    return plan_charts(row, reserved=reserved, statuses=await _lake_statuses(row))


async def search_step_charts(study_id: str) -> dict[str, Any]:
    """The Search step's charts: each recorded procedure's path, knob moves and profiles, and every point it scored."""
    row = await get_row(study_id)
    evaluations = {}
    for start, end in procedure_windows(row):
        evaluations[(start, end)] = await with_connection(repo.window_evaluations, row.id, window_start_ms=start, window_end_ms=end)
    return search_charts(row, evaluations)


async def test_over_time_charts(study_id: str) -> dict[str, Any]:
    """The Test over time step's charts, from the stored folds or, before they ran, the receipt's plan."""
    return fold_charts(await get_row(study_id))


async def evaluations(study_id: str, *, stage: str | None = None, fold_index: int | None = None, page: int = 1, page_size: int = 50) -> dict[str, Any]:
    await get_row(study_id)
    try:
        result = await with_connection(repo.list_evaluations, study_id, stage=stage, fold_index=fold_index, page=page, page_size=page_size)
    except ValueError as exc:
        raise GoldenSearchRefusal(str(exc), code="PAGE_INVALID") from exc
    return result.as_dict()


async def hide(study_id: str, *, liveness: Liveness = job_is_live) -> None:
    """Hide a study from history; refused while a stage runs. Rows, trials and exposures stay."""
    row = await get_row(study_id)
    presented = await asyncio.to_thread(lambda: presented_status(row, live=_live(row, liveness)))
    if row.state in RUNNING_STATES and presented in LIVE_PRESENTATIONS:
        raise GoldenSearchRefusal("A study cannot be hidden while a stage runs; cancel it first.", code="STUDY_RUNNING", kind="conflict", study=row)
    await with_connection(repo.hide_study, study_id)


# ── Commands ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandOutcome:
    """The study the response describes (the new study for ``revise``) and the stage to dispatch, if any."""

    study: StudyRow
    dispatch: dict[str, Any] | None
    replayed: bool = False


def _dispatch(study_id: str, token: str) -> dict[str, Any]:
    return {"job_type": JOB_TYPE, "payload": {"study_id": study_id, "stage_token": token}}


async def _replayed(study_id: str, idempotency_key: str, digest: str) -> CommandOutcome | None:
    async with connection() as conn:
        record = await repo.get_command(conn, study_id, idempotency_key)
        if record is None:
            return None
        if record.request_sha256 != digest:
            current = await repo.get_study(conn, study_id)
            raise GoldenSearchRefusal(
                "This idempotency key was already used for a different request.",
                code="IDEMPOTENCY_CONFLICT",
                kind="conflict",
                study=current,
            )
        target = record.response.get("study_id", study_id)
        row = await repo.get_study(conn, target)
    if row is None:
        raise _not_found(target)
    stored = record.response.get("dispatch")
    # The original dispatch is offered again only while its stage still waits for a worker.
    still_waiting = stored is not None and unclaimed(row) and row.stage_token == stored["payload"]["stage_token"]
    return CommandOutcome(study=row, dispatch=stored if still_waiting else None, replayed=True)


def _stale(row: StudyRow) -> GoldenSearchRefusal:
    return GoldenSearchRefusal(
        f"The study moved on to revision {row.revision}; review it and try again.", code="STALE_REVISION", kind="conflict", study=row
    )


async def run_command(
    study_id: str,
    *,
    command: str,
    expected_revision: int,
    idempotency_key: str,
    payload: Mapping[str, Any] | None = None,
    roots: Sequence[Path] | None = None,
    liveness: Liveness = job_is_live,
    identity: CodeIdentity | None = None,
) -> CommandOutcome:
    """Apply one lifecycle command under its expected revision; a repeat of a recorded request returns its outcome."""
    if command not in COMMANDS:
        raise GoldenSearchRefusal(f"Unknown command {command!r}.", code="UNKNOWN_COMMAND", field="command")
    key = _idempotency_key(idempotency_key)
    body = dict(require_mapping(payload if payload is not None else {}, "payload"))
    try:
        digest = request_sha256({"command": command, "expected_revision": expected_revision, "payload": body})
    except (TypeError, ValueError) as exc:
        raise GoldenSearchRefusal(f"The payload is not plain JSON: {exc}", code="PAYLOAD_INVALID", field="payload") from exc
    replay = await _replayed(study_id, key, digest)
    if replay is not None:
        return replay
    seen = await get_row(study_id)
    if seen.revision != expected_revision:
        raise _stale(seen)
    presentation = await asyncio.to_thread(_present, seen, liveness=liveness, identity=identity, verify_data=command in ("finish", "run_research"))
    reason = presentation.refusals[command]
    if reason is not None:
        raise GoldenSearchRefusal(reason, code="COMMAND_NOT_PERMITTED", kind="conflict", study=seen)
    prepared = None
    if command == "revise":
        protocol = protocol_from_request(require_mapping(body.get("protocol"), "payload.protocol"))
        if (protocol.strategy_key, protocol.symbol) != (seen.strategy_key, seen.symbol):
            raise GoldenSearchRefusal(
                f"A revision studies the same strategy and stock ({seen.strategy_key} on {seen.symbol}); start a new study for another.",
                code="REVISION_SUBJECT_CHANGED",
                field="payload.protocol",
            )
        prepared = await asyncio.to_thread(prepare_lock, protocol, idempotency_key=key, parent_study_id=study_id, roots=roots, identity=identity)
    async with connection() as conn, conn.transaction():
        # Commands on one study serialize on its row lock, so the key is checked once the lock is held:
        # a concurrent twin (a revise, which moves no revision) is refused here, not by the key's primary key.
        row = await repo.lock_study(conn, study_id)
        if row is None:
            raise _not_found(study_id)
        if await repo.get_command(conn, study_id, key) is not None:
            raise GoldenSearchRefusal(
                "A concurrent request used this idempotency key.", code="IDEMPOTENCY_CONFLICT", kind="conflict", study=row
            )
        if (row.revision, row.status, row.job_id, row.attempt) != (seen.revision, seen.status, seen.job_id, seen.attempt):
            raise _stale(row)
        outcome = await _apply(conn, row, command, body, prepared=prepared)
        response: dict[str, Any] = (
            {"study_id": outcome.study.id}
            if command == "revise"
            else {"revision": outcome.study.revision, "dispatch": outcome.dispatch}
        )
        await repo.record_command(
            conn, study_id=study_id, idempotency_key=key, command=command, request_sha256=digest, response=response
        )
    logger.info(
        "golden search command applied",
        extra={"action": "golden_search_command", "study_id": study_id, "command": command, "revision": outcome.study.revision},
    )
    return outcome


async def _apply(conn: Any, row: StudyRow, command: str, body: Mapping[str, Any], *, prepared: Any) -> CommandOutcome:
    if command in ("continue", "run_research"):
        # Run research resumes a stopped stage or starts the next; either way the study then runs on to Compare.
        stage = row.pending_stage if row.state in RUNNING_STATES else ("search" if row.state == "locked" else "validation")
        assert stage is not None  # a stopped stage keeps the stage it was authorized for
        changes, token = authorize(stage)
        changes["run_to_compare"] = command == "run_research"
        return CommandOutcome(study=await repo.update_study(conn, row.id, changes=changes), dispatch=_dispatch(row.id, token))
    if command == "select_candidate":
        return await _select_candidate(conn, row, body)
    if command == "open_exam":
        return await _open_exam(conn, row, body)
    if command == "approve":
        return await _approve(conn, row, body)
    if command in ("retain", "close"):
        return await _decide(conn, row, command, body)
    if command == "cancel":
        # A cancel also ends Run research: resuming it is a new, explicit command.
        if unclaimed(row):
            changes: dict[str, Any] = {"status": "cancelled", "stage_token": None, "incomplete": True, "run_to_compare": False}
        else:
            # Delivered before the command is recorded: an unreachable job store records nothing.
            await _request_cancel(str(row.job_id))
            changes = {"run_to_compare": False}
        return CommandOutcome(study=await repo.update_study(conn, row.id, changes=changes), dispatch=None)
    if command == "finish":
        stage = row.pending_stage
        assert stage is not None  # a stopped stage keeps the stage it was authorized for
        changes, token = authorize(stage)
        return CommandOutcome(study=await repo.update_study(conn, row.id, changes=changes), dispatch=_dispatch(row.id, token))
    created, _ = await repo.insert_study(conn, prepared)
    return CommandOutcome(study=created, dispatch=None)


async def _request_cancel(job_id: str) -> None:
    try:
        await asyncio.to_thread(lifecycle.request_cancel, job_id)
    except redis.RedisError as exc:
        raise GoldenSearchRefusal(
            "The job store is unreachable, so the cancel could not be delivered; try again shortly.",
            code="JOB_STORE_UNREACHABLE",
            kind="unavailable",
        ) from exc


def _evidence_candidate(row: StudyRow, key: str) -> Mapping[str, Any] | None:
    return next((item for item in (row.results.get("evidence") or {}).get("candidates", []) if item["key"] == key), None)


async def _select_candidate(conn: Any, row: StudyRow, body: Mapping[str, Any]) -> CommandOutcome:
    key = body.get("candidate_key")
    if key not in CANDIDATE_KEYS:
        raise GoldenSearchRefusal(f"Choose one of {', '.join(CANDIDATE_KEYS)}.", code="PAYLOAD_INVALID", field="candidate_key")
    chosen = _evidence_candidate(row, str(key))
    if chosen is None:
        raise GoldenSearchRefusal(f"This study has no {key} candidate.", code="CANDIDATE_UNAVAILABLE", field="candidate_key")
    incumbent = _evidence_candidate(row, "incumbent")
    if key == "incumbent" or (incumbent is not None and chosen["point_hash"] == incumbent["point_hash"]):
        raise GoldenSearchRefusal(
            "Use Keep current settings to finish without consuming the test.", code="INCUMBENT_NOT_EXAMINABLE", field="candidate_key"
        )
    await repo.insert_trial(conn, row.id, stage="candidate", kind="pick", payload={"candidate_key": key, "point_hash": chosen["point_hash"]})
    updated = await repo.update_study(conn, row.id, changes={"candidate_key": key, "state": "candidate_locked"})
    return CommandOutcome(study=updated, dispatch=None)


async def _open_exam(conn: Any, row: StudyRow, body: Mapping[str, Any]) -> CommandOutcome:
    if body.get("acknowledge_final_test") is not True:
        raise GoldenSearchRefusal(
            "Confirm that opening the final test consumes it for this candidate.",
            code="ACKNOWLEDGEMENT_REQUIRED",
            field="acknowledge_final_test",
        )
    chosen = _evidence_candidate(row, str(row.candidate_key))
    assert chosen is not None  # candidate_locked implies a chosen evidence candidate
    protocol = row.protocol
    start, end = int(protocol["final_start_ms"]), int(protocol["final_end_ms"])
    await repo.lock_exposure(conn, row.symbol)
    overlaps = await repo.exposure_overlaps(conn, symbol=row.symbol, start_ms=start, end_ms=end, exclude_study_id=row.id)
    state = exposure_state(ledger_overlaps=overlaps.ledger, outside_activity_overlaps=overlaps.outside)
    claim = claim_for(state)
    await repo.insert_exposure(
        conn,
        symbol=row.symbol,
        start_ms=start,
        end_ms=end,
        study_id=row.id,
        strategy_key=row.strategy_key,
        kind="reserved",
        state_at_reservation=state,
        claim=claim,
        candidate_point_hash=chosen["point_hash"],
        payload={
            "candidate_key": row.candidate_key,
            "candidate_point": chosen["point"],
            "protocol_hash": row.protocol_hash,
            "ledger_overlaps": overlaps.ledger,
            "outside_activity_overlaps": overlaps.outside,
        },
    )
    await repo.insert_trial(conn, row.id, stage="exam", kind="exam_open", payload={"candidate_key": row.candidate_key, "state": state, "claim": claim})
    changes, token = authorize("exam")
    exam = {
        "candidate_key": row.candidate_key,
        "candidate_point": dict(chosen["point"]),
        "candidate_point_hash": chosen["point_hash"],
        "window": {"start_ms": start, "end_ms": end},
        "claim": claim,
        "exposure_state": state,
        "ledger_overlaps": overlaps.ledger,
        "outside_activity_overlaps": overlaps.outside,
        "outcome": None,
        "checks": [],
        "retention": None,
        "candidate_metrics": None,
        "incumbent_metrics": None,
    }
    updated = await repo.update_study(conn, row.id, changes={**changes, "exam_locked": True}, results_patch={"exam": exam})
    return CommandOutcome(study=updated, dispatch=_dispatch(row.id, token))


async def _approve(conn: Any, row: StudyRow, body: Mapping[str, Any]) -> CommandOutcome:
    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        raise GoldenSearchRefusal("Write a note for the approval record.", code="NOTE_REQUIRED", field="note")
    if body.get("acknowledge_missing_parity") is not True:
        raise GoldenSearchRefusal(
            "Acknowledge that independent engine (LEAN) parity is missing for this approval.",
            code="ACKNOWLEDGEMENT_REQUIRED",
            field="acknowledge_missing_parity",
        )
    weak_ack = body.get("acknowledge_research_weakness")
    if not isinstance(weak_ack, bool):
        raise GoldenSearchRefusal("acknowledge_research_weakness must be true or false.", code="PAYLOAD_INVALID", field="acknowledge_research_weakness")
    if "expected_default_qualification_id" not in body or not isinstance(body["expected_default_qualification_id"], str | None):
        raise GoldenSearchRefusal(
            "Name the default you reviewed against (or null when there is none).",
            code="PAYLOAD_INVALID",
            field="expected_default_qualification_id",
        )
    exam = row.results["exam"]
    weakness = research_weakness(exam["outcome"], exam["claim"], exam["exposure_state"])
    if weakness and not weak_ack:
        raise GoldenSearchRefusal(
            "This evidence is weak (" + ", ".join(weakness) + "); approving needs your explicit acceptance of it.",
            code="WEAKNESS_ACKNOWLEDGEMENT_REQUIRED",
            field="acknowledge_research_weakness",
        )
    previous = row.decision or {}
    decision = {
        "kind": "approve",
        "note": note.strip(),
        "at_ms": now_ms_utc(),
        "actor": "owner",
        "acknowledge_missing_parity": True,
        "acknowledge_research_weakness": weak_ack,
        "expected_default_qualification_id": body["expected_default_qualification_id"],
        "weakness": weakness,
        # A retried approval reuses the proof and saved run it already built for this exact candidate.
        "checkpoint": previous.get("checkpoint") if previous.get("kind") == "approve" else None,
    }
    await repo.insert_trial(conn, row.id, stage="qualification", kind="approval", payload={"event": "intent", "weakness": weakness, "override": bool(weakness)})
    changes, token = authorize("qualification")
    updated = await repo.update_study(conn, row.id, changes=changes, decision=decision)
    return CommandOutcome(study=updated, dispatch=_dispatch(row.id, token))


async def _decide(conn: Any, row: StudyRow, command: str, body: Mapping[str, Any]) -> CommandOutcome:
    note = body.get("note", "")
    if not isinstance(note, str):
        raise GoldenSearchRefusal("The note must be text.", code="PAYLOAD_INVALID", field="note")
    if command == "retain":
        kind = body.get("kind")
        if kind not in RETAIN_KINDS:
            raise GoldenSearchRefusal(f"Choose one of {', '.join(RETAIN_KINDS)}.", code="PAYLOAD_INVALID", field="kind")
        state = "retained"
    else:
        kind, state = "close", "closed"
    decision = {"kind": kind, "note": note.strip(), "at_ms": now_ms_utc()}
    await repo.insert_trial(conn, row.id, stage="decision", kind="decision", payload=decision)
    changes: dict[str, Any] = {"state": state, "pending_stage": None, "stage_token": None}
    if row.state in RUNNING_STATES:
        # Closing a stopped stage seals its attempt: a worker presented as interrupted but still alive
        # must not move a closed study back into the lifecycle (the fence refuses a completed record).
        changes["status"] = "completed"
    updated = await repo.update_study(conn, row.id, changes=changes, decision=decision)
    return CommandOutcome(study=updated, dispatch=None)


# ── The jobs boundary ────────────────────────────────────────────────────


async def bind_dispatch(study_id: str, *, stage_token: str, job_id: str) -> str:
    """Bind a job to the authorized stage; ``"bound"`` the first time, ``"redelivery"`` for the same job again."""
    outcome = await with_connection(repo.bind_dispatch, study_id, stage_token=stage_token, job_id=job_id)
    if outcome in ("bound", "redelivery"):
        return outcome
    if outcome == "not_found":
        raise _not_found(study_id)
    if outcome == "mismatch":
        raise GoldenSearchRefusal("This stage token was not issued for the study's pending stage.", code="STAGE_TOKEN_MISMATCH", kind="conflict")
    raise GoldenSearchRefusal("The study has no stage waiting for a worker.", code="NOTHING_PENDING", kind="conflict")


def run_stage(
    study_id: str,
    *,
    stage_token: str,
    job_id: str,
    execute: Callable[..., Any] | None = None,
    approval: ApprovalBinding | None = None,
    blob_store: Any | None = None,
    roots: Sequence[Path] | None = None,
    cancel_check: Callable[[], object] = lambda: None,
    on_phase: Callable[[str], None] = lambda phase: None,
    on_progress: Callable[[int, int], None] = lambda done, total: None,
    on_log: Callable[[str], None] = lambda message: None,
    identity: CodeIdentity | None = None,
) -> StageOutcome:
    """Run the bound stage on the calling worker thread (``stages.execute_stage``)."""
    return execute_stage(
        study_id,
        stage_token=stage_token,
        job_id=job_id,
        execute=execute,
        approval=approval,
        blob_store=blob_store,
        roots=roots,
        cancel_check=cancel_check,
        on_phase=on_phase,
        on_progress=on_progress,
        on_log=on_log,
        identity=identity,
    )

