"""Golden Search plan-time work: read a requested plan, size it, plan its run-up, freeze it at lock, propose defaults.

Formula (common run-up): the slowest warmup requirement is measured over
the plan's whole legal search space — every warmup-dependent knob at its
seed, incumbent, range ends and one neighbor step past the upper end and
past the seed (clipped to the domain), in every combination the declared constraints
allow — by the real program (``app.research.sweep.warmup``). The canonical
planner (``plan_run_up``) turns it into whole sessions before the
development start; a plan whose run-up would have to be carved from the
scored range is refused (``RUN_UP_HISTORY_MISSING``), because a carved start
would shift the development interval. Every evaluation window then reads
the same number of sessions before its start, widened to the deepest
window's need so a window behind an early close is still fully primed.

Formula (default intervals): the final interval is the last ``final_months``
whole ET calendar months ending at the first day of the current ET month
(completed sessions only). Development is the longest whole-month range
ending at the final start that (a) plans whole walk-forward folds, (b)
leaves at least one calendar month of lake history before it for the
run-up (24 months when the lake's first session is unknown), and (c) keeps
the default plan's conservative evaluation bound within its budget.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Plan before
  seeing results", "Open one final test"; ``app/research/sweep/warmup.py``.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_planning.py.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any

from app.engine.data.availability import MissingSessionsError, check_availability
from app.engine.data.policy_store import resolve_data_roots
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_start_for_bar_count
from app.research.golden_search.budget import ProtocolReview, estimate, review_protocol
from app.research.golden_search.declarations import (
    SearchDeclaration,
    canonical_point,
    declaration_for,
    knob_values,
    scalar,
    to_decimal,
    unavailable_reason,
    violates,
)
from app.research.golden_search.evaluator import context_digest, execution_context, warmup_session
from app.research.golden_search.models import GoldenSearchRefusal, NewStudy
from app.research.golden_search.procedure_history import fold_windows
from app.research.golden_search.protocol import (
    GoldenSearchProtocol,
    IncumbentRef,
    KnobPlan,
    ProtocolRefusal,
    canonical_json,
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

logger = logging.getLogger(__name__)

RECEIPT_SCHEMA_VERSION = 1
DEFAULT_FINAL_MONTHS = 3
DEFAULT_TRAINING_MONTHS = 6
DEFAULT_TEST_MONTHS = 2
UNKNOWN_HISTORY_MONTHS = 24
_SCAN_DAYS = 14


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
    """A frozen plan from a request: symbol upper-cased, seed defaulted to the incumbent, both canonical.

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
        seed = dict(incumbent_params) if raw_seed is None else _canonical_or_raw(strategy_key, symbol, dict(raw_seed))
        body.update(symbol=symbol, seed=seed, incumbent={**incumbent, "params": incumbent_params})
        return GoldenSearchProtocol.from_dict(body)
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


def window_starts(protocol: GoldenSearchProtocol) -> list[int]:
    """Every evaluation window start the protocol can produce."""
    starts = {protocol.development_start_ms, protocol.final_start_ms, recent_window_ms(protocol)[0]}
    for fold in fold_windows(protocol):
        starts |= {fold.train_start_ms, fold.test_start_ms}
    return sorted(starts)


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
    missing = GoldenSearchRefusal(
        f"The slowest setting needs {probe.required_samples} decision bars of history before "
        f"{start_day.isoformat()}, and the lake does not hold them for {protocol.symbol}. Backfill earlier "
        "history or start the development interval later; the run-up is never carved from the scored range.",
        code="RUN_UP_HISTORY_MISSING",
    )
    if plan.carved_from_range:
        raise missing
    sessions = max(
        plan.run_up_sessions,
        *(_sessions_needed(start, probe.required_samples, probe.bar_span_ms) for start in window_starts(protocol)),
    )
    data_start = plan.data_start
    if sessions > plan.run_up_sessions:
        data_start = warmup_session(protocol.development_start_ms, sessions)
        prior = check_availability(roots, protocol.symbol, data_start, plan.data_start - timedelta(days=1))
        if not prior.is_complete:
            raise missing
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

    @property
    def lockable(self) -> bool:
        return not self.refusals and self.review is not None and self.run_up is not None


def _as_refusal(exc: GoldenSearchRefusal) -> ProtocolRefusal:
    return ProtocolRefusal(code=exc.code, field=exc.field, message=str(exc))


