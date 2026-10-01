"""The frozen plan: every refusal code, and a hash that only values can change."""

from __future__ import annotations

import dataclasses
import json
import random
from collections.abc import Callable
from datetime import date

import pytest

from app.research.golden_search.declarations import KnobConstraint
from app.research.golden_search.protocol import (
    ExecutionAssumptions,
    GoldenSearchProtocol,
    IncumbentRef,
    KnobPlan,
    SelectionPolicy,
    StressScenario,
    ZoomSettings,
    development_folds,
    recent_window_ms,
    validate_protocol,
)
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import declaration, knob, protocol, synthetic_point

DECL = declaration(
    knob("x", high="10", default="1"),
    knob("y", high="10", default="5"),
    knob("g", kind="decimal", high="2", quantum="0.01", default="0.2"),
    knob("z", kind="decimal", high="100", quantum="0.5", default="0", searchable=False),
    constraints=(KnobConstraint("x", "<", "y", "x must be below y"),),
)
SEED = synthetic_point({"x": 1, "y": 5, "g": 0.2, "z": 0.0})
HOUR_MS = 3_600_000


def _valid() -> GoldenSearchProtocol:
    return protocol(DECL, seed=SEED)


def _with_knob(p: GoldenSearchProtocol, name: str, **changes: object) -> GoldenSearchProtocol:
    return dataclasses.replace(p, knobs=tuple(dataclasses.replace(plan, **changes) if plan.name == name else plan for plan in p.knobs))


def _grid(p: GoldenSearchProtocol, steps: dict[str, float | None]) -> GoldenSearchProtocol:
    knobs = tuple(dataclasses.replace(plan, step=steps.get(plan.name)) for plan in p.knobs)
    return dataclasses.replace(p, method="grid", knobs=knobs)


def _seed(**changes: object) -> Callable[[GoldenSearchProtocol], GoldenSearchProtocol]:
    return lambda p: dataclasses.replace(p, seed={**SEED, **changes})


def _day(year: int, month: int, day: int) -> int:
    return et_midnight_ms(date(year, month, day))


