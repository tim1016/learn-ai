"""Golden Search plan-time work: read a requested plan, size it, plan its run-up, freeze it at lock, propose defaults.

Formula (common run-up): the slowest warmup requirement is measured over
the plan's whole legal search space — every warmup-dependent knob at its
seed, incumbent, range ends and one neighbor step past the upper end and
past the seed (clipped to the domain), in every combination the declared constraints
allow — by the real program (``app.research.sweep.warmup``). The canonical
planner (``plan_run_up``) turns it into whole sessions before the
development start; a plan whose run-up would have to be carved from the
scored range is refused, because a carved start would shift the development
interval: ``RUN_UP_HISTORY_MISSING`` when the lake does not hold those
sessions, ``DATA_UNREADABLE`` when it holds them and a run cannot read
them. Every evaluation window then reads
the same number of sessions before its start, widened to the deepest
window's need so a window behind an early close is still fully primed.

Formula (default intervals): the final interval is the last ``final_months``
whole ET calendar months ending at the first day of the current ET month
(completed sessions only). When the lake's last complete session falls
inside it and at most ``FINAL_TAIL_CUT_SESSIONS`` scheduled NYSE sessions
follow it before that boundary, the interval ends after that session
instead; a lake further behind keeps the whole months, so the availability
check refuses the plan and names the backfill. Development is the longest whole-month range
ending at the final start that (a) plans whole walk-forward folds, (b)
leaves at least one calendar month of unbroken lake history before it for
the run-up (24 months when the lake's sessions are unknown), and (c) keeps
the default plan's conservative evaluation bound within its budget.

Formula (a picked range): the final interval ends where the range ends and
nominally starts ``final_months`` months before. Development starts on the
earliest day ``training_months + k·test_months`` months before that nominal
start (``k ≥ 1``) that falls inside the range and leaves the first month of
the lake's unbroken history before it for the run-up; the lake's run is the
one that reaches the range's last day. Development ends, and the final
interval starts, that many whole months after the start: on the nominal day,
or up to three days before it when the start was clamped to a shorter
month's end (a range ending on the 29th to the 31st). Scheduled sessions
between the range's first day and the development start are left out and
reported; a range that holds no fold is refused.
The run cap does not shorten a picked range: the plan check judges it.

Formula (default ranges): a knob's range is its declared default range
widened to hold the seed's and the incumbent's values, each end rounded
outward onto the quantum and clamped to the domain, so the range holds
where the search starts and the benchmark it must beat.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Plan before
  seeing results", "Open one final test"; ``app/research/sweep/warmup.py``.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_planning.py.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from itertools import product
from pathlib import Path
from typing import Any

from app.data_lake.polygon_fetcher import polygon_history_floor
from app.data_lake.types import polygon_mode_for, trading_date_to_calendar_anchor_ms
from app.engine.data.availability import (
    AvailabilityReport,
    MissingSessionsError,
    check_availability,
    unbroken_run_start,
)
from app.engine.data.policy_store import resolve_data_roots
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_start_for_bar_count
from app.research.golden_search.activity import DEFAULT_EXPECTED_TRADES_PER_YEAR, activity_plan
from app.research.golden_search.budget import ProtocolReview, estimate, review_protocol
from app.research.golden_search.declarations import (
    SearchDeclaration,
    SearchKnob,
    canonical_point,
    declaration_for,
    knob_values,
    quantize,
    scalar,
    to_decimal,
    unavailable_reason,
    violates,
)
from app.research.golden_search.evaluator import context_digest, execution_context, warmup_session
from app.research.golden_search.models import GoldenSearchRefusal, NewStudy
from app.research.golden_search.procedure_history import fold_windows
from app.research.golden_search.protocol import (
    DEFAULT_IMPORTANCE,
    BackfillRemedy,
    GoldenSearchProtocol,
    IncumbentRef,
    KnobPlan,
    ProtocolRefusal,
    SelectionPolicy,
    by_importance,
    canonical_json,
    knob_value_counts,
    recent_window_ms,
)
from app.research.grid_search.service import (
    SWEEP_DATA_POLICY,
    GridSearchRefusal,
    current_code_identity,
    data_missing_refusal,
)
from app.research.sweep.identity import CodeIdentity
from app.research.sweep.snapshot import capture_data_snapshot
from app.research.sweep.warmup import (
    RunUpExceedsRangeError,
    WarmupProbe,
    WarmupProbeError,
    plan_run_up,
    probe_warmup_samples,
)
from app.research.walk_forward_study.folds import add_months
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

RECEIPT_SCHEMA_VERSION = 1
DEFAULT_FINAL_MONTHS = 3
DEFAULT_TRAINING_MONTHS = 6
DEFAULT_TEST_MONTHS = 2
UNKNOWN_HISTORY_MONTHS = 24
_SCAN_DAYS = 14
#: The most trailing scheduled sessions a default final test drops when the lake has not reached them yet: one
#: trading week of backfill lag. A lake further behind is a backfill to name, not a shorter final test.
FINAL_TAIL_CUT_SESSIONS = 5


def _refusal(exc: GridSearchRefusal) -> GoldenSearchRefusal:
    return GoldenSearchRefusal(str(exc), code=exc.code)


def sweep_roots() -> list[Path]:
    return resolve_data_roots(source=SWEEP_DATA_POLICY["source"], adjusted=SWEEP_DATA_POLICY["adjusted"])


# ── Reading a requested plan ─────────────────────────────────────────────


def _canonical_or_raw(strategy_key: str, symbol: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical point when the values admit one; otherwise the values as sent, for validation to name."""
    if strategy_key not in _STRATEGY_REGISTRY:
        return {**params, "symbol": symbol}
    try:
        return canonical_point(strategy_key, symbol, {k: v for k, v in params.items() if k != "symbol"})
    except ValueError:
        # Validation refuses these values with the exact knob and reason; canonicalizing cannot.
        return {**params, "symbol": symbol}


