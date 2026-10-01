"""Deterministic coordinate Zoom, including the landscapes that defeat it (PRD #2696 "Tests that challenge the design")."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from decimal import Decimal
from itertools import pairwise
from typing import Any

import numpy as np
import pytest

from app.research.golden_search.declarations import (
    KnobConstraint,
    canonical_point,
    point_hash,
    violates,
)
from app.research.golden_search.grid_procedure import run_grid
from app.research.golden_search.protocol import KnobPlan, ZoomSettings
from app.research.golden_search.zoom import (
    BudgetExhausted,
    ProcedureResult,
    max_evaluations,
    run_zoom,
)
from tests._helpers.golden_search import (
    STRATEGY,
    Landscape,
    declaration,
    ema_declaration_in_schema,
    knob,
    metrics,
    plans_for,
    protocol,
    synthetic_point,
)


def _zoom(decl, plan, landscape, *, seed=None):  # type: ignore[no-untyped-def]
    return run_zoom(
        declaration=decl,
        protocol=plan,
        seed=seed if seed is not None else plan.seed,
        evaluate=landscape,
        canonicalize=synthetic_point,
    )


def _xy(result: ProcedureResult) -> tuple[Any, Any]:
    return result.winner["x"], result.winner["y"]


def test_run_zoom_separable_landscape_converges_to_the_peak() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"))
    plan = protocol(decl)
    landscape = Landscape(lambda p: -float((p["x"] - 3) ** 2 + (p["y"] - 7) ** 2))

    result = _zoom(decl, plan, landscape)

    assert _xy(result) == (3, 7)
    assert result.winner_hash == point_hash(STRATEGY, synthetic_point({"x": 3, "y": 7}))
    assert result.winner_metrics == metrics(0.0)
    # Every knob was refined down to its quantum and the second pass moved nothing.
    assert result.stop_reason == "quantization_limit"
    assert [r.moved for r in result.rounds if r.pass_index == 1] == [False] * len([r for r in result.rounds if r.pass_index == 1])
    assert result.edge_hits == ()


def test_run_zoom_pairwise_trap_from_the_methodology_note_keeps_the_start() -> None:
    # f(0,0)=10, f(1,0)=9, f(0,1)=9, f(1,1)=20: no single move improves, the joint move does.
    table = {(0, 0): 10.0, (1, 0): 9.0, (0, 1): 9.0, (1, 1): 20.0}
    decl = declaration(knob("x", high="1"), knob("y", high="1"))
    plan = protocol(decl)

    result = _zoom(decl, plan, Landscape(lambda p: table[(p["x"], p["y"])]))
    grid = run_grid(
        declaration=decl,
        protocol=dataclasses.replace(plan, method="grid", knobs=plans_for(decl, step=1.0)),
        seed=plan.seed,
        evaluate=Landscape(lambda p: table[(p["x"], p["y"])]),
        canonicalize=synthetic_point,
    )

    assert _xy(result) == (0, 0)
    assert result.winner_metrics is not None and result.winner_metrics.sharpe_ratio == 10.0
    assert _xy(grid) == (1, 1)
    assert result.edge_hits == ("x", "y")


def test_run_zoom_interacting_landscape_misses_the_joint_move_and_says_so() -> None:
    # Each single move from (0, 0) loses; moving both knobs together wins up to f(8, 8) = 14.
    def score(p: Mapping[str, Any]) -> float:
        return 10.0 - 0.5 * (p["x"] + p["y"]) + 1.5 * min(p["x"], p["y"])

    decl = declaration(knob("x", high="8"), knob("y", high="8"))
    plan = protocol(decl, zoom=ZoomSettings(points=5, refinements=0, passes=3))

    result = _zoom(decl, plan, Landscape(score))
    grid = run_grid(
        declaration=decl,
        protocol=dataclasses.replace(plan, method="grid", knobs=plans_for(decl, step=1.0)),
        seed=plan.seed,
        evaluate=Landscape(score),
        canonicalize=synthetic_point,
    )

    assert _xy(result) == (0, 0)
    assert _xy(grid) == (8, 8)
    assert result.stop_reason == "no_improvement"
    assert result.edge_hits == ("x", "y")
    # The stop explanation names what was tested and never claims an optimum.
    explanation = result.stop_explanation.lower()
    assert "tested moves" in explanation
    assert "optimum" not in explanation and "best possible" not in explanation


def test_run_zoom_alternate_knob_order_reaches_a_different_winner() -> None:
    table = {(0, 0): 10.0, (2, 0): 12.0, (0, 2): 11.0}
    decl = declaration()
    x_first = protocol(decl)
    y_first = dataclasses.replace(x_first, knobs=tuple(reversed(x_first.knobs)))

    def score(p: Mapping[str, Any]) -> float:
        return table.get((p["x"], p["y"]), 5.0)

    assert _xy(_zoom(decl, x_first, Landscape(score))) == (2, 0)
    assert _xy(_zoom(decl, y_first, Landscape(score))) == (0, 2)


def test_run_zoom_ties_keep_the_current_value() -> None:
    decl = declaration(knob("x", kind="decimal", high="1", quantum="0.01", default="0.5"))
    plan = protocol(decl, zoom=ZoomSettings(points=5, refinements=1, passes=2))

    result = _zoom(decl, plan, Landscape(lambda p: 1.0))

    assert result.winner["x"] == 0.5
    assert result.stop_reason == "no_improvement"
    assert all(not r.moved and r.chosen == r.current_before == 0.5 for r in result.rounds)


def test_run_zoom_ineligible_current_moves_to_the_nearest_then_smaller_tied_value() -> None:
    def score(p: Mapping[str, Any]):  # type: ignore[no-untyped-def]
        if p["x"] == 2:
            return metrics(9.0, trades=0)  # the start cannot win: no trades
        return 5.0 if p["x"] in (1, 3) else 1.0

    decl = declaration(knob("x", default="2"))
    plan = protocol(decl)

    result = _zoom(decl, plan, Landscape(score))

    first = result.rounds[0]
    assert first.results[2] == (2.0, "NO_TRADES")
    assert first.moved and first.chosen == 1.0
    assert result.winner["x"] == 1


def test_run_zoom_budget_exhausted_mid_pass_returns_the_current_point() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"))
    plan = protocol(decl)
    inner = Landscape(lambda p: -float((p["x"] - 3) ** 2 + (p["y"] - 7) ** 2))
    calls = 0

    def evaluate(points):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 3:
            raise BudgetExhausted
        return inner(points)

    result = _zoom(decl, plan, evaluate)

    # Batch 1 is the seed, batch 2 moves x from 0 to 2, batch 3 is refused.
    assert result.stop_reason == "budget"
    assert _xy(result) == (2, 0)
    assert result.winner_metrics == metrics(-50.0)
    assert len(result.rounds) == 1


def test_run_zoom_budget_exhausted_before_the_seed_returns_the_seed_without_metrics() -> None:
    decl = declaration()
    plan = protocol(decl)

    def evaluate(points):  # type: ignore[no-untyped-def]
        raise BudgetExhausted

    result = _zoom(decl, plan, evaluate)

    assert result.stop_reason == "budget"
    assert _xy(result) == (0, 0)
    assert result.winner_metrics is None
    assert result.evaluated_hashes == ()


def test_run_zoom_quantization_limit_stops_refining_and_ends_the_search() -> None:
    decl = declaration(knob("x"))
    plan = protocol(decl, zoom=ZoomSettings(points=5, refinements=4, passes=3))

    result = _zoom(decl, plan, Landscape(lambda p: -float((p["x"] - 2) ** 2)))

    # Spacing 1 over 0..4 already samples every integer: one round per pass, no refinement.
    assert [(r.pass_index, r.round_index, r.quantization_limit) for r in result.rounds] == [(0, 0, True), (1, 0, True)]
    assert result.winner["x"] == 2
    assert result.stop_reason == "quantization_limit"


def test_run_zoom_refines_down_to_the_plan_step_and_never_finer() -> None:
    gap = knob("gap", kind="decimal", high="2", quantum="0.01", default="0.2")
    decl = declaration(gap)
    plan = protocol(
        decl,
        knobs=(KnobPlan("gap", "search", 0.0, 0.6, 0.2, step=0.05),),
        zoom=ZoomSettings(points=5, refinements=4, passes=2),
    )

    result = _zoom(decl, plan, Landscape(lambda p: -abs(p["gap"] - 0.37)))

    # Spacing 0.15, then 0.075, then 0.0375 <= 0.05: the third round samples the 0.05 lattice and ends the knob.
    first_pass = [r for r in result.rounds if r.pass_index == 0]
    assert [(r.round_index, r.quantization_limit) for r in first_pass] == [(0, False), (1, False), (2, True)]
    assert first_pass[1].values == (0.15, 0.22, 0.3, 0.38, 0.45)
    assert first_pass[2].values == (0.35, 0.38, 0.4, 0.45)
    assert result.winner["gap"] == 0.38
    assert result.stop_reason == "quantization_limit"
    for r in result.rounds:
        sampled = sorted(Decimal(str(value)) for value in r.values if value != r.current_before)
        assert all(b - a >= Decimal("0.05") for a, b in pairwise(sampled)), r


def test_run_zoom_a_range_narrower_than_its_points_samples_the_step_lattice_at_once() -> None:
    decl = declaration(knob("rsi", high="100", default="50"))
    plan = protocol(decl, knobs=(KnobPlan("rsi", "search", 40.0, 55.0, 50.0, step=5.0),), zoom=ZoomSettings(points=5))

    result = _zoom(decl, plan, Landscape(lambda p: -abs(p["rsi"] - 44)))

    # Five points over 40..55 would be 3.75 apart, finer than the step of 5.
    assert result.rounds[0].values == (40.0, 45.0, 50.0, 55.0)
    assert result.rounds[0].quantization_limit
    assert result.winner["rsi"] == 45


def test_run_zoom_refuses_a_searched_knob_without_a_step() -> None:
    decl = declaration()
    plan = protocol(decl, knobs=tuple(dataclasses.replace(k, step=None) for k in plans_for(decl)))

    with pytest.raises(ValueError, match="without a step"):
        _zoom(decl, plan, Landscape(lambda p: 1.0))


def test_run_zoom_no_eligible_point_returns_the_seed() -> None:
    decl = declaration()
    plan = protocol(decl, seed=synthetic_point({"x": 1, "y": 3}))

    result = _zoom(decl, plan, Landscape(lambda p: metrics(float(p["x"]), trades=10)))

    assert result.stop_reason == "no_eligible"
    assert _xy(result) == (1, 3)
    assert result.winner_metrics == metrics(1.0, trades=10)
    assert all(code == "TOO_FEW_TRADES" for r in result.rounds for _, code in r.results)


def test_run_zoom_includes_the_current_value_in_every_round() -> None:
    gap = knob("gap", kind="decimal", high="2", quantum="0.01", default="0.2")
    decl = declaration(gap)
    plan = protocol(decl, knobs=(KnobPlan("gap", "search", 0.0, 0.6, 0.2, step=0.01),), zoom=ZoomSettings(points=5, refinements=3, passes=2))

    result = _zoom(decl, plan, Landscape(lambda p: -abs(p["gap"] - 0.37)))

    assert result.rounds[0].values == (0.0, 0.15, 0.2, 0.3, 0.45, 0.6)
    for r in result.rounds:
        assert r.current_before in [value for value, _ in r.results]


def test_run_zoom_never_sends_more_points_than_its_bound() -> None:
    rng = np.random.default_rng(seed=2696)
    decl = declaration(knob("x", high="40"), knob("y", high="40"), knob("z", kind="decimal", high="1", quantum="0.01"))
    steps = {"x": [1.0, 2.0, 5.0], "y": [1.0, 3.0], "z": [0.01, 0.05, 0.1]}
    for _ in range(25):
        table: dict[tuple[Any, ...], float] = {}

        def score(p: Mapping[str, Any], table=table) -> float:  # type: ignore[no-untyped-def]
            key = (p["x"], p["y"], p["z"])
            if key not in table:
                table[key] = float(rng.normal())
            return table[key]

        settings = ZoomSettings(
            points=int(rng.integers(3, 10)), refinements=int(rng.integers(0, 5)), passes=int(rng.integers(1, 6))
        )
        knobs = tuple(dataclasses.replace(k, step=float(rng.choice(steps[k.name]))) for k in plans_for(decl))
        plan = protocol(decl, zoom=settings, knobs=knobs)
        landscape = Landscape(score)

        _zoom(decl, plan, landscape)

        assert len(landscape.sent) <= max_evaluations(plan)


def test_run_zoom_never_evaluates_a_constraint_violating_point() -> None:
    decl = declaration(
        knob("x", high="10"),
        knob("y", high="10", default="2"),
        constraints=(KnobConstraint("x", "<", "y", "x must be below y"),),
    )
    plan = protocol(decl)
    landscape = Landscape(lambda p: float(p["x"] + p["y"]))

    result = _zoom(decl, plan, landscape)

    assert all(violates(decl, point) is None for point in landscape.sent)
    assert any(r.invalid for r in result.rounds)
    assert all(message == "x must be below y" for r in result.rounds for _, message in r.invalid)
    assert result.invalid_points > 0
    assert result.winner["x"] < result.winner["y"]


def test_run_zoom_refuses_a_seed_that_breaks_a_constraint() -> None:
    decl = declaration(constraints=(KnobConstraint("x", "<", "y", "x must be below y"),))
    plan = protocol(decl, seed=synthetic_point({"x": 3, "y": 1}))

    with pytest.raises(ValueError, match="x must be below y"):
        _zoom(decl, plan, Landscape(lambda p: 1.0))


def test_procedure_result_round_trips_through_its_dict() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"))
    plan = protocol(decl)
    result = _zoom(decl, plan, Landscape(lambda p: -float((p["x"] - 3) ** 2 + (p["y"] - 7) ** 2)))

    assert ProcedureResult.from_dict(result.as_dict()) == result


def test_run_zoom_over_the_registered_ema_knobs_sends_canonical_points_without_float_drift() -> None:
    decl = ema_declaration_in_schema()
    seed = canonical_point("ema_crossover_signal", "SPY", {})
    plan = dataclasses.replace(
        protocol(decl, seed=seed),
        strategy_key="ema_crossover_signal",
        knobs=plans_for(decl),
    )
    def score(p: Mapping[str, Any]) -> float:
        return -abs(p["gap"] - 0.33) - abs(p["rsi_min"] - 41) / 100

    landscape = Landscape(score)

    result = run_zoom(declaration=decl, protocol=plan, seed=seed, evaluate=landscape)

    for point in landscape.sent:
        assert point == canonical_point("ema_crossover_signal", "SPY", point)
        assert Decimal(str(point["gap"])) == Decimal(str(point["gap"])).quantize(Decimal("0.01"))
        assert isinstance(point["rsi_min"], float) and point["rsi_min"].is_integer()
    distinct = {point_hash("ema_crossover_signal", point) for point in landscape.sent}
    assert len(distinct) == len({tuple(sorted(point.items())) for point in landscape.sent})
    # Separable, so the local search ends on the best point it evaluated. The gap's last round samples
    # its 0.05 step lattice, so 0.33 itself is never sampled and 0.35 is the nearest allowed value.
    assert result.winner == max(landscape.sent, key=score)
    assert result.winner["gap"] == 0.35