_MUTATIONS: list[tuple[str, Callable[[GoldenSearchProtocol], GoldenSearchProtocol]]] = [
    ("STRATEGY_MISMATCH", lambda p: dataclasses.replace(p, strategy_key="another_program")),
    ("SYMBOL_INVALID", lambda p: dataclasses.replace(p, symbol="spy/../x")),
    ("METHOD_INVALID", lambda p: dataclasses.replace(p, method="annealing")),
    ("DUPLICATE_KNOB", lambda p: dataclasses.replace(p, knobs=(*p.knobs, p.knobs[0]))),
    ("UNKNOWN_KNOB", lambda p: dataclasses.replace(p, knobs=(*p.knobs, KnobPlan("w", "search", 0.0, 1.0, 0.0)))),
    ("MISSING_KNOB", lambda p: dataclasses.replace(p, knobs=tuple(plan for plan in p.knobs if plan.name != "g"))),
    (
        "NO_SEARCHABLE_KNOB",
        lambda p: dataclasses.replace(
            p, knobs=tuple(dataclasses.replace(plan, mode="fixed", fixed_value=float(SEED[plan.name])) for plan in p.knobs)
        ),
    ),
    ("KNOB_MODE_INVALID", lambda p: _with_knob(p, "x", mode="maybe")),
    ("FIXED_OUTSIDE_DOMAIN", lambda p: _with_knob(p, "z", fixed_value=100.5)),
    ("FIXED_NOT_QUANTIZED", lambda p: _with_knob(p, "z", fixed_value=0.3)),
    ("RANGE_OUTSIDE_DOMAIN", lambda p: _with_knob(p, "x", high=11.0)),
    ("RANGE_EMPTY", lambda p: _with_knob(p, "x", low=5.0, high=5.0)),
    ("RANGE_NOT_QUANTIZED", lambda p: _with_knob(p, "g", low=0.005)),
    ("STEP_MISSING", lambda p: _with_knob(p, "x", step=None)),
    ("STEP_MISSING", lambda p: _grid(p, {"x": 1.0, "y": 1.0})),
    ("STEP_INVALID", lambda p: _with_knob(p, "g", step=0.015)),
    ("STEP_INVALID", lambda p: _grid(p, {"x": 1.0, "y": -1.0, "g": 0.01})),
    ("GRID_TOO_LARGE", lambda p: _grid(p, {"x": 1.0, "y": 1.0, "g": 0.01})),
    ("SEED_SYMBOL_MISMATCH", _seed(symbol="QQQ")),
    ("SEED_UNKNOWN_PARAMETER", _seed(w=1)),
    ("SEED_INVALID", _seed(x="five")),
    ("SEED_OUTSIDE_DOMAIN", _seed(x=11)),
    ("SEED_NOT_QUANTIZED", _seed(g=0.205)),
    ("SEED_VIOLATES_CONSTRAINT", _seed(x=6, y=5)),
    ("SEED_CONFLICTS_FIXED", _seed(z=1.0)),
    ("INCUMBENT_INVALID", lambda p: dataclasses.replace(p, incumbent=IncumbentRef("qualification", None, dict(SEED)))),
    ("INCUMBENT_INVALID", lambda p: dataclasses.replace(p, incumbent=IncumbentRef("registry", "q-1", dict(SEED)))),
    ("INCUMBENT_INVALID", lambda p: dataclasses.replace(p, incumbent=IncumbentRef("registry", None, {**SEED, "symbol": "QQQ"}))),
    ("ZOOM_SETTINGS_INVALID", lambda p: dataclasses.replace(p, zoom=ZoomSettings(points=2))),
    ("ZOOM_SETTINGS_INVALID", lambda p: dataclasses.replace(p, zoom=ZoomSettings(refinements=5))),
    ("ZOOM_SETTINGS_INVALID", lambda p: dataclasses.replace(p, zoom=ZoomSettings(passes=0))),
    ("INTERVALS_INVALID", lambda p: dataclasses.replace(p, development_start_ms=_day(2025, 2, 1))),
    ("INTERVALS_INVALID", lambda p: dataclasses.replace(p, final_end_ms=_day(2024, 12, 1))),
    ("INTERVALS_INVALID", lambda p: dataclasses.replace(p, final_end_ms=-1)),
    ("INTERVALS_INVALID", lambda p: dataclasses.replace(p, final_end_ms=et_midnight_ms(date(2300, 1, 1)))),
    ("INTERVAL_NOT_ET_MIDNIGHT", lambda p: dataclasses.replace(p, development_start_ms=p.development_start_ms + HOUR_MS)),
    ("FINAL_NOT_ADJACENT", lambda p: dataclasses.replace(p, final_start_ms=_day(2025, 1, 2))),
    (
        "FOLDS_INVALID",
        lambda p: dataclasses.replace(p, development_end_ms=_day(2024, 12, 1), final_start_ms=_day(2024, 12, 1)),
    ),
    ("OBJECTIVE_UNKNOWN", lambda p: dataclasses.replace(p, policy=SelectionPolicy(objective="sortino"))),  # type: ignore[arg-type]
    ("POLICY_INVALID", lambda p: dataclasses.replace(p, policy=SelectionPolicy(min_trades=0))),
    ("POLICY_INVALID", lambda p: dataclasses.replace(p, policy=SelectionPolicy(max_drawdown_ceiling=0.0))),
    ("POLICY_INVALID", lambda p: dataclasses.replace(p, policy=SelectionPolicy(max_drawdown_ceiling=1.5))),
    ("POLICY_INVALID", lambda p: dataclasses.replace(p, exam_min_trades=0)),
    ("BUDGET_CAP_INVALID", lambda p: dataclasses.replace(p, budget_cap=0)),
    ("BUDGET_CAP_INVALID", lambda p: dataclasses.replace(p, budget_cap=5001)),
    ("PAIR_AUDIT_INVALID", lambda p: dataclasses.replace(p, pair_audits=(("x", "x"),))),
    ("PAIR_AUDIT_INVALID", lambda p: dataclasses.replace(p, pair_audits=(("x", "z"),))),
    ("PAIR_AUDIT_INVALID", lambda p: dataclasses.replace(p, pair_audits=(("x", "y"), ("y", "x")))),
    ("EXECUTION_INVALID", lambda p: dataclasses.replace(p, execution=ExecutionAssumptions(fill_mode="magic"))),
    ("EXECUTION_INVALID", lambda p: dataclasses.replace(p, execution=ExecutionAssumptions(commission_per_order=-1.0))),
    ("EXECUTION_INVALID", lambda p: dataclasses.replace(p, execution=ExecutionAssumptions(initial_cash=0.0))),
    ("STRESS_INVALID", lambda p: dataclasses.replace(p, stress=(StressScenario("a", "A"), StressScenario("a", "B")))),
    ("STRESS_INVALID", lambda p: dataclasses.replace(p, stress=(StressScenario("a", "A", slippage_add=-0.01),))),
    ("STRESS_INVALID", lambda p: dataclasses.replace(p, stress=(StressScenario("a", "A", fill_mode="magic"),))),
]


