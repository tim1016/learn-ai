"""The frozen Golden Search plan and every reason it can be refused before a study exists.

Formula: a protocol is the complete plan a study is locked to — knobs (all
declared knobs, in search order, each searched over a quantized range at a
smallest step that is a multiple of its quantum, or held at a fixed value),
seed and frozen incumbent, selection policy, Zoom
settings, the half-open development interval ``[development_start,
development_end)`` and final interval ``[final_start, final_end)`` with
``final_start == development_end``, fold lengths, audits, stress scenarios,
execution assumptions and the evaluation budget. ``protocol_hash`` is the
SHA-256 of its canonical JSON (keys sorted, compact separators, no NaN), so
dict key order never changes it and every value does. Validation returns
every refusal rather than the first, each with a stable code.
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Plan before
  seeing results" and "Validate the procedure without leaking its future".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_protocol.py.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, cast, get_args

from app.research.golden_search.declarations import (
    SearchDeclaration,
    SearchKnob,
    is_quantized,
    to_decimal,
    violates,
)
from app.research.sweep.grid import LowHighStepRange, StrategyGridConfig
from app.research.sweep.grid import grid_size as sweep_grid_size
from app.research.sweep.ranking import RANKING_MEASURES, RankingMeasure
from app.research.walk_forward_study.folds import FoldPlan, FoldPlanError, add_months, plan_folds
from app.schemas.grid_search import SYMBOL_PATTERN, FillModeName
from app.utils.session_anchors import (
    LAST_SCHEDULABLE_DATE,
    MAX_TIMESTAMP_MS,
    et_date_at_ms,
    et_midnight_ms,
    require_schedulable_end,
)

Method = Literal["zoom", "grid"]
KnobMode = Literal["search", "fixed"]
IncumbentSource = Literal["registry", "qualification"]

#: The study-wide ceiling on engine evaluations, proof included (Grid Search's ``MAX_TOTAL_BACKTESTS``).
MAX_BUDGET_CAP = 5_000
ZOOM_POINTS_RANGE = (3, 9)
ZOOM_REFINEMENTS_RANGE = (0, 4)
ZOOM_PASSES_RANGE = (1, 5)
_FILL_MODES: tuple[str, ...] = get_args(FillModeName)
_SYMBOL = re.compile(SYMBOL_PATTERN)


#: The importance scale each knob of a new plan carries (ADR 0074 decision 10): the more important a knob, the
#: earlier Zoom moves it. A new plan starts every knob at the middle of the scale.
IMPORTANCE_LOW = 1
IMPORTANCE_HIGH = 10
DEFAULT_IMPORTANCE = 5


@dataclass(frozen=True)
class KnobPlan:
    """One declared knob's role in the plan: searched over ``[low, high]`` or held at ``fixed_value``."""

    name: str
    mode: KnobMode
    low: float
    high: float
    fixed_value: float
    # The smallest step, required for a searched knob (#2696): Grid samples ``low..high`` by
    # it, and Zoom stops refining the knob once a round's spacing reaches it.
    step: float | None = None
    # IMPORTANCE_LOW..IMPORTANCE_HIGH, higher searched first; ``None`` on a legacy plan, which keeps its own order.
    importance: int | None = None


@dataclass(frozen=True)
class SelectionPolicy:
    objective: RankingMeasure = "sharpe_ratio"
    # ``None`` on a plan with an expected trade frequency: each window's floor comes from its receipt (ADR 0074).
    min_trades: int | None = 30
    # A fraction of peak equity, the engine's ``max_drawdown_pct`` unit.
    max_drawdown_ceiling: float = 0.20
    require_positive_net: bool = True


@dataclass(frozen=True)
class ZoomSettings:
    points: int = 5
    refinements: int = 2
    passes: int = 2


@dataclass(frozen=True)
class ExecutionAssumptions:
    fill_mode: str = "decision_minute_open"
    commission_per_order: float = 0.0
    slippage_per_share: float = 0.0
    initial_cash: float = 100_000.0