def protocol_from_request(data: Mapping[str, Any]) -> GoldenSearchProtocol:
    """A frozen plan from a request: symbol upper-cased, the seed defaulted, both canonical, knobs in importance order.

    The seed defaults to a registry incumbent's point, and to the registry
    point when the incumbent is a qualification (see :func:`default_seed`).

    Raises ``GoldenSearchRefusal(PROTOCOL_MALFORMED)`` only for a request that
    is not a plan at all; every problem a plan can have is a refusal of
    :func:`review_plan`.
    """
    try:
        body = dict(data)
        symbol = str(body["symbol"]).strip().upper()
        strategy_key = str(body["strategy_key"])
        incumbent = dict(body["incumbent"])
        incumbent_params = _canonical_or_raw(strategy_key, symbol, dict(incumbent["params"]))
        raw_seed = body.get("seed")
        seed = (
            default_seed(strategy_key, symbol, source=str(incumbent["source"]), params=incumbent_params)
            if raw_seed is None
            else _canonical_or_raw(strategy_key, symbol, dict(raw_seed))
        )
        body.update(symbol=symbol, seed=seed, incumbent={**incumbent, "params": incumbent_params})
        protocol = GoldenSearchProtocol.from_dict(body)
        declaration = declaration_for(strategy_key)
        if declaration is None:
            return protocol
        # The frozen knob order is the search order: by importance, ties in the declaration's order.
        return replace(protocol, knobs=by_importance(protocol.knobs, [knob.name for knob in declaration.knobs]))
    except (KeyError, TypeError, ValueError) as exc:
        raise GoldenSearchRefusal(f"The plan is malformed: {exc}", code="PROTOCOL_MALFORMED") from exc


def request_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ── Run-up ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StudyRunUp:
    required_samples: int
    bar_span_ms: int
    run_up_sessions: int
    data_start: date
    probed_points: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "required_samples": self.required_samples,
            "bar_span_ms": self.bar_span_ms,
            "run_up_sessions": self.run_up_sessions,
            "data_start_ms": et_midnight_ms(self.data_start),
            "probed_points": self.probed_points,
        }


def _warmup_values(declaration: SearchDeclaration, protocol: GoldenSearchProtocol) -> dict[str, list[Decimal]]:
    seed = knob_values(declaration, protocol.seed)
    incumbent = knob_values(declaration, protocol.incumbent.params)
    plans = {plan.name: plan for plan in protocol.knobs}
    values: dict[str, list[Decimal]] = {}
    for knob in declaration.knobs:
        if not knob.warmup_dependent:
            continue
        candidates = {seed[knob.name], incumbent[knob.name]}
        plan = plans[knob.name]
        if plan.mode == "search":
            high = to_decimal(plan.high)
            candidates |= {to_decimal(plan.low), high, min(knob.domain_high, high + knob.neighbor_step)}
            # A seed outside the range stays the winner when no move improves on it; its neighbors are audited too.
            candidates.add(min(knob.domain_high, seed[knob.name] + knob.neighbor_step))
        else:
            candidates.add(to_decimal(plan.fixed_value))
        values[knob.name] = sorted(candidates)
    return values


def slowest_requirement(protocol: GoldenSearchProtocol, declaration: SearchDeclaration) -> tuple[WarmupProbe, int]:
    """The slowest valid point's readiness over the plan's legal space, and how many points were probed."""
    seed = knob_values(declaration, protocol.seed)
    values = _warmup_values(declaration, protocol)
    names = list(values)
    probes: list[WarmupProbe] = []
    for combo in product(*(values[name] for name in names)):
        assignment = {**seed, **dict(zip(names, combo, strict=True))}
        if violates(declaration, assignment) is not None:
            continue
        params = {knob.name: scalar(knob, assignment[knob.name]) for knob in declaration.knobs}
        try:
            probes.append(probe_warmup_samples(protocol.strategy_key, {**params, "symbol": protocol.symbol}))
        except WarmupProbeError as exc:
            raise GoldenSearchRefusal(str(exc), code="WARMUP_UNMEASURABLE") from exc
    if not probes:
        raise GoldenSearchRefusal("No valid setting of the warmup-dependent knobs exists in this plan.", code="WARMUP_UNMEASURABLE")
    slowest = max(probes, key=lambda probe: (probe.required_samples * probe.bar_span_ms, probe.required_samples))
    return slowest, len(probes)