def review_plan(protocol: GoldenSearchProtocol, *, roots: Sequence[Path] | None = None, check_data: bool = True) -> PlanReview:
    """Validate, size and plan the run-up (and, with ``check_data``, confirm every session it reads is in the lake)."""
    declaration = declaration_for(protocol.strategy_key)
    if declaration is None:
        reason = unavailable_reason(protocol.strategy_key) or "No Golden Search declaration exists for this strategy."
        refusal = ProtocolRefusal(code="STRATEGY_UNAVAILABLE", field="strategy_key", message=reason)
        return PlanReview(protocol=protocol, review=None, refusals=(refusal,), run_up=None)
    review = review_protocol(protocol, declaration)
    if not review.lockable:
        return PlanReview(protocol=protocol, review=review, refusals=review.refusals, run_up=None)
    resolved = list(roots) if roots is not None else sweep_roots()
    try:
        run_up = plan_study_run_up(protocol, declaration, roots=resolved)
    except GoldenSearchRefusal as exc:
        return PlanReview(protocol=protocol, review=review, refusals=(_as_refusal(exc),), run_up=None)
    if check_data:
        availability = check_availability(resolved, protocol.symbol, run_up.data_start, et_date_at_ms(protocol.final_end_ms - 1))
        if not availability.is_complete:
            refusal = _refusal(data_missing_refusal(MissingSessionsError(availability)))
            return PlanReview(protocol=protocol, review=review, refusals=(_as_refusal(refusal),), run_up=run_up)
    return PlanReview(protocol=protocol, review=review, refusals=(), run_up=run_up)


def preflight_view(plan: PlanReview, exposure: Mapping[str, Any] | None) -> dict[str, Any]:
    """The preflight answer: refusals, the estimate against the cap, folds, exposure and run-up."""
    review = plan.review
    study_estimate = review.estimate if review is not None else None
    return {
        "refusals": [refusal.as_dict() for refusal in plan.refusals],
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
    """The first month start leaving at least one whole calendar month of lake history for the run-up."""
    month = _month_start(earliest_session)
    first_session = _first_session_on_or_after(month)
    return add_months(month, 1 if earliest_session <= first_session else 2)


def _whole_months(start: date, end: date) -> int:
    return max(0, (end.year - start.year) * 12 + (end.month - start.month))


def default_protocol(
    strategy_key: str,
    symbol: str,
    incumbent: IncumbentRef,
    *,
    now_ms: int,
    earliest_session: date | None,
    final_months: int = DEFAULT_FINAL_MONTHS,
    training_months: int = DEFAULT_TRAINING_MONTHS,
    test_months: int = DEFAULT_TEST_MONTHS,
) -> GoldenSearchProtocol:
    """A complete starting plan: declared default ranges, the incumbent as seed, and server-computed intervals."""
    declaration = declaration_for(strategy_key)
    if declaration is None:
        reason = unavailable_reason(strategy_key) or "No Golden Search declaration exists for this strategy."
        raise GoldenSearchRefusal(reason, code="STRATEGY_UNAVAILABLE")
    if min(final_months, training_months, test_months) < 1:
        raise GoldenSearchRefusal("Interval lengths must be at least one month.", code="INTERVALS_INVALID")
    final_end_day = _month_start(et_date_at_ms(now_ms))
    final_start_day = add_months(final_end_day, -final_months)
    seed = knob_values(declaration, incumbent.params)
    knobs = tuple(
        KnobPlan(
            name=knob.name,
            mode="search" if knob.searchable_by_default else "fixed",
            low=float(knob.default_low),
            high=float(knob.default_high),
            fixed_value=float(seed[knob.name]),
            step=float(knob.default_step) if knob.searchable_by_default else None,
        )
        for knob in declaration.knobs
    )
    searched = {plan.name for plan in knobs if plan.mode == "search"}
    available = (
        UNKNOWN_HISTORY_MONTHS
        if earliest_session is None
        else _whole_months(_earliest_development_start(earliest_session), final_start_day)
    )
    folds = max(1, (available - training_months) // test_months)

    def plan_with(fold_count: int) -> GoldenSearchProtocol:
        development_start = add_months(final_start_day, -(training_months + fold_count * test_months))
        return GoldenSearchProtocol(
            strategy_key=strategy_key,
            symbol=symbol,
            method="zoom",
            knobs=knobs,
            seed=dict(incumbent.params),
            incumbent=incumbent,
            development_start_ms=et_midnight_ms(development_start),
            development_end_ms=et_midnight_ms(final_start_day),
            final_start_ms=et_midnight_ms(final_start_day),
            final_end_ms=et_midnight_ms(final_end_day),
            training_months=training_months,
            test_months=test_months,
            pair_audits=tuple(pair for pair in declaration.default_pair_audits if set(pair) <= searched),
        )

    protocol = plan_with(folds)
    while folds > 1 and estimate(protocol, folds=folds).total_max > protocol.budget_cap:
        folds -= 1
        protocol = plan_with(folds)
    return protocol