@dataclass(frozen=True)
class StressScenario:
    key: str
    label: str
    slippage_add: float = 0.0
    commission_add: float = 0.0
    fill_mode: str | None = None


DEFAULT_STRESS: tuple[StressScenario, ...] = (
    StressScenario("slippage_1c", "Extra 1¢/share slippage", slippage_add=0.01),
    StressScenario("commission_1", "Extra $1 per order", commission_add=1.0),
)
#: The scenario name of an unstressed evaluation; a stress scenario may not take it, or its runs would read back as base runs.
BASE_SCENARIO = "base"


@dataclass(frozen=True)
class IncumbentRef:
    """The comparison frozen at lock: where it came from and its full canonical tuple."""

    source: IncumbentSource
    qualification_id: str | None
    params: dict[str, Any]


@dataclass(frozen=True, kw_only=True)
class GoldenSearchProtocol:
    strategy_key: str
    symbol: str
    method: Method
    knobs: tuple[KnobPlan, ...]
    seed: dict[str, Any]
    incumbent: IncumbentRef
    development_start_ms: int
    development_end_ms: int
    final_start_ms: int
    final_end_ms: int
    policy: SelectionPolicy = field(default_factory=SelectionPolicy)
    zoom: ZoomSettings = field(default_factory=ZoomSettings)
    training_months: int = 6
    test_months: int = 2
    recent_window: bool = True
    pair_audits: tuple[tuple[str, str], ...] = ()
    neighbor_audit: bool = True
    stress: tuple[StressScenario, ...] = DEFAULT_STRESS
    execution: ExecutionAssumptions = field(default_factory=ExecutionAssumptions)
    exam_min_trades: int | None = 30
    budget_cap: int = MAX_BUDGET_CAP
    # ADR 0074: a plan has either this or the two fixed floors above, never both. Absent on legacy plans.
    expected_trades_per_year: int | None = None

    @property
    def search_knobs(self) -> tuple[KnobPlan, ...]:
        return tuple(plan for plan in self.knobs if plan.mode == "search")

    def as_dict(self) -> dict[str, Any]:
        body = {
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "method": self.method,
            "knobs": [_knob_dict(plan) for plan in self.knobs],
            "seed": dict(self.seed),
            "incumbent": {
                "source": self.incumbent.source,
                "qualification_id": self.incumbent.qualification_id,
                "params": dict(self.incumbent.params),
            },
            "policy": {
                "objective": self.policy.objective,
                "min_trades": self.policy.min_trades,
                "max_drawdown_ceiling": self.policy.max_drawdown_ceiling,
                "require_positive_net": self.policy.require_positive_net,
            },
            "zoom": {"points": self.zoom.points, "refinements": self.zoom.refinements, "passes": self.zoom.passes},
            "development_start_ms": self.development_start_ms,
            "development_end_ms": self.development_end_ms,
            "final_start_ms": self.final_start_ms,
            "final_end_ms": self.final_end_ms,
            "training_months": self.training_months,
            "test_months": self.test_months,
            "recent_window": self.recent_window,
            "pair_audits": [[a, b] for a, b in self.pair_audits],
            "neighbor_audit": self.neighbor_audit,
            "stress": [
                {
                    "key": scenario.key,
                    "label": scenario.label,
                    "slippage_add": scenario.slippage_add,
                    "commission_add": scenario.commission_add,
                    "fill_mode": scenario.fill_mode,
                }
                for scenario in self.stress
            ],
            "execution": {
                "fill_mode": self.execution.fill_mode,
                "commission_per_order": self.execution.commission_per_order,
                "slippage_per_share": self.execution.slippage_per_share,
                "initial_cash": self.execution.initial_cash,
            },
            "exam_min_trades": self.exam_min_trades,
            "budget_cap": self.budget_cap,
        }
        if self.expected_trades_per_year is not None:
            body["expected_trades_per_year"] = self.expected_trades_per_year
        return body

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GoldenSearchProtocol:
        """The inverse of :meth:`as_dict`; refuses a missing or unknown key at every level."""
        body = _strict({"expected_trades_per_year": None, **data}, GoldenSearchProtocol, "protocol")
        incumbent = _strict(body["incumbent"], IncumbentRef, "incumbent")
        policy = _strict(body["policy"], SelectionPolicy, "policy")
        zoom = _strict(body["zoom"], ZoomSettings, "zoom")
        execution = _strict(body["execution"], ExecutionAssumptions, "execution")
        return cls(
            strategy_key=str(body["strategy_key"]),
            symbol=str(body["symbol"]),
            method=body["method"],
            knobs=tuple(_knob_plan(_strict({"importance": None, **item}, KnobPlan, "knob")) for item in body["knobs"]),
            seed=dict(body["seed"]),
            incumbent=IncumbentRef(
                source=incumbent["source"],
                qualification_id=incumbent["qualification_id"],
                params=dict(incumbent["params"]),
            ),
            policy=SelectionPolicy(
                objective=policy["objective"],
                min_trades=_optional_int(policy["min_trades"]),
                max_drawdown_ceiling=float(policy["max_drawdown_ceiling"]),
                require_positive_net=bool(policy["require_positive_net"]),
            ),
            zoom=ZoomSettings(points=int(zoom["points"]), refinements=int(zoom["refinements"]), passes=int(zoom["passes"])),
            development_start_ms=int(body["development_start_ms"]),
            development_end_ms=int(body["development_end_ms"]),
            final_start_ms=int(body["final_start_ms"]),
            final_end_ms=int(body["final_end_ms"]),
            training_months=int(body["training_months"]),
            test_months=int(body["test_months"]),
            recent_window=bool(body["recent_window"]),
            pair_audits=tuple((str(a), str(b)) for a, b in body["pair_audits"]),
            neighbor_audit=bool(body["neighbor_audit"]),
            stress=tuple(_stress(_strict(item, StressScenario, "stress scenario")) for item in body["stress"]),
            execution=ExecutionAssumptions(
                fill_mode=str(execution["fill_mode"]),
                commission_per_order=float(execution["commission_per_order"]),
                slippage_per_share=float(execution["slippage_per_share"]),
                initial_cash=float(execution["initial_cash"]),
            ),
            exam_min_trades=_optional_int(body["exam_min_trades"]),
            budget_cap=int(body["budget_cap"]),
            expected_trades_per_year=body["expected_trades_per_year"],
        )

    def protocol_hash(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict()).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """Sorted keys, compact separators, NaN refused — the bytes every Golden Search hash is taken over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _strict(data: object, shape: type, what: str) -> Mapping[str, Any]:
    """``data`` when its keys are exactly ``shape``'s dataclass fields — the keys :meth:`as_dict` writes."""
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be an object")
    keys = {item.name for item in dataclasses.fields(shape)}
    missing = sorted(keys - set(data))
    unknown = sorted(set(data) - set(keys))
    if missing or unknown:
        raise ValueError(f"{what}: missing {missing}, unknown {unknown}")
    return data