def _first_session_on_or_after(day: date) -> date:
    sessions = expected_sessions(day, day + timedelta(days=_SCAN_DAYS))
    if not sessions:
        raise GoldenSearchRefusal(f"No trading session within two weeks of {day.isoformat()}.", code="INTERVALS_INVALID")
    return sessions[0]


def _sessions_needed(window_start_ms: int, required_samples: int, bar_span_ms: int) -> int:
    """Sessions before a window that hold ``required_samples`` whole decision bars (fewer bars on a half-day)."""
    first = _first_session_on_or_after(et_date_at_ms(window_start_ms))
    earliest = session_start_for_bar_count(session_open_ms_utc(first), target_bars=required_samples, bar_span_ms=bar_span_ms)
    return len(expected_sessions(earliest, first - timedelta(days=1)))


def _deepest_run_up_sessions(protocol: GoldenSearchProtocol, probe: WarmupProbe | StudyRunUp) -> int:
    """The run-up sessions the plan's most demanding window needs: one behind an early close needs more."""
    return max(_sessions_needed(start, probe.required_samples, probe.bar_span_ms) for start in window_starts(protocol))


def window_starts(protocol: GoldenSearchProtocol) -> list[int]:
    """Every evaluation window start the protocol can produce."""
    starts = {protocol.development_start_ms, protocol.final_start_ms, recent_window_ms(protocol)[0]}
    for fold in fold_windows(protocol):
        starts |= {fold.train_start_ms, fold.test_start_ms}
    return sorted(starts)


def _servable_start_ms(protocol: GoldenSearchProtocol, probe: WarmupProbe | StudyRunUp, roots: Sequence[Path]) -> int | None:
    """The earliest later development start the lake can serve, or ``None`` when no later start helps.

    Later starts keep the development end and drop whole folds from the front,
    so the plan still divides. One is servable when the lake holds its run-up
    and every session after it, without a gap, through the final test. The
    lake is read from the refused start only, so a run-up longer than a test
    fold lands on a later start than the lake strictly needs.
    """
    boundary = et_date_at_ms(protocol.development_end_ms)
    report = check_availability(
        roots, protocol.symbol, et_date_at_ms(protocol.development_start_ms), et_date_at_ms(protocol.final_end_ms - 1)
    )
    held = {day for days in report.sources.values() for day in days}
    readable_from = unbroken_run_start(held, report.end)
    if readable_from is None:
        return None
    for folds in range(len(fold_windows(protocol)) - 1, 0, -1):
        months = protocol.training_months + folds * protocol.test_months
        start = add_months(boundary, -months)
        # A start whose day its month lacks does not add back to the same end; the fold planner would refuse it.
        if start < readable_from or add_months(start, months) != boundary:
            continue
        later = replace(protocol, development_start_ms=et_midnight_ms(start))
        if warmup_session(later.development_start_ms, _deepest_run_up_sessions(later, probe)) >= readable_from:
            return later.development_start_ms
    return None


def plan_study_run_up(protocol: GoldenSearchProtocol, declaration: SearchDeclaration, *, roots: Sequence[Path]) -> StudyRunUp:
    """The common run-up every evaluation reads; refuses one the lake cannot supply before the development start."""
    probe, probed = slowest_requirement(protocol, declaration)
    start_day = et_date_at_ms(protocol.development_start_ms)
    end_day = et_date_at_ms(protocol.final_end_ms - 1)
    try:
        plan = plan_run_up(
            symbol=protocol.symbol,
            requested_start=start_day,
            requested_end=end_day,
            required_samples=probe.required_samples,
            bar_span_ms=probe.bar_span_ms,
            roots=roots,
        )
    except RunUpExceedsRangeError as exc:
        raise GoldenSearchRefusal(str(exc), code="RUN_UP_EXCEEDS_RANGE") from exc

    deepest = _deepest_run_up_sessions(protocol, probe)

    def refused() -> GoldenSearchRefusal:
        """Why the run-up cannot be read: the lake holds it and a run cannot read it, or the lake does not hold it."""
        later = _servable_start_ms(protocol, probe, roots)
        named = "" if later is None else f" The earliest start the lake can serve with whole folds is {et_date_at_ms(later).isoformat()}."
        held = check_availability(
            roots, protocol.symbol, warmup_session(protocol.development_start_ms, deepest), start_day - timedelta(days=1)
        )
        if held.unreadable_files:
            unreadable = data_missing_refusal(MissingSessionsError(held))
            why, code = f"a run cannot read them: {unreadable}.", unreadable.code
        else:
            why, code = (
                f"the lake does not hold them for {protocol.symbol}. Backfill earlier history or start the development "
                "interval later; the run-up is never carved from the scored range.",
                "RUN_UP_HISTORY_MISSING",
            )
        return GoldenSearchRefusal(
            f"The slowest setting needs {probe.required_samples} decision bars of history before {start_day.isoformat()}, and {why}{named}",
            code=code,
            field="development_start_ms",
            nearest_valid_ms=() if later is None else (later,),
        )

    if plan.carved_from_range:
        raise refused()
    sessions = max(plan.run_up_sessions, deepest)
    data_start = plan.data_start
    if sessions > plan.run_up_sessions:
        data_start = warmup_session(protocol.development_start_ms, sessions)
        prior = check_availability(roots, protocol.symbol, data_start, plan.data_start - timedelta(days=1))
        if not prior.is_complete:
            raise refused()
    return StudyRunUp(
        required_samples=probe.required_samples,
        bar_span_ms=probe.bar_span_ms,
        run_up_sessions=sessions,
        data_start=data_start,
        probed_points=probed,
    )


