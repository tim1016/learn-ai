"""Exhaustive Grid procedure and the bounded audit grids around a chosen point.

Formula: each searched knob expands to ``low, low + step, …`` up to ``high``
inclusive in exact rational arithmetic (``app.research.sweep.grid.expand_param``),
each fixed knob holds its fixed value, and the grid is the cartesian product
in protocol knob order. A combination that violates a declared constraint is
counted invalid and never evaluated; the rest are evaluated in batches of at
most :data:`GRID_BATCH_SIZE`. The winner is ``selection.best`` over every
evaluated combination; with none eligible the winner is the seed and the stop
reason ``no_eligible``. A pair audit is a landscape over two searched knobs:
each axis takes ``size`` values evenly spaced over that knob's planned range
``[low, high]`` (quantized, deduped), with the value nearest the center's —
the smaller one on a tie — replaced by the center's exact value, so the
candidate's own cell is always on the map; every other knob stays at the
center. A neighbor probe moves one knob one ``neighbor_step`` either way. A
value outside the knob's legal domain is untested (outside domain), a
constraint violation is invalid, the rest are testable canonical points.
Upper bounds: one Grid run, the product of the searched axes' sizes; one
pair audit, ``size²``.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Use the two
  search tools for different questions" and "Compare candidates and weaknesses".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_grid_procedure.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import product
from typing import Any, Literal

from app.research.golden_search.declarations import (
    Canonicalize,
    SearchDeclaration,
    SearchKnob,
    canonicalizer,
    knob_scalars,
    knob_values,
    point_hash,
    quantize,
    to_decimal,
    violates,
)
from app.research.golden_search.protocol import GoldenSearchProtocol, KnobPlan, grid_size
from app.research.golden_search.selection import Candidate, best
from app.research.golden_search.zoom import (
    BudgetExhausted,
    EvaluateBatch,
    PointEvaluator,
    ProcedureResult,
    StopReason,
    edge_hits,
)
from app.research.sweep.grid import LowHighStepRange, expand_param

GRID_BATCH_SIZE = 50
PAIR_GRID_SIZE = 5


def max_grid_evaluations(protocol: GoldenSearchProtocol) -> int:
    """Upper bound on points one Grid run passes to ``evaluate``; raises when an axis is not a valid range."""
    size = grid_size(protocol)
    if size is None:
        raise ValueError("every searched knob needs a valid low/high/step range")
    return size


def _axis(protocol: GoldenSearchProtocol, name: str) -> list[Decimal]:
    plan = next(plan for plan in protocol.knobs if plan.name == name)
    if plan.mode == "fixed":
        return [to_decimal(plan.fixed_value)]
    if plan.step is None:
        raise ValueError(f"{name} is searched without a step")
    return [to_decimal(value) for value in expand_param(LowHighStepRange(low=plan.low, high=plan.high, step=plan.step))]


def run_grid(
    *,
    declaration: SearchDeclaration,
    protocol: GoldenSearchProtocol,
    seed: Mapping[str, Any],
    evaluate: EvaluateBatch,
    canonicalize: Canonicalize | None = None,
) -> ProcedureResult:
    """Evaluate every valid combination of the planned grid and pick the canonical winner."""
    build = canonicalize or canonicalizer(declaration.strategy_key, protocol.symbol)
    evaluator = PointEvaluator(strategy_key=declaration.strategy_key, evaluate=evaluate)
    names = [plan.name for plan in protocol.knobs]
    axes = [_axis(protocol, name) for name in names]
    # A declared knob the plan does not list stays at the seed's value.
    base = knob_values(declaration, seed)
    invalid = 0
    candidates: list[Candidate] = []
    pending: list[dict[str, Any]] = []
    stop: StopReason = "no_improvement"

    def flush() -> None:
        results = evaluator.run(pending)
        candidates.extend(
            Candidate(point_hash=point_hash(declaration.strategy_key, point), point=point, metrics=metrics)
            for point, metrics in zip(pending, results, strict=True)
        )
        pending.clear()

    try:
        for combo in product(*axes):
            values = {**base, **dict(zip(names, combo, strict=True))}
            if violates(declaration, values) is not None:
                invalid += 1
                continue
            pending.append(build(knob_scalars(declaration, values)))
            if len(pending) == GRID_BATCH_SIZE:
                flush()
        if pending:
            flush()
    except BudgetExhausted:
        stop = "budget"

    winner = best(candidates, protocol.policy)
    if winner is None:
        seed_point = build(knob_scalars(declaration, base))
        seed_hash = point_hash(declaration.strategy_key, seed_point)
        return ProcedureResult(
            winner=seed_point,
            winner_hash=seed_hash,
            winner_metrics=evaluator.metrics_by_hash.get(seed_hash),
            stop_reason="budget" if stop == "budget" else "no_eligible",
            rounds=(),
            edge_hits=(),
            evaluated_hashes=tuple(evaluator.evaluated),
            invalid_points=invalid,
            method="grid",
        )
    return ProcedureResult(
        winner=winner.point,
        winner_hash=winner.point_hash,
        winner_metrics=winner.metrics,
        stop_reason=stop,
        rounds=(),
        edge_hits=edge_hits(protocol.knobs, knob_values(declaration, winner.point)),
        evaluated_hashes=tuple(evaluator.evaluated),
        invalid_points=invalid,
        method="grid",
    )


# ── Audit grids ──────────────────────────────────────────────────────────

AuditStatus = Literal["testable", "invalid", "outside_domain"]


@dataclass(frozen=True)
class AuditCell:
    """One audited setting: the audited knobs' values and whether it can be evaluated."""

    values: dict[str, float]
    status: AuditStatus
    point: dict[str, Any] | None
    point_hash: str | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "values": dict(self.values),
            "status": self.status,
            "point": None if self.point is None else dict(self.point),
            "point_hash": self.point_hash,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PairGrid:
    """A pair audit: rows are the pair's first knob (``y``), columns its second (``x``)."""

    x_knob: str
    y_knob: str
    x_values: tuple[float, ...]
    y_values: tuple[float, ...]
    # Row-major: every x for the first y, then every x for the next.
    cells: tuple[AuditCell, ...]

    @property
    def testable(self) -> list[AuditCell]:
        return [cell for cell in self.cells if cell.status == "testable"]