def _knob_dict(plan: KnobPlan) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": plan.name,
        "mode": plan.mode,
        "low": plan.low,
        "high": plan.high,
        "fixed_value": plan.fixed_value,
        "step": plan.step,
    }
    # Absent on a legacy knob, so a legacy plan keeps its hash.
    if plan.importance is not None:
        body["importance"] = plan.importance
    return body


def _knob_plan(item: Mapping[str, Any]) -> KnobPlan:
    return KnobPlan(
        name=str(item["name"]),
        mode=item["mode"],
        low=float(item["low"]),
        high=float(item["high"]),
        fixed_value=float(item["fixed_value"]),
        step=None if item["step"] is None else float(item["step"]),
        importance=item["importance"],
    )


def by_importance(knobs: Sequence[KnobPlan], declared: Sequence[str]) -> tuple[KnobPlan, ...]:
    """``knobs`` in search order: higher importance first, ties in the declaration's order (ADR 0074 decision 10).

    A plan where any knob lacks a whole-number importance (a legacy plan, or
    one validation will refuse knob by knob) keeps its own order. Angular's
    ``byImportance`` shows the same order; contracts/fixtures/golden-search-importance-order-v1.json pins both.
    """
    if any(isinstance(plan.importance, bool) or not isinstance(plan.importance, int) for plan in knobs):
        return tuple(knobs)
    rank = {name: index for index, name in enumerate(declared)}
    return tuple(sorted(knobs, key=lambda plan: (-cast(int, plan.importance), rank.get(plan.name, len(rank)))))


