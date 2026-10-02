"""Deterministic coordinate Zoom: improve a starting point one knob at a time, within a budget.

Formula: ``current`` starts at the seed's canonical point and is evaluated
first, alone. For pass ``p`` in ``0..passes-1``, for each searched knob in
protocol order (most important first, ADR 0074 decision 10), with ``[lo, hi]`` its plan range and ``step`` its plan's
smallest step, for round ``r`` in ``0..refinements``, with
``spacing = (hi − lo) / (points − 1)``:
  1. when ``spacing > step``, sample ``points`` values evenly spaced over
     ``[lo, hi]`` (ends included) in exact ``Decimal`` and quantize each
     (half-even to the knob's quantum, clamped to its domain); when
     ``spacing <= step``, sample instead every value of the plan's step
     lattice ``plan low + k · step`` inside ``[lo, hi]`` (at most ``points``
     of them), so no round ever samples finer than the step. Dedupe and add
     the knob's current value — the incumbent value is in every comparison;
  2. a sample whose point (``current`` with this knob replaced) violates a
     declared constraint is recorded invalid and never evaluated; the rest
     are evaluated as one batch, in ascending value order;
  3. among eligible results (``selection.ineligibility``) the best objective
     wins; the current value wins every tie; among non-current values a tie
     goes to the smaller ``|value − current|``, then the smaller value. The
     knob MOVES only when that best is strictly greater than the current
     objective, or the current point is ineligible and the best is eligible;
  4. when ``spacing <= step`` the round tested every value the step allows
     in ``[lo, hi]`` and the knob's refinement stops (the minimum step is
     reached: a quantization limit). Otherwise the next round searches
     ``[chosen − spacing, chosen + spacing]`` clipped to the plan range.
A pass that moves no knob ends the procedure: ``quantization_limit`` when
every searched knob's last round reached its minimum step (no finer move
the plan allows exists), else ``no_improvement`` (no improvement along the
tested moves). ``pass_limit`` when the last pass still moved; ``budget`` when the
evaluator raises :class:`BudgetExhausted` (the winner is ``current`` at that
moment); ``no_eligible`` when the seed and every evaluated point are
ineligible (the winner is the seed). ``budget`` outranks ``no_eligible``
because an unfinished search cannot say nothing was eligible. A knob whose
final value equals its plan range's low or high end is an edge hit.
Coordinate search is local and order-sensitive and can miss a move that
needs two knobs to change together; no stop reason claims an optimum.
Upper bound for one run: ``1 + passes · S · (refinements + 1) · (points + 1)``
points passed to ``evaluate``, ``S`` the searched-knob count.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Use the two
  search tools for different questions"; ties and moves are judged by the
  frozen policy of ``app/research/golden_search/selection.py``.
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_zoom.py.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
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
from app.research.golden_search.protocol import GoldenSearchProtocol, KnobPlan, Method, SelectionPolicy
from app.research.golden_search.selection import Metrics, ineligibility, objective_value

#: Evaluates canonical points; returns one ``Metrics`` per input point, in input order.
EvaluateBatch = Callable[[Sequence[dict[str, Any]]], Sequence[Metrics]]
StopReason = Literal["no_improvement", "pass_limit", "budget", "quantization_limit", "no_eligible"]

STOP_EXPLANATIONS: dict[tuple[Method, StopReason], str] = {
    ("zoom", "no_improvement"): (
        "Stopped: no tested move improved the objective. This is improvement along the tested moves only, "
        "not proof that no better setting exists."
    ),
    ("zoom", "quantization_limit"): (
        "Stopped: every searched knob was refined to its smallest step and no tested move improved the objective."
    ),
    ("zoom", "pass_limit"): "Stopped after the planned number of passes; the last pass was still finding improvements.",
    ("zoom", "budget"): "Stopped early: the evaluation budget ran out. The result is incomplete.",
    ("zoom", "no_eligible"): "No tested setting met your rules, including the starting point.",
    ("grid", "no_improvement"): (
        "Tested every valid combination in the grid. The winner is the best of those combinations only."
    ),
    ("grid", "budget"): "Stopped early: the evaluation budget ran out before the grid was finished. The result is incomplete.",
    ("grid", "no_eligible"): "No combination in the grid met your rules.",
}


class BudgetExhausted(Exception):
    """Raised by an evaluate callback when the study's budget cannot admit another evaluation."""


class RetryAllowanceExhausted(Exception):
    """Raised by a reservation callback when a step has already been retried as often as allowed."""