# ── Review (preflight) ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanReview:
    """Everything a preflight shows: refusals are data, never an error."""

    protocol: GoldenSearchProtocol
    review: ProtocolReview | None
    refusals: tuple[ProtocolRefusal, ...]
    run_up: StudyRunUp | None
    # Each window's trade floor (``activity.activity_plan``); the lock freezes this same value.
    activity: dict[str, Any] | None = None

    @property
    def lockable(self) -> bool:
        return not self.refusals and self.review is not None and self.run_up is not None


def _as_refusal(exc: GoldenSearchRefusal) -> ProtocolRefusal:
    return ProtocolRefusal(code=exc.code, field=exc.field, message=str(exc), nearest_valid_ms=exc.nearest_valid_ms)


def backfill_remedy(report: AvailabilityReport) -> BackfillRemedy | None:
    """The backfill that fetches a window's missing sessions, or ``None`` when the provider serves none of them.

    Its window runs from the first to the last missing session the provider
    still serves and the market has reached; a backfill reuses the sessions
    between them that the lake already holds.
    """
    today = et_date_at_ms(now_ms_utc())
    floor = polygon_history_floor(today)
    fetchable = [day for day in report.missing_days if floor <= day <= today]
    if not fetchable:
        return None
    return BackfillRemedy(
        symbol=report.symbol,
        price_adjustment_mode=polygon_mode_for(adjusted=SWEEP_DATA_POLICY["adjusted"]),
        start_trading_date_ms=trading_date_to_calendar_anchor_ms(fetchable[0]),
        end_trading_date_ms=trading_date_to_calendar_anchor_ms(fetchable[-1]),
    )


def _servable_date(
    protocol: GoldenSearchProtocol, run_up: StudyRunUp, roots: Sequence[Path], report: AvailabilityReport
) -> tuple[str | None, tuple[int, ...]]:
    """The date a plan with missing sessions could take instead, and the field it belongs to; no field when none helps.

    A lake that stops before the final test does is served by ending the
    final test after the lake's last session, while that leaves the final
    test a session. A hole further back is served by a later development
    start (:func:`_servable_start_ms`).
    """
    held = [day for days in report.sources.values() for day in days]
    if not held:
        return None, ()
    last_held = max(held)
    if report.missing_days[-1] > last_held:
        if last_held < et_date_at_ms(protocol.final_start_ms):
            return None, ()
        return "final_end_ms", (et_midnight_ms(last_held + timedelta(days=1)),)
    later = _servable_start_ms(protocol, run_up, roots)
    return (None, ()) if later is None else ("development_start_ms", (later,))


def _unread_sessions_refusal(
    protocol: GoldenSearchProtocol, run_up: StudyRunUp, roots: Sequence[Path], report: AvailabilityReport
) -> ProtocolRefusal:
    """Sessions the plan reads that the lake cannot supply: why, and for missing ones the backfill and the date that serve it."""
    refused = data_missing_refusal(MissingSessionsError(report))
    if refused.code != "DATA_MISSING":
        return ProtocolRefusal(code=refused.code, field=None, message=str(refused))
    field, nearest = _servable_date(protocol, run_up, roots, report)
    return ProtocolRefusal(
        code=refused.code, field=field, message=str(refused), nearest_valid_ms=nearest, backfill=backfill_remedy(report)
    )


def review_plan(protocol: GoldenSearchProtocol, *, roots: Sequence[Path] | None = None, check_data: bool = True) -> PlanReview:
    """Validate, size and plan the run-up (and, with ``check_data``, confirm every session it reads is in the lake)."""
    declaration = declaration_for(protocol.strategy_key)
    if declaration is None:
        reason = unavailable_reason(protocol.strategy_key) or "No Golden Search declaration exists for this strategy."
        refusal = ProtocolRefusal(code="STRATEGY_UNAVAILABLE", field="strategy_key", message=reason)
        return PlanReview(protocol=protocol, review=None, refusals=(refusal,), run_up=None)
    review = review_protocol(protocol, declaration)
    refusals = (*review.refusals, *_qualified_seed_refusals(protocol))
    if refusals:
        return PlanReview(protocol=protocol, review=review, refusals=refusals, run_up=None)
    activity = activity_plan(protocol)
    if activity is not None:
        empty = tuple(
            ProtocolRefusal(
                code="NO_TRADING_DAYS",
                field="final_end_ms" if window["key"] == "final" else "development_end_ms",
                message=f"{window['label']} contains no exchange trading days; select a tradable window.",
            )
            for window in activity["windows"] if window["trading_sessions"] == 0
        )
        if empty:
            return PlanReview(protocol=protocol, review=review, refusals=empty, run_up=None)
    resolved = list(roots) if roots is not None else sweep_roots()
    try:
        run_up = plan_study_run_up(protocol, declaration, roots=resolved)
    except GoldenSearchRefusal as exc:
        return PlanReview(protocol=protocol, review=review, refusals=(_as_refusal(exc),), run_up=None, activity=activity)
    if check_data:
        availability = check_availability(resolved, protocol.symbol, run_up.data_start, et_date_at_ms(protocol.final_end_ms - 1))
        if not availability.is_complete:
            refusal = _unread_sessions_refusal(protocol, run_up, resolved, availability)
            return PlanReview(protocol=protocol, review=review, refusals=(refusal,), run_up=run_up, activity=activity)
    return PlanReview(protocol=protocol, review=review, refusals=(), run_up=run_up, activity=activity)