def _stress(item: Mapping[str, Any]) -> StressScenario:
    return StressScenario(
        key=str(item["key"]),
        label=str(item["label"]),
        slippage_add=float(item["slippage_add"]),
        commission_add=float(item["commission_add"]),
        fill_mode=None if item["fill_mode"] is None else str(item["fill_mode"]),
    )


# ── Windows derived from the plan ───────────────────────────────────────


def development_folds(protocol: GoldenSearchProtocol) -> list[FoldPlan]:
    """The canonical walk-forward folds over the development interval; raises ``FoldPlanError``."""
    return plan_folds(
        start=et_date_at_ms(protocol.development_start_ms),
        end_exclusive=et_date_at_ms(protocol.development_end_ms),
        training_months=protocol.training_months,
        test_months=protocol.test_months,
    )


def recent_window_ms(protocol: GoldenSearchProtocol) -> tuple[int, int]:
    """``[start, development_end)``: the last ``training_months`` whole months of development, ET-midnight anchored."""
    end_day = et_date_at_ms(protocol.development_end_ms)
    return et_midnight_ms(add_months(end_day, -protocol.training_months)), protocol.development_end_ms




# ── Validation ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProtocolRefusal:
    code: str
    field: str | None
    message: str

    def as_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "field": self.field, "message": self.message}


class _Refusals:
    """Collects every refusal in the order the checks run."""

    def __init__(self) -> None:
        self.items: list[ProtocolRefusal] = []

    def add(self, code: str, field_name: str | None, message: str) -> None:
        self.items.append(ProtocolRefusal(code=code, field=field_name, message=message))

    def any_for(self, field_prefix: str) -> bool:
        return any((refusal.field or "").startswith(field_prefix) for refusal in self.items)


def validate_protocol(p: GoldenSearchProtocol, declaration: SearchDeclaration) -> list[ProtocolRefusal]:
    """Every reason this plan cannot be locked, in a stable order; empty when it can.

    The study-wide workload ceiling needs the procedure bounds and is
    checked by ``app.research.golden_search.budget.review_protocol``, which
    runs this first.
    """
    refusals = _Refusals()
    if p.strategy_key != declaration.strategy_key:
        refusals.add(
            "STRATEGY_MISMATCH",
            "strategy_key",
            f"The plan is for {p.strategy_key!r} but the declaration is for {declaration.strategy_key!r}.",
        )
    if not _SYMBOL.match(p.symbol):
        refusals.add("SYMBOL_INVALID", "symbol", f"{p.symbol!r} is not a ticker symbol.")
    if p.method not in get_args(Method):
        refusals.add("METHOD_INVALID", "method", f"The search method must be zoom or grid, not {p.method!r}.")
    _validate_knobs(p, declaration, refusals)
    _validate_seed(p, declaration, refusals)
    _validate_incumbent(p, refusals)
    _validate_zoom(p, refusals)
    _validate_intervals(p, refusals)
    _validate_policy(p, refusals)
    _validate_audits(p, refusals)
    _validate_execution(p, refusals)
    return refusals.items


def _finite(*values: object) -> bool:
    return all(isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) for value in values)