def test_validate_protocol_accepts_a_complete_plan() -> None:
    assert validate_protocol(_valid(), DECL) == []


@pytest.mark.parametrize(("code", "mutate"), _MUTATIONS, ids=[f"{code}-{i}" for i, (code, _) in enumerate(_MUTATIONS)])
def test_validate_protocol_refuses_with_its_code(code: str, mutate: Callable[[GoldenSearchProtocol], GoldenSearchProtocol]) -> None:
    refusals = validate_protocol(mutate(_valid()), DECL)

    assert code in {refusal.code for refusal in refusals}
    assert all(refusal.message for refusal in refusals)


def test_validate_protocol_returns_every_refusal_not_only_the_first() -> None:
    plan = dataclasses.replace(_valid(), budget_cap=0, zoom=ZoomSettings(points=2), seed={**SEED, "x": 11})

    codes = [refusal.code for refusal in validate_protocol(plan, DECL)]

    assert {"BUDGET_CAP_INVALID", "ZOOM_SETTINGS_INVALID", "SEED_OUTSIDE_DOMAIN"} <= set(codes)


def test_validate_protocol_allows_a_seed_outside_the_search_range() -> None:
    plan = _with_knob(_valid(), "x", low=2.0, high=4.0)

    assert validate_protocol(plan, DECL) == []


def test_validate_protocol_accepts_a_grid_within_the_budget() -> None:
    plan = _grid(_valid(), {"x": 1.0, "y": 1.0, "g": 0.5})

    # 11 x 11 x 5 = 605 combinations.
    assert validate_protocol(plan, DECL) == []


def test_validate_protocol_seed_omitting_a_default_stands_at_the_declared_default() -> None:
    seed = {key: value for key, value in SEED.items() if key != "y"}
    plan = dataclasses.replace(_valid(), seed=seed)

    assert validate_protocol(plan, DECL) == []


def _shuffled(value: object, rng: random.Random) -> object:
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffled(item, rng) for key, item in items}
    if isinstance(value, list):
        return [_shuffled(item, rng) for item in value]
    return value


def test_protocol_hash_ignores_key_order_and_survives_a_round_trip() -> None:
    plan = dataclasses.replace(_valid(), pair_audits=(("x", "y"),))
    rng = random.Random(2696)

    reordered = GoldenSearchProtocol.from_dict(_shuffled(json.loads(json.dumps(plan.as_dict())), rng))  # type: ignore[arg-type]

    assert reordered == plan
    assert reordered.protocol_hash() == plan.protocol_hash()


def test_protocol_hash_changes_with_any_value_including_knob_order() -> None:
    plan = _valid()

    assert dataclasses.replace(plan, budget_cap=4999).protocol_hash() != plan.protocol_hash()
    assert dataclasses.replace(plan, seed={**SEED, "g": 0.21}).protocol_hash() != plan.protocol_hash()
    assert dataclasses.replace(plan, knobs=tuple(reversed(plan.knobs))).protocol_hash() != plan.protocol_hash()


def test_from_dict_refuses_unknown_and_missing_keys() -> None:
    body = _valid().as_dict()

    with pytest.raises(ValueError, match="unknown"):
        GoldenSearchProtocol.from_dict({**body, "surprise": 1})
    with pytest.raises(ValueError, match="missing"):
        GoldenSearchProtocol.from_dict({key: value for key, value in body.items() if key != "budget_cap"})
    with pytest.raises(ValueError, match="knob"):
        GoldenSearchProtocol.from_dict({**body, "knobs": [{**body["knobs"][0], "extra": True}]})


def test_recent_window_is_the_last_training_months_of_development() -> None:
    assert recent_window_ms(_valid()) == (_day(2024, 7, 1), _day(2025, 1, 1))


def test_development_folds_plan_three_six_two_folds_over_twelve_months() -> None:
    folds = development_folds(_valid())

    assert [(fold.test_start, fold.test_end) for fold in folds] == [
        (date(2024, 7, 1), date(2024, 9, 3)),
        (date(2024, 9, 3), date(2024, 11, 1)),
        (date(2024, 11, 1), date(2025, 1, 2)),
    ]