def preflight_view(plan: PlanReview, exposure: Mapping[str, Any] | None) -> dict[str, Any]:
    """The preflight answer: refusals, the estimate against the cap, each knob's value count, folds, exposure and run-up."""
    review = plan.review
    study_estimate = review.estimate if review is not None else None
    return {
        "refusals": [refusal.as_dict() for refusal in plan.refusals],
        "knob_values": [{"name": name, "values": count} for name, count in knob_value_counts(plan.protocol).items()],
        "estimate": None
        if study_estimate is None
        else {**study_estimate.as_dict(), "budget_cap": plan.protocol.budget_cap},
        "folds": [] if review is None else [fold.as_dict() for fold in review.folds],
        "exposure": None if exposure is None else dict(exposure),
        "run_up": None
        if plan.run_up is None
        else {
            "required_samples": plan.run_up.required_samples,
            "run_up_sessions": plan.run_up.run_up_sessions,
            "data_start_ms": et_midnight_ms(plan.run_up.data_start),
        },
        "activity": plan.activity,
    }


# ── Lock ─────────────────────────────────────────────────────────────────


def study_id_for(idempotency_key: str, *, parent_study_id: str | None = None) -> str:
    """A lock's study id follows from its idempotency key, so a retried lock finds the study it wrote."""
    scope = f"revise/{parent_study_id}" if parent_study_id else "lock"
    return hashlib.sha256(f"golden-search/{scope}/{idempotency_key}".encode()).hexdigest()[:32]


def build_receipt(plan: PlanReview, *, snapshot_dict: Mapping[str, Any], snapshot_digest: str, identity: CodeIdentity) -> dict[str, Any]:
    """The immutable record every evaluation, resume and proof is checked against."""
    protocol = plan.protocol
    assert plan.review is not None and plan.review.estimate is not None and plan.run_up is not None
    # A frequency plan's floors are frozen here or never; a stage refuses a receipt without them.
    assert (plan.activity is None) == (protocol.expected_trades_per_year is None)
    registration = _STRATEGY_REGISTRY[protocol.strategy_key]
    contract = registration.signal_program_contract
    program_version = contract.program_version if contract is not None else None
    schema_version = getattr(registration.param_schema, "PARAMETER_SCHEMA_VERSION", None)
    run_up = plan.run_up.as_dict()
    context = execution_context(
        strategy_key=protocol.strategy_key,
        program_version=program_version,
        parameter_schema_version=schema_version,
        code_identity=identity.as_dict(),
        data_snapshot_digest=snapshot_digest,
        execution=protocol.execution,
        data_policy=SWEEP_DATA_POLICY,
        run_up=run_up,
    )
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "execution_contract": {
            "strategy_name": protocol.strategy_key,
            "symbol": protocol.symbol,
            "resolution": "minute",
            "fill_mode": protocol.execution.fill_mode,
            "commission_per_order": protocol.execution.commission_per_order,
            "slippage_per_share": protocol.execution.slippage_per_share,
            "initial_cash": protocol.execution.initial_cash,
            "data_policy": dict(SWEEP_DATA_POLICY),
            "save_study": False,
        },
        "intervals": {
            "development_start_ms": protocol.development_start_ms,
            "development_end_ms": protocol.development_end_ms,
            "final_start_ms": protocol.final_start_ms,
            "final_end_ms": protocol.final_end_ms,
            "data_start_ms": run_up["data_start_ms"],
        },
        "run_up": run_up,
        "program_version": program_version,
        "parameter_schema_version": schema_version,
        "code_identity": identity.as_dict(),
        "data_snapshot": dict(snapshot_dict),
        "data_snapshot_digest": snapshot_digest,
        "execution_context": context,
        "context_digest": context_digest(context),
        "estimate": plan.review.estimate.as_dict(),
        "folds": [fold.as_dict() for fold in plan.review.folds],
        "activity": plan.activity,
    }