def _validate_knobs(p: GoldenSearchProtocol, declaration: SearchDeclaration, refusals: _Refusals) -> None:
    declared = {knob.name: knob for knob in declaration.knobs}
    names = [plan.name for plan in p.knobs]
    for name in sorted({name for name in names if names.count(name) > 1}):
        refusals.add("DUPLICATE_KNOB", f"knobs.{name}", f"{name} appears more than once in the plan.")
    for name in names:
        if name not in declared:
            refusals.add("UNKNOWN_KNOB", f"knobs.{name}", f"{name} is not a declared knob of {declaration.strategy_key}.")
    for name in declared:
        if name not in names:
            refusals.add("MISSING_KNOB", f"knobs.{name}", f"{name} must be searched or held fixed; the plan omits it.")
    if not any(plan.mode == "search" for plan in p.knobs if plan.name in declared):
        refusals.add("NO_SEARCHABLE_KNOB", "knobs", "Search at least one knob; with every knob fixed there is nothing to search.")
    for plan in p.knobs:
        knob = declared.get(plan.name)
        if knob is not None:
            _validate_knob_plan(plan, knob, refusals)
    _validate_importance(p, declared, refusals)
    if p.method == "grid" and not refusals.any_for("knobs."):
        size = grid_size(p)
        if size is None:
            refusals.add("STEP_INVALID", "knobs", "The grid cannot be expanded from these ranges and steps.")
        elif size > p.budget_cap:
            refusals.add(
                "GRID_TOO_LARGE",
                "knobs",
                f"The grid has {size} combinations, more than the budget of {p.budget_cap} evaluations.",
            )


def _validate_importance(p: GoldenSearchProtocol, declared: Mapping[str, SearchKnob], refusals: _Refusals) -> None:
    """Every knob carries an importance on the scale, or none does (a legacy plan, searched in its own order)."""
    if all(plan.importance is None for plan in p.knobs):
        return
    for plan in p.knobs:
        label = declared[plan.name].label if plan.name in declared else plan.name
        value = plan.importance
        if isinstance(value, bool) or not isinstance(value, int) or not IMPORTANCE_LOW <= value <= IMPORTANCE_HIGH:
            refusals.add(
                "IMPORTANCE_INVALID",
                f"knobs.{plan.name}.importance",
                f"{label}: the importance must be a whole number from {IMPORTANCE_LOW} to {IMPORTANCE_HIGH}.",
            )


def _validate_knob_plan(plan: KnobPlan, knob: SearchKnob, refusals: _Refusals) -> None:
    where = f"knobs.{plan.name}"
    if plan.mode not in get_args(KnobMode):
        refusals.add("KNOB_MODE_INVALID", where, f"{knob.label}: the mode must be search or fixed, not {plan.mode!r}.")
        return
    if plan.mode == "fixed":
        if not _finite(plan.fixed_value):
            refusals.add("FIXED_OUTSIDE_DOMAIN", where, f"{knob.label}: the fixed value must be a finite number.")
            return
        value = to_decimal(plan.fixed_value)
        if not knob.domain_low <= value <= knob.domain_high:
            refusals.add(
                "FIXED_OUTSIDE_DOMAIN",
                where,
                f"{knob.label}: the fixed value {value} is outside {knob.domain_low}–{knob.domain_high} {knob.unit}.",
            )
        elif not is_quantized(knob, value):
            refusals.add("FIXED_NOT_QUANTIZED", where, f"{knob.label}: the fixed value {value} is not a multiple of {knob.quantum}.")
        return
    if not _finite(plan.low, plan.high):
        refusals.add("RANGE_OUTSIDE_DOMAIN", where, f"{knob.label}: the search range must be finite numbers.")
        return
    low, high = to_decimal(plan.low), to_decimal(plan.high)
    if not (knob.domain_low <= low and high <= knob.domain_high):
        refusals.add(
            "RANGE_OUTSIDE_DOMAIN",
            where,
            f"{knob.label}: {low}–{high} leaves the legal domain {knob.domain_low}–{knob.domain_high} {knob.unit}.",
        )
    if low >= high:
        refusals.add("RANGE_EMPTY", where, f"{knob.label}: the low end {low} must be below the high end {high}.")
    if not (is_quantized(knob, low) and is_quantized(knob, high)):
        refusals.add("RANGE_NOT_QUANTIZED", where, f"{knob.label}: both ends must be multiples of {knob.quantum}.")
    _validate_step(plan, knob, low, high, refusals)