def _audit_cell(
    declaration: SearchDeclaration,
    base: Mapping[str, Decimal],
    changes: Mapping[str, Decimal],
    build: Canonicalize,
) -> AuditCell:
    shown = {name: float(value) for name, value in changes.items()}
    for name, value in changes.items():
        knob = declaration.knob(name)
        if not knob.domain_low <= value <= knob.domain_high:
            return AuditCell(values=shown, status="outside_domain", point=None, point_hash=None, reason="untested: outside the legal domain")
    assignment = {**base, **changes}
    message = violates(declaration, assignment)
    if message is not None:
        return AuditCell(values=shown, status="invalid", point=None, point_hash=None, reason=message)
    point = build(knob_scalars(declaration, assignment))
    return AuditCell(
        values=shown,
        status="testable",
        point=point,
        point_hash=point_hash(declaration.strategy_key, point),
        reason=None,
    )


def _landscape_axis(knob: SearchKnob, plan: KnobPlan, center: Decimal, size: int) -> list[Decimal]:
    low, high = to_decimal(plan.low), to_decimal(plan.high)
    values = {quantize(knob, low + (high - low) * index / (size - 1)) for index in range(size)}
    nearest = min(values, key=lambda value: (abs(value - center), value))
    return sorted((values - {nearest}) | {center})


def pair_grid(
    declaration: SearchDeclaration,
    protocol: GoldenSearchProtocol,
    center: Mapping[str, Any],
    a: str,
    b: str,
    *,
    size: int = PAIR_GRID_SIZE,
    canonicalize: Canonicalize | None = None,
) -> PairGrid:
    """The landscape of searched knobs ``a`` (rows) and ``b`` (columns) over their planned ranges, through ``center``."""
    if size < 2:
        raise ValueError("a pair audit needs at least two values per axis")
    plans = {plan.name: plan for plan in protocol.knobs}
    for name in (a, b):
        if name not in plans or plans[name].mode != "search":
            raise ValueError(f"{name} is not a searched knob of this plan")
    build = canonicalize or canonicalizer(declaration.strategy_key, protocol.symbol)
    base = knob_values(declaration, center)
    rows = _landscape_axis(declaration.knob(a), plans[a], base[a], size)
    columns = _landscape_axis(declaration.knob(b), plans[b], base[b], size)
    cells = tuple(_audit_cell(declaration, base, {a: row, b: column}, build) for row in rows for column in columns)
    return PairGrid(
        x_knob=b,
        y_knob=a,
        x_values=tuple(float(value) for value in columns),
        y_values=tuple(float(value) for value in rows),
        cells=cells,
    )


def neighbor_probes(
    declaration: SearchDeclaration,
    center: Mapping[str, Any],
    knob_name: str,
    *,
    canonicalize: Canonicalize | None = None,
) -> tuple[AuditCell, AuditCell]:
    """The settings one neighbor step below and above the canonical ``center`` on one knob, everything else unchanged."""
    build = canonicalize or canonicalizer(declaration.strategy_key, str(center["symbol"]))
    base = knob_values(declaration, center)
    step = declaration.knob(knob_name).neighbor_step
    below = _audit_cell(declaration, base, {knob_name: base[knob_name] - step}, build)
    above = _audit_cell(declaration, base, {knob_name: base[knob_name] + step}, build)
    return below, above