@dataclass(frozen=True)
class ZoomRound:
    """One refinement round on one knob: what was sampled, skipped, evaluated and chosen."""

    pass_index: int
    knob: str
    round_index: int
    low: float
    high: float
    values: tuple[float, ...]
    invalid: tuple[tuple[float, str], ...]
    results: tuple[tuple[float, str | None], ...]
    chosen: float
    moved: bool
    current_before: float
    # The objective each evaluated value scored (``None`` when undefined), in ``results`` order.
    objectives: tuple[tuple[float, float | None], ...] = ()
    # True on the round where this knob's refinement stopped because the sample spacing
    # reached the plan's smallest step for the knob.
    quantization_limit: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "pass_index": self.pass_index,
            "knob": self.knob,
            "round_index": self.round_index,
            "low": self.low,
            "high": self.high,
            "values": list(self.values),
            "invalid": [[value, message] for value, message in self.invalid],
            "results": [[value, code] for value, code in self.results],
            "objectives": [[value, objective] for value, objective in self.objectives],
            "chosen": self.chosen,
            "moved": self.moved,
            "current_before": self.current_before,
            "quantization_limit": self.quantization_limit,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ZoomRound:
        return cls(
            pass_index=int(data["pass_index"]),
            knob=str(data["knob"]),
            round_index=int(data["round_index"]),
            low=float(data["low"]),
            high=float(data["high"]),
            values=tuple(float(value) for value in data["values"]),
            invalid=tuple((float(value), str(message)) for value, message in data["invalid"]),
            results=tuple((float(value), code) for value, code in data["results"]),
            objectives=tuple((float(value), objective) for value, objective in data["objectives"]),
            chosen=float(data["chosen"]),
            moved=bool(data["moved"]),
            current_before=float(data["current_before"]),
            quantization_limit=bool(data["quantization_limit"]),
        )


@dataclass(frozen=True)
class ProcedureResult:
    """The outcome of one Zoom or Grid procedure over one window."""

    winner: dict[str, Any]
    winner_hash: str
    winner_metrics: Metrics | None
    stop_reason: StopReason
    rounds: tuple[ZoomRound, ...]
    edge_hits: tuple[str, ...]
    evaluated_hashes: tuple[str, ...]
    # Distinct constraint-violating assignments skipped without evaluation.
    invalid_points: int = 0
    method: Method = "zoom"

    @property
    def stop_explanation(self) -> str:
        return STOP_EXPLANATIONS[(self.method, self.stop_reason)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "winner": dict(self.winner),
            "winner_hash": self.winner_hash,
            "winner_metrics": None if self.winner_metrics is None else self.winner_metrics.as_dict(),
            "stop_reason": self.stop_reason,
            "stop_explanation": self.stop_explanation,
            "rounds": [round_.as_dict() for round_ in self.rounds],
            "edge_hits": list(self.edge_hits),
            "evaluated_hashes": list(self.evaluated_hashes),
            "invalid_points": self.invalid_points,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProcedureResult:
        metrics = data["winner_metrics"]
        return cls(
            winner=dict(data["winner"]),
            winner_hash=str(data["winner_hash"]),
            winner_metrics=None if metrics is None else Metrics.from_dict(metrics),
            stop_reason=data["stop_reason"],
            rounds=tuple(ZoomRound.from_dict(item) for item in data["rounds"]),
            edge_hits=tuple(str(name) for name in data["edge_hits"]),
            evaluated_hashes=tuple(str(value) for value in data["evaluated_hashes"]),
            invalid_points=int(data["invalid_points"]),
            method=data["method"],
        )


@dataclass
class PointEvaluator:
    """Routes canonical points to ``evaluate`` and remembers every result by point hash.

    Points identical within one batch are sent once. Points already
    evaluated in an earlier batch are sent again: the study evaluator owns
    the cache, and a re-sent point is how a procedure sees the same answer.
    """

    strategy_key: str
    evaluate: EvaluateBatch
    metrics_by_hash: dict[str, Metrics] = field(default_factory=dict)
    evaluated: list[str] = field(default_factory=list)
    submitted: int = 0

    def run(self, points: Sequence[dict[str, Any]]) -> list[Metrics]:
        hashes = [point_hash(self.strategy_key, point) for point in points]
        unique: dict[str, dict[str, Any]] = {}
        for digest, point in zip(hashes, points, strict=True):
            unique.setdefault(digest, point)
        if not unique:
            return []
        batch = list(unique.values())
        self.submitted += len(batch)
        results = list(self.evaluate(batch))
        if len(results) != len(batch):
            raise ValueError(f"evaluate returned {len(results)} results for {len(batch)} points")
        for digest, metrics in zip(unique, results, strict=True):
            if digest not in self.metrics_by_hash:
                self.evaluated.append(digest)
            self.metrics_by_hash[digest] = metrics
        return [self.metrics_by_hash[digest] for digest in hashes]

    def any_eligible(self, policy: SelectionPolicy) -> bool:
        return any(ineligibility(metrics, policy) is None for metrics in self.metrics_by_hash.values())


def max_evaluations(protocol: GoldenSearchProtocol) -> int:
    """Upper bound on points one Zoom run passes to ``evaluate``."""
    settings = protocol.zoom
    searched = len(protocol.search_knobs)
    return 1 + settings.passes * searched * (settings.refinements + 1) * (settings.points + 1)


def edge_hits(plans: Sequence[KnobPlan], values: Mapping[str, Decimal]) -> tuple[str, ...]:
    """Searched knobs whose value sits on either end of its planned range."""
    return tuple(
        plan.name
        for plan in plans
        if plan.mode == "search" and values[plan.name] in (to_decimal(plan.low), to_decimal(plan.high))
    )


def _sample(knob: SearchKnob, low: Decimal, high: Decimal, points: int) -> set[Decimal]:
    span = high - low
    return {quantize(knob, low + span * index / (points - 1)) for index in range(points)}


def _lattice(knob: SearchKnob, origin: Decimal, step: Decimal, low: Decimal, high: Decimal) -> set[Decimal]:
    """Every ``origin + k·step`` inside ``[low, high]`` — the finest values a plan with this step allows there."""
    first = int(((low - origin) / step).to_integral_value(rounding=ROUND_CEILING))
    last = int(((high - origin) / step).to_integral_value(rounding=ROUND_FLOOR))
    return {quantize(knob, origin + step * index) for index in range(max(first, 0), last + 1)}


def _assignment_key(values: Mapping[str, Decimal]) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted(values.items()))