def _validate_step(plan: KnobPlan, knob: SearchKnob, low: Decimal, high: Decimal, refusals: _Refusals) -> None:
    where = f"knobs.{plan.name}.step"
    if plan.step is None:
        refusals.add("STEP_MISSING", where, f"{knob.label}: a searched knob needs its smallest step.")
        return
    if not _finite(plan.step) or plan.step <= 0:
        refusals.add("STEP_INVALID", where, f"{knob.label}: the step must be a positive number.")
        return
    step = to_decimal(plan.step)
    if not is_quantized(knob, step):
        refusals.add("STEP_INVALID", where, f"{knob.label}: the step {step} must be a multiple of {knob.quantum}.")
    elif low < high and step > high - low:
        refusals.add(
            "STEP_INVALID",
            where,
            f"{knob.label}: the step {step} is wider than the searched range {low}–{high}, so only {low} would be tested.",
        )


def grid_size(p: GoldenSearchProtocol) -> int | None:
    """The product of the searched axes' sizes, or ``None`` when an axis is not a valid low/high/step range."""
    counts = knob_value_counts(p)
    sizes = [counts[plan.name] for plan in p.search_knobs]
    if any(size is None for size in sizes):
        return None
    return math.prod(size for size in sizes if size is not None)


def knob_value_counts(p: GoldenSearchProtocol) -> dict[str, int | None]:
    """How many settings each knob can take: 1 when held, its range's size at its step when searched, ``None`` when that range is not valid.

    A Grid plan's size is the product of the searched knobs' counts (:func:`grid_size`).
    """
    counts: dict[str, int | None] = {}
    for plan in p.knobs:
        if plan.mode != "search":
            counts[plan.name] = 1
            continue
        if plan.step is None:
            counts[plan.name] = None
            continue
        axis = {plan.name: LowHighStepRange(low=plan.low, high=plan.high, step=plan.step)}
        try:
            counts[plan.name] = sweep_grid_size([StrategyGridConfig(strategy_key=p.strategy_key, param_ranges=axis)], [p.symbol])
        except ValueError:
            counts[plan.name] = None
    return counts


def _validate_seed(p: GoldenSearchProtocol, declaration: SearchDeclaration, refusals: _Refusals) -> None:
    declared = {knob.name: knob for knob in declaration.knobs}
    seed_symbol = p.seed.get("symbol")
    if seed_symbol != p.symbol:
        refusals.add("SEED_SYMBOL_MISMATCH", "seed.symbol", f"The starting point is for {seed_symbol!r}, not {p.symbol!r}.")
    unknown = sorted(name for name in p.seed if name != "symbol" and name not in declared)
    if unknown:
        refusals.add("SEED_UNKNOWN_PARAMETER", "seed", f"The starting point sets undeclared parameters: {', '.join(unknown)}.")
    values: dict[str, Decimal] = {}
    for name, knob in declared.items():
        raw = p.seed.get(name, knob.default_value)
        try:
            value = to_decimal(raw)
        except ValueError:
            refusals.add("SEED_INVALID", f"seed.{name}", f"{knob.label}: {raw!r} is not a finite number.")
            continue
        values[name] = value
        if not knob.domain_low <= value <= knob.domain_high:
            refusals.add(
                "SEED_OUTSIDE_DOMAIN",
                f"seed.{name}",
                f"{knob.label}: the starting value {value} is outside {knob.domain_low}–{knob.domain_high} {knob.unit}.",
            )
        elif not is_quantized(knob, value):
            refusals.add("SEED_NOT_QUANTIZED", f"seed.{name}", f"{knob.label}: the starting value {value} is not a multiple of {knob.quantum}.")
    if len(values) == len(declared):
        message = violates(declaration, values)
        if message is not None:
            refusals.add("SEED_VIOLATES_CONSTRAINT", "seed", f"The starting point breaks a rule: {message}")
    for plan in p.knobs:
        if plan.mode != "fixed" or plan.name not in values or not _finite(plan.fixed_value):
            continue
        fixed = to_decimal(plan.fixed_value)
        if fixed != values[plan.name]:
            refusals.add(
                "SEED_CONFLICTS_FIXED",
                f"seed.{plan.name}",
                f"{declared[plan.name].label} is held fixed at {fixed} but the starting point uses {values[plan.name]}; "
                "a fixed knob must equal the starting point.",
            )


