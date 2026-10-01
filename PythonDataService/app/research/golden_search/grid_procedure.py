"""Exhaustive Grid procedure and the bounded audit grids around a chosen point.

Formula: each searched knob expands to ``low, low + step, …`` up to ``high``
inclusive in exact rational arithmetic (``app.research.sweep.grid.expand_param``),
each fixed knob holds its fixed value, and the grid is the cartesian product
in protocol knob order. A combination that violates a declared constraint is
counted invalid and never evaluated; the rest are evaluated in batches of at
most :data:`GRID_BATCH_SIZE`. The winner is ``selection.best`` over every
evaluated combination; with none eligible the winner is the seed and the stop
reason ``no_eligible``. An audit grid places ``2·half_width + 1`` values per
knob at ``center ± k·neighbor_step``: a value outside the knob's legal domain
is untested (outside domain), a constraint violation is invalid, the rest
are testable canonical points. Upper bound for one Grid run: the product of
the searched axes' sizes.
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
    canonicalizer,
    knob_scalars,
    knob_values,
    point_hash,
    to_decimal,
    violates,
)
from app.research.golden_search.protocol import GoldenSearchProtocol, grid_size
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
PAIR_HALF_WIDTH = 2


def max_grid_evaluations(protocol: GoldenSearchProtocol) -> int:
    """Upper bound on points one Grid run passes to ``evaluate``; raises when an axis is not a valid range."""
    size = grid_size(protocol)
    if size is None:
        raise ValueError("every searched knob needs a valid low/high/grid_step range")
    return size


def _axis(protocol: GoldenSearchProtocol, name: str) -> list[Decimal]:
    plan = next(plan for plan in protocol.knobs if plan.name == name)
    if plan.mode == "fixed":
        return [to_decimal(plan.fixed_value)]
    if plan.grid_step is None:
        raise ValueError(f"{name} is searched without a grid step")
    return [to_decimal(value) for value in expand_param(LowHighStepRange(low=plan.low, high=plan.high, step=plan.grid_step))]


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


def _offsets(center: Decimal, step: Decimal, half_width: int) -> list[Decimal]:
    return [center + step * offset for offset in range(-half_width, half_width + 1)]


def pair_grid(
    declaration: SearchDeclaration,
    center: Mapping[str, Any],
    a: str,
    b: str,
    *,
    half_width: int = PAIR_HALF_WIDTH,
    canonicalize: Canonicalize | None = None,
) -> PairGrid:
    """The ``(2·half_width + 1)²`` grid of knobs ``a`` (x) and ``b`` (y) around the canonical ``center`` at their neighbor steps."""
    build = canonicalize or canonicalizer(declaration.strategy_key, str(center["symbol"]))
    base = knob_values(declaration, center)
    x_values = _offsets(base[a], declaration.knob(a).neighbor_step, half_width)
    y_values = _offsets(base[b], declaration.knob(b).neighbor_step, half_width)
    cells = tuple(_audit_cell(declaration, base, {a: x, b: y}, build) for y in y_values for x in x_values)
    return PairGrid(
        x_knob=a,
        y_knob=b,
        x_values=tuple(float(value) for value in x_values),
        y_values=tuple(float(value) for value in y_values),
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