@dataclass(frozen=True)
class _Choice:
    value: Decimal
    metrics: Metrics
    moved: bool


def _choose(
    current: Decimal,
    current_metrics: Metrics,
    evaluated: Sequence[tuple[Decimal, Metrics]],
    policy: SelectionPolicy,
) -> _Choice:
    current_objective = objective_value(current_metrics, policy) if ineligibility(current_metrics, policy) is None else None
    best: tuple[tuple[float, Decimal, Decimal], Decimal, Metrics] | None = None
    for value, metrics in evaluated:
        if value == current or ineligibility(metrics, policy) is not None:
            continue
        objective = objective_value(metrics, policy)
        assert objective is not None  # an eligible result has a finite objective
        key = (-objective, abs(value - current), value)
        if best is None or key < best[0]:
            best = (key, value, metrics)
    if best is None:
        return _Choice(value=current, metrics=current_metrics, moved=False)
    best_objective = -best[0][0]
    if current_objective is None or best_objective > current_objective:
        return _Choice(value=best[1], metrics=best[2], moved=True)
    return _Choice(value=current, metrics=current_metrics, moved=False)


class _ZoomRun:
    """One Zoom procedure's mutable state: the current point, its metrics and the recorded path."""

    def __init__(
        self,
        declaration: SearchDeclaration,
        protocol: GoldenSearchProtocol,
        seed: Mapping[str, Any],
        evaluate: EvaluateBatch,
        build: Canonicalize,
        policy: SelectionPolicy,
    ) -> None:
        self.declaration = declaration
        self.protocol = protocol
        self.policy = policy
        self.settings = protocol.zoom
        self.build = build
        self.evaluator = PointEvaluator(strategy_key=declaration.strategy_key, evaluate=evaluate)
        self.current = knob_values(declaration, seed)
        violation = violates(declaration, self.current)
        if violation is not None:
            raise ValueError(f"the starting point breaks a rule: {violation}")
        self.seed_values = dict(self.current)
        self.current_metrics: Metrics | None = None
        self.rounds: list[ZoomRound] = []
        self.invalid_assignments: set[tuple[tuple[str, Decimal], ...]] = set()

    def point(self, values: Mapping[str, Decimal]) -> dict[str, Any]:
        return self.build(knob_scalars(self.declaration, values))

    def result(self, stop: StopReason, values: Mapping[str, Decimal], metrics: Metrics | None) -> ProcedureResult:
        winner = self.point(values)
        return ProcedureResult(
            winner=winner,
            winner_hash=point_hash(self.declaration.strategy_key, winner),
            winner_metrics=metrics,
            stop_reason=stop,
            rounds=tuple(self.rounds),
            edge_hits=edge_hits(self.protocol.knobs, values),
            evaluated_hashes=tuple(self.evaluator.evaluated),
            invalid_points=len(self.invalid_assignments),
        )

    def run(self) -> ProcedureResult:
        try:
            (self.current_metrics,) = self.evaluator.run([self.point(self.current)])
        except BudgetExhausted:
            return self.result("budget", self.current, None)
        try:
            stop = self._passes()
        except BudgetExhausted:
            return self.result("budget", self.current, self.current_metrics)
        if not self.evaluator.any_eligible(self.policy):
            seed_hash = point_hash(self.declaration.strategy_key, self.point(self.seed_values))
            return self.result("no_eligible", self.seed_values, self.evaluator.metrics_by_hash[seed_hash])
        return self.result(stop, self.current, self.current_metrics)

    def _passes(self) -> StopReason:
        for pass_index in range(self.settings.passes):
            outcomes = [self._refine(pass_index, plan) for plan in self.protocol.search_knobs]
            if not any(moved for moved, _ in outcomes):
                return "quantization_limit" if all(quantized for _, quantized in outcomes) else "no_improvement"
        return "pass_limit"

    def _refine(self, pass_index: int, plan: KnobPlan) -> tuple[bool, bool]:
        """Every refinement round on one knob; returns (moved, reached the plan's step)."""
        knob = self.declaration.knob(plan.name)
        if plan.step is None:
            raise ValueError(f"{plan.name} is searched without a step")
        step = to_decimal(plan.step)
        plan_low, plan_high = to_decimal(plan.low), to_decimal(plan.high)
        low, high = plan_low, plan_high
        moved = reached_step = False
        for round_index in range(self.settings.refinements + 1):
            spacing = (high - low) / (self.settings.points - 1)
            reached_step = spacing <= step
            if reached_step:
                sample = _lattice(knob, plan_low, step, low, high)
            else:
                sample = _sample(knob, low, high, self.settings.points)
            chosen = self._round(pass_index, round_index, knob, low, high, sample, reached_step=reached_step)
            moved = moved or self.rounds[-1].moved
            if reached_step:
                break
            low, high = max(plan_low, chosen - spacing), min(plan_high, chosen + spacing)
            if low >= high:
                break
        return moved, reached_step

    def _round(
        self,
        pass_index: int,
        round_index: int,
        knob: SearchKnob,
        low: Decimal,
        high: Decimal,
        sample: set[Decimal],
        *,
        reached_step: bool,
    ) -> Decimal:
        """Evaluate one round's sample and choose once; moves ``current`` when the choice improves on it."""
        before = self.current[knob.name]
        values = sorted(sample | {before})
        invalid: list[tuple[Decimal, str]] = []
        testable: list[tuple[Decimal, dict[str, Any]]] = []
        for value in values:
            assignment = {**self.current, knob.name: value}
            message = violates(self.declaration, assignment)
            if message is None:
                testable.append((value, self.point(assignment)))
            else:
                invalid.append((value, message))
                self.invalid_assignments.add(_assignment_key(assignment))
        results = self.evaluator.run([point for _, point in testable])
        evaluated = [(value, metrics) for (value, _), metrics in zip(testable, results, strict=True)]
        current_metrics = next(metrics for value, metrics in evaluated if value == before)
        choice = _choose(before, current_metrics, evaluated, self.policy)
        self.rounds.append(
            ZoomRound(
                pass_index=pass_index,
                knob=knob.name,
                round_index=round_index,
                low=float(low),
                high=float(high),
                values=tuple(float(value) for value in values),
                invalid=tuple((float(value), message) for value, message in invalid),
                results=tuple((float(value), ineligibility(metrics, self.policy)) for value, metrics in evaluated),
                objectives=tuple((float(value), objective_value(metrics, self.policy)) for value, metrics in evaluated),
                chosen=float(choice.value),
                moved=choice.moved,
                current_before=float(before),
                quantization_limit=reached_step,
            )
        )
        self.current[knob.name] = choice.value
        self.current_metrics = choice.metrics
        return choice.value


def run_zoom(
    *,
    declaration: SearchDeclaration,
    protocol: GoldenSearchProtocol,
    seed: Mapping[str, Any],
    evaluate: EvaluateBatch,
    canonicalize: Canonicalize | None = None,
    policy: SelectionPolicy | None = None,
) -> ProcedureResult:
    """Run the Zoom procedure from ``seed``; ``canonicalize`` defaults to the registered model's canonical point.

    ``policy`` is the evaluated window's selection policy, its trade floor
    resolved (``activity.TradeFloors``); it defaults to the protocol's own.

    Every knob starts at the seed's value (an omitted one at its declared
    default); fixed knobs keep it, which is why a locked plan's seed must
    equal each fixed value. Raises ``ValueError`` when the seed itself breaks
    a declared constraint.
    """
    build = canonicalize or canonicalizer(declaration.strategy_key, protocol.symbol)
    return _ZoomRun(declaration, protocol, seed, evaluate, build, policy or protocol.policy).run()