def _validate_incumbent(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    source = p.incumbent.source
    if source not in get_args(IncumbentSource):
        refusals.add("INCUMBENT_INVALID", "incumbent.source", f"The incumbent must come from the registry or a qualification, not {source!r}.")
    elif (source == "qualification") != bool(p.incumbent.qualification_id):
        refusals.add(
            "INCUMBENT_INVALID",
            "incumbent.qualification_id",
            "A qualification incumbent names its qualification id; a registry incumbent names none.",
        )
    if p.incumbent.params.get("symbol") != p.symbol:
        refusals.add("INCUMBENT_INVALID", "incumbent.params", f"The incumbent's parameters are not for {p.symbol!r}.")


def _validate_zoom(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    for name, value, (low, high) in (
        ("points", p.zoom.points, ZOOM_POINTS_RANGE),
        ("refinements", p.zoom.refinements, ZOOM_REFINEMENTS_RANGE),
        ("passes", p.zoom.passes, ZOOM_PASSES_RANGE),
    ):
        if not low <= value <= high:
            refusals.add("ZOOM_SETTINGS_INVALID", f"zoom.{name}", f"Zoom {name} must be between {low} and {high}, not {value}.")


def _is_et_midnight(ms: int) -> bool:
    return et_midnight_ms(et_date_at_ms(ms)) == ms


def _validate_intervals(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    bounds = {
        "development_start_ms": p.development_start_ms,
        "development_end_ms": p.development_end_ms,
        "final_start_ms": p.final_start_ms,
        "final_end_ms": p.final_end_ms,
    }
    for name, ms in bounds.items():
        if not 0 <= ms <= MAX_TIMESTAMP_MS:
            refusals.add("INTERVALS_INVALID", name, f"{name} is outside the admissible instant range.")
            return
    try:
        require_schedulable_end(p.final_end_ms)
    except ValueError as exc:
        refusals.add("INTERVALS_INVALID", "final_end_ms", str(exc))
        return
    for name, ms in bounds.items():
        if not _is_et_midnight(ms):
            refusals.add("INTERVAL_NOT_ET_MIDNIGHT", name, f"{name} must fall on an ET midnight.")
    if not p.development_start_ms < p.development_end_ms:
        refusals.add("INTERVALS_INVALID", "development_end_ms", "The development interval must end after it starts.")
    if not p.final_start_ms < p.final_end_ms:
        refusals.add("INTERVALS_INVALID", "final_end_ms", "The final test interval must end after it starts.")
    if p.final_start_ms != p.development_end_ms:
        refusals.add("FINAL_NOT_ADJACENT", "final_start_ms", "The final test must start exactly where the development interval ends.")
    if p.development_start_ms < p.development_end_ms:
        try:
            development_folds(p)
        except FoldPlanError as exc:
            refusals.add("FOLDS_INVALID", "development_end_ms", str(exc))
        except ValueError:
            # The planner's month arithmetic left the calendar (a date past year 9999).
            refusals.add("FOLDS_INVALID", "training_months", "The training and test lengths reach past the calendar; shorten them.")


def _validate_trade_floors(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    rate = p.expected_trades_per_year
    if rate is not None:
        if isinstance(rate, bool) or not isinstance(rate, int) or rate < 1:
            refusals.add("POLICY_INVALID", "expected_trades_per_year", "Expected trade frequency must be a positive whole number of trades per year.")
        if p.policy.min_trades is not None or p.exam_min_trades is not None:
            refusals.add(
                "POLICY_INVALID",
                "expected_trades_per_year",
                "A plan with an expected trade frequency takes each window's minimum from the calendar; it has no fixed trade floors.",
            )
        # Each year's floor divides by that whole year's sessions, which the calendar cannot count for its last, partial year.
        if et_date_at_ms(p.final_end_ms - 1).year >= LAST_SCHEDULABLE_DATE.year:
            refusals.add(
                "INTERVALS_INVALID",
                "final_end_ms",
                f"An expected trade frequency needs every year's full calendar; end the plan before {LAST_SCHEDULABLE_DATE.year}.",
            )
        return
    if p.policy.min_trades is None or p.policy.min_trades < 1:
        refusals.add("POLICY_INVALID", "policy.min_trades", "The minimum trade count must be at least 1.")
    if p.exam_min_trades is None or p.exam_min_trades < 1:
        refusals.add("POLICY_INVALID", "exam_min_trades", "The final test's minimum trade count must be at least 1.")


def _validate_policy(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    _validate_trade_floors(p, refusals)
    if p.policy.objective not in RANKING_MEASURES:
        refusals.add(
            "OBJECTIVE_UNKNOWN",
            "policy.objective",
            f"Rank by one of {', '.join(RANKING_MEASURES)}, not {p.policy.objective!r}.",
        )
    ceiling = p.policy.max_drawdown_ceiling
    if not (_finite(ceiling) and 0 < ceiling <= 1):
        refusals.add(
            "POLICY_INVALID",
            "policy.max_drawdown_ceiling",
            "The drawdown ceiling must be above 0% and at most 100% of peak equity.",
        )
    if not 1 <= p.budget_cap <= MAX_BUDGET_CAP:
        refusals.add("BUDGET_CAP_INVALID", "budget_cap", f"The evaluation budget must be between 1 and {MAX_BUDGET_CAP}.")


def _validate_audits(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    searched = {plan.name for plan in p.knobs if plan.mode == "search"}
    seen: set[frozenset[str]] = set()
    for a, b in p.pair_audits:
        pair = frozenset((a, b))
        if a == b or a not in searched or b not in searched:
            refusals.add("PAIR_AUDIT_INVALID", "pair_audits", f"A pair audit needs two different searched knobs; {a} and {b} are not.")
        elif pair in seen:
            refusals.add("PAIR_AUDIT_INVALID", "pair_audits", f"The pair {a} and {b} is audited twice.")
        seen.add(pair)


def _validate_execution(p: GoldenSearchProtocol, refusals: _Refusals) -> None:
    e = p.execution
    if e.fill_mode not in _FILL_MODES:
        refusals.add("EXECUTION_INVALID", "execution.fill_mode", f"The fill mode must be one of {', '.join(_FILL_MODES)}.")
    costs_ok = _finite(e.commission_per_order, e.slippage_per_share) and e.commission_per_order >= 0 and e.slippage_per_share >= 0
    if not costs_ok:
        refusals.add("EXECUTION_INVALID", "execution", "Commission and slippage must be finite and not negative.")
    if not (_finite(e.initial_cash) and e.initial_cash > 0):
        refusals.add("EXECUTION_INVALID", "execution.initial_cash", "Starting capital must be a positive amount.")
    keys = [scenario.key for scenario in p.stress]
    if len(set(keys)) != len(keys):
        refusals.add("STRESS_INVALID", "stress", "Each stress scenario needs its own key.")
    if BASE_SCENARIO in keys:
        refusals.add("STRESS_INVALID", "stress", f"The key {BASE_SCENARIO!r} names the unstressed run; give the stress scenario another key.")
    for scenario in p.stress:
        adds = (scenario.slippage_add, scenario.commission_add)
        adds_ok = _finite(*adds) and min(adds) >= 0
        if not adds_ok or (scenario.fill_mode is not None and scenario.fill_mode not in _FILL_MODES):
            refusals.add(
                "STRESS_INVALID",
                f"stress.{scenario.key}",
                f"The stress scenario {scenario.label!r} must add non-negative costs and use a known fill mode.",
            )