def prepare_lock(
    protocol: GoldenSearchProtocol,
    *,
    idempotency_key: str,
    parent_study_id: str | None = None,
    roots: Sequence[Path] | None = None,
    identity: CodeIdentity | None = None,
) -> NewStudy:
    """Review, freeze the data snapshot and code identity, and build the study to insert — blocking disk work, no database."""
    resolved = list(roots) if roots is not None else sweep_roots()
    plan = review_plan(protocol, roots=resolved, check_data=False)
    if not plan.lockable:
        first = plan.refusals[0]
        raise GoldenSearchRefusal(
            "; ".join(refusal.message for refusal in plan.refusals),
            code=first.code,
            field=first.field,
            refusals=tuple(refusal.as_dict() for refusal in plan.refusals),
        )
    assert plan.run_up is not None
    try:
        snapshot = capture_data_snapshot(
            roots=resolved,
            symbol=protocol.symbol,
            resolution="minute",
            data_start=plan.run_up.data_start,
            data_end=et_date_at_ms(protocol.final_end_ms - 1),
        )
    except MissingSessionsError as exc:
        raise _refusal(data_missing_refusal(exc)) from exc
    if identity is None:
        try:
            identity = current_code_identity()
        except GridSearchRefusal as exc:
            raise _refusal(exc) from exc
    protocol_dict = protocol.as_dict()
    return NewStudy(
        id=study_id_for(idempotency_key, parent_study_id=parent_study_id),
        parent_study_id=parent_study_id,
        strategy_key=protocol.strategy_key,
        symbol=protocol.symbol,
        protocol=protocol_dict,
        protocol_hash=protocol.protocol_hash(),
        receipt=build_receipt(plan, snapshot_dict=snapshot.as_dict(), snapshot_digest=snapshot.digest(), identity=identity),
        budget_cap=protocol.budget_cap,
        request_sha256=request_sha256(protocol_dict),
        idempotency_key=idempotency_key,
    )


# ── Defaults ─────────────────────────────────────────────────────────────


def default_seed(strategy_key: str, symbol: str, *, source: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Where the searches start when the plan names no seed: never a qualified tuple.

    A qualified configuration was chosen on data the walk-forward folds then
    test, so seeding a fold from it would lean every fold toward that
    future (#2696). It stays the frozen incumbent; the search starts from
    the registry point.
    """
    if source == "qualification":
        return dict(registry_incumbent(strategy_key, symbol).params)
    return dict(params)


def _qualified_seed_refusals(protocol: GoldenSearchProtocol) -> tuple[ProtocolRefusal, ...]:
    """A seed equal to the qualified incumbent's tuple is refused, unless that tuple is the registry point itself."""
    incumbent = protocol.incumbent
    if incumbent.source != "qualification" or dict(protocol.seed) != dict(incumbent.params):
        return ()
    if dict(protocol.seed) == registry_incumbent(protocol.strategy_key, protocol.symbol).params:
        return ()
    return (
        ProtocolRefusal(
            code="SEED_IS_QUALIFIED",
            field="seed",
            message=(
                "The search cannot start from the qualified configuration: it was chosen on data the "
                "walk-forward folds test. Start from the registry point or another value fixed before results."
            ),
        ),
    )


def registry_incumbent(strategy_key: str, symbol: str) -> IncumbentRef:
    """The registry's validated point as a canonical incumbent (the strategy's schema defaults when it has none)."""
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        raise GoldenSearchRefusal(f"No strategy named {strategy_key!r} is registered.", code="UNKNOWN_STRATEGY", kind="not_found")
    contract = registration.signal_program_contract
    settings = dict(contract.validated_settings) if contract is not None else {}
    return IncumbentRef(source="registry", qualification_id=None, params=canonical_point(strategy_key, symbol, settings))


def _month_start(day: date) -> date:
    return date(day.year, day.month, 1)


def _earliest_development_start(earliest_session: date) -> date:
    """The first month start leaving at least one whole calendar month of lake history for the run-up.

    ``earliest_session`` starts the lake's latest unbroken run, not its held span: nothing is read across a hole.
    """
    month = _month_start(earliest_session)
    first_session = _first_session_on_or_after(month)
    return add_months(month, 1 if earliest_session <= first_session else 2)


def _whole_months(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + (end.month - start.month))


@dataclass(frozen=True)
class FinalInterval:
    """The default final interval ``[start, end)`` and how many trailing scheduled sessions it left to the lake's lag."""

    start: date
    end: date
    sessions_cut: int


def default_final_interval(now_ms: int, final_months: int, latest_session: date | None) -> FinalInterval:
    """The last ``final_months`` whole months, ending after the lake's last session when it lags by a few sessions.

    Only scheduled NYSE sessions count, so a month ending on a weekend or
    holiday is whole when the lake holds its last session. A lake further
    behind than ``FINAL_TAIL_CUT_SESSIONS``, or stopping before the interval
    starts, keeps the whole months for the availability check to refuse.
    """
    month_end = _month_start(et_date_at_ms(now_ms))
    start = add_months(month_end, -final_months)
    if latest_session is None or not start <= latest_session < month_end - timedelta(days=1):
        return FinalInterval(start=start, end=month_end, sessions_cut=0)
    lagging = expected_sessions(latest_session + timedelta(days=1), month_end - timedelta(days=1))
    if not lagging or len(lagging) > FINAL_TAIL_CUT_SESSIONS:
        return FinalInterval(start=start, end=month_end, sessions_cut=0)
    return FinalInterval(start=start, end=latest_session + timedelta(days=1), sessions_cut=len(lagging))


@dataclass(frozen=True)
class StudyRange:
    """The ET days a picked range lets a plan use: ``[start, end)``."""

    start: date
    end: date


@dataclass(frozen=True)
class RangeFit:
    """A plan's intervals inside a picked range, and the sessions at the range's start the plan leaves out."""

    development_start: date
    final_start: date
    final_end: date
    unused_sessions: tuple[date, ...]
    #: Where the lake's unbroken history starts, when its run-up month put the development start later than the folds alone would.
    lake_from: date | None
    #: Days the final test starts before its whole months: development ended there, from a start clamped to a shorter month's end.
    final_early_days: int


def study_range_at(range_start_ms: int | None, range_end_ms: int | None) -> StudyRange | None:
    """A picked range from its two ET midnights, ``[start, end)``; ``None`` when neither is given."""
    if range_start_ms is None and range_end_ms is None:
        return None
    for name, ms in (("range_start_ms", range_start_ms), ("range_end_ms", range_end_ms)):
        if ms is None:
            raise GoldenSearchRefusal("A study range needs both its first day and the day after its last.", code="INTERVALS_INVALID", field=name)
        if et_midnight_ms(et_date_at_ms(ms)) != ms:
            raise GoldenSearchRefusal(f"{name} must fall on an ET midnight.", code="INTERVAL_NOT_ET_MIDNIGHT", field=name)
    assert range_start_ms is not None and range_end_ms is not None
    if not range_start_ms < range_end_ms:
        raise GoldenSearchRefusal("The study range must end after it starts.", code="INTERVALS_INVALID", field="range_end_ms")
    return StudyRange(start=et_date_at_ms(range_start_ms), end=et_date_at_ms(range_end_ms))


def _months(count: int) -> str:
    return f"{count} month" if count == 1 else f"{count} months"


def fit_range(
    study_range: StudyRange, *, final_months: int, training_months: int, test_months: int, earliest_session: date | None
) -> RangeFit:
    """The longest whole-fold plan inside ``study_range`` whose final test ends where the range ends.

    Development may start no earlier than the range's first day, nor before
    the lake's unbroken run of sessions (``earliest_session`` starts it)
    leaves a calendar month for the run-up: nothing is read across a hole. A
    range that holds no fold is refused with the start the plan needs.
    """
    if min(final_months, training_months, test_months) < 1:
        raise GoldenSearchRefusal("Interval lengths must be at least one month.", code="INTERVALS_INVALID")
    nominal_final_start = add_months(study_range.end, -final_months)
    floor = study_range.start if earliest_session is None else max(study_range.start, _earliest_development_start(earliest_session))

    def first_fit(first_day: date) -> tuple[date, date] | None:
        """The earliest development start on or after ``first_day``, and the day its whole months end."""
        folds = (_whole_months(first_day, nominal_final_start) - training_months) // test_months
        while folds >= 1:
            months = training_months + folds * test_months
            start = add_months(nominal_final_start, -months)
            if start >= first_day:
                # From a start clamped to a shorter month's end, whole months end up to three days before the nominal day.
                return start, add_months(start, months)
            folds -= 1
        return None

    fit = first_fit(floor)
    if fit is None:
        needed = add_months(nominal_final_start, -(training_months + test_months))
        asks = f"{_months(training_months)} of training, one test of {_months(test_months)} and a final test of {_months(final_months)}"
        # The range is to blame whenever it starts too late by itself: no backfill cures that, whatever the lake holds.
        if earliest_session is None or study_range.start > needed:
            raise GoldenSearchRefusal(
                f"The range is too short: {asks} need development to start by {needed.isoformat()}, and the range starts on {study_range.start.isoformat()}.",
                code="INTERVALS_INVALID",
                field="range_start_ms",
            )
        raise GoldenSearchRefusal(
            f"The lake's unbroken history starts on {earliest_session.isoformat()}, and its first month is kept for the run-up, "
            f"so development cannot start before {floor.isoformat()}. {asks[0].upper()}{asks[1:]} that end with "
            f"this range need it to start by {needed.isoformat()}.",
            code="INTERVALS_INVALID",
        )
    development_start, final_start = fit
    unused = expected_sessions(study_range.start, development_start - timedelta(days=1)) if development_start > study_range.start else []
    held_back = floor > study_range.start and first_fit(study_range.start) != fit
    return RangeFit(
        development_start=development_start,
        final_start=final_start,
        final_end=study_range.end,
        unused_sessions=tuple(unused),
        lake_from=earliest_session if held_back else None,
        final_early_days=(nominal_final_start - final_start).days,
    )


def range_fit_sentence(fit: RangeFit, *, final_months: int, training_months: int, test_months: int) -> str | None:
    """What a plan laid out inside a picked range does not take from it as asked, and why; ``None`` when nothing.

    The sessions it leaves out at the range's start, and a final test that
    starts before its whole months.
    """
    said: list[str] = []
    if fit.unused_sessions:
        first, last = fit.unused_sessions[0].isoformat(), fit.unused_sessions[-1].isoformat()
        count = len(fit.unused_sessions)
        left_out = f"The session on {first} is" if count == 1 else f"The {count} sessions from {first} to {last} are"
        split = f"split into {_months(training_months)} of training and whole tests of {_months(test_months)}"
        why = (
            f"the months before the final test have to {split}"
            if fit.lake_from is None
            else f"the lake's unbroken history starts on {fit.lake_from.isoformat()}, its first month is kept for the run-up, and the rest has to {split}"
        )
        said.append(f"Development starts on {fit.development_start.isoformat()}. {left_out} left out: {why}.")
    if fit.final_early_days:
        early = "1 day" if fit.final_early_days == 1 else f"{fit.final_early_days} days"
        said.append(
            f"The final test starts on {fit.final_start.isoformat()}, {early} before its {_months(final_months)}: "
            "development's whole months end there."
        )
    return " ".join(said) or None


def _default_knob(knob: SearchKnob, seed: Decimal, benchmark: Decimal) -> KnobPlan:
    """A knob as a fresh plan starts it: searched or held by declaration, its default range widened to hold the seed
    and the benchmark (each end rounded outward onto the quantum and clamped to the domain), and held at the seed."""
    return KnobPlan(
        name=knob.name,
        mode="search" if knob.searchable_by_default else "fixed",
        low=float(quantize(knob, min(knob.default_low, seed, benchmark), ROUND_FLOOR)),
        high=float(quantize(knob, max(knob.default_high, seed, benchmark), ROUND_CEILING)),
        fixed_value=float(seed),
        step=float(knob.default_step) if knob.searchable_by_default else None,
        importance=DEFAULT_IMPORTANCE,
    )


def default_protocol(
    strategy_key: str,
    symbol: str,
    incumbent: IncumbentRef,
    *,
    now_ms: int,
    earliest_session: date | None,
    latest_session: date | None = None,
    final_months: int = DEFAULT_FINAL_MONTHS,
    training_months: int = DEFAULT_TRAINING_MONTHS,
    test_months: int = DEFAULT_TEST_MONTHS,
    fit: RangeFit | None = None,
) -> GoldenSearchProtocol:
    """A complete starting plan: declared default ranges, the default seed, and server-computed intervals.

    With ``fit`` the intervals are the ones laid out inside a picked range, for the same month counts.
    """
    declaration = declaration_for(strategy_key)
    if declaration is None:
        reason = unavailable_reason(strategy_key) or "No Golden Search declaration exists for this strategy."
        raise GoldenSearchRefusal(reason, code="STRATEGY_UNAVAILABLE")
    if min(final_months, training_months, test_months) < 1:
        raise GoldenSearchRefusal("Interval lengths must be at least one month.", code="INTERVALS_INVALID")
    if fit is None:
        final = default_final_interval(now_ms, final_months, latest_session)
        final_start_day, final_end_day = final.start, final.end
    else:
        final_start_day, final_end_day = fit.final_start, fit.final_end
    # Fixed knobs hold the seed's values, so the plan never contradicts where it starts.
    start = default_seed(strategy_key, symbol, source=incumbent.source, params=incumbent.params)
    seed = knob_values(declaration, start)
    benchmark = knob_values(declaration, incumbent.params)
    knobs = tuple(_default_knob(knob, seed[knob.name], benchmark[knob.name]) for knob in declaration.knobs)
    searched = {plan.name for plan in knobs if plan.mode == "search"}

    def plan_from(development_start: date) -> GoldenSearchProtocol:
        return GoldenSearchProtocol(
            strategy_key=strategy_key,
            symbol=symbol,
            method="zoom",
            knobs=knobs,
            seed=start,
            incumbent=incumbent,
            development_start_ms=et_midnight_ms(development_start),
            development_end_ms=et_midnight_ms(final_start_day),
            final_start_ms=et_midnight_ms(final_start_day),
            final_end_ms=et_midnight_ms(final_end_day),
            training_months=training_months,
            test_months=test_months,
            pair_audits=tuple(pair for pair in declaration.default_pair_audits if set(pair) <= searched),
            policy=SelectionPolicy(min_trades=None),
            exam_min_trades=None,
            expected_trades_per_year=DEFAULT_EXPECTED_TRADES_PER_YEAR,
        )

    if fit is not None:
        return plan_from(fit.development_start)
    available = (
        UNKNOWN_HISTORY_MONTHS
        if earliest_session is None
        else _whole_months(_earliest_development_start(earliest_session), final_start_day)
    )
    folds = max(1, (available - training_months) // test_months)

    def plan_with(fold_count: int) -> GoldenSearchProtocol:
        return plan_from(add_months(final_start_day, -(training_months + fold_count * test_months)))

    protocol = plan_with(folds)
    while folds > 1 and estimate(protocol, folds=folds).total_max > protocol.budget_cap:
        folds -= 1
        protocol = plan_with(folds)
    return protocol
