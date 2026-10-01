"""The exhaustive Grid procedure and the audit grids around a candidate."""

from __future__ import annotations

import dataclasses

import pytest

from app.research.golden_search.declarations import KnobConstraint, canonical_point, violates
from app.research.golden_search.grid_procedure import (
    GRID_BATCH_SIZE,
    max_grid_evaluations,
    neighbor_probes,
    pair_grid,
    run_grid,
)
from app.research.golden_search.protocol import KnobPlan
from app.research.golden_search.zoom import BudgetExhausted
from tests._helpers.golden_search import (
    Landscape,
    declaration,
    ema_declaration,
    knob,
    metrics,
    plans_for,
    protocol,
    synthetic_point,
)

_BELOW = KnobConstraint("x", "<", "y", "x must be below y")


def _grid_plan(decl, **overrides):  # type: ignore[no-untyped-def]
    return protocol(decl, method="grid", knobs=plans_for(decl, step=1.0), **overrides)


def _run(decl, plan, evaluate):  # type: ignore[no-untyped-def]
    return run_grid(declaration=decl, protocol=plan, seed=plan.seed, evaluate=evaluate, canonicalize=synthetic_point)


def test_run_grid_evaluates_every_valid_combination_in_bounded_batches() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"), constraints=(_BELOW,))
    plan = _grid_plan(decl, seed=synthetic_point({"x": 0, "y": 1}))
    landscape = Landscape(lambda p: float(p["x"] * p["y"]))

    result = _run(decl, plan, landscape)

    # 11 x 11 = 121 combinations; x < y keeps the 55 strictly above the diagonal.
    assert max_grid_evaluations(plan) == 121
    assert len(landscape.sent) == 55
    assert result.invalid_points == 66
    assert all(violates(decl, point) is None for point in landscape.sent)
    assert max(len(batch) for batch in landscape.batches) == GRID_BATCH_SIZE
    assert (result.winner["x"], result.winner["y"]) == (9, 10)
    assert result.stop_reason == "no_improvement"
    assert result.rounds == ()
    assert result.edge_hits == ("y",)
    assert "every valid combination" in result.stop_explanation


def test_run_grid_holds_fixed_knobs_and_steps_decimal_axes_exactly() -> None:
    decl = declaration(knob("gap", kind="decimal", high="2", quantum="0.01"), knob("k", default="3"))
    plan = protocol(
        decl,
        method="grid",
        knobs=(KnobPlan("gap", "search", 0.15, 0.6, 0.0, step=0.15), KnobPlan("k", "fixed", 0.0, 4.0, 3.0)),
        seed=synthetic_point({"gap": 0.15, "k": 3}),
    )
    landscape = Landscape(lambda p: 1.0)

    _run(decl, plan, landscape)

    assert [point["gap"] for point in landscape.sent] == [0.15, 0.3, 0.45, 0.6]
    assert {point["k"] for point in landscape.sent} == {3}


def test_run_grid_budget_exhaustion_keeps_the_best_completed_batch() -> None:
    decl = declaration(knob("x", high="99"))
    plan = protocol(decl, method="grid", knobs=(KnobPlan("x", "search", 0.0, 99.0, 0.0, step=1.0),))
    inner = Landscape(lambda p: float(p["x"]))
    calls = 0

    def evaluate(points):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise BudgetExhausted
        return inner(points)

    result = _run(decl, plan, evaluate)

    assert result.stop_reason == "budget"
    assert result.winner["x"] == GRID_BATCH_SIZE - 1
    assert len(result.evaluated_hashes) == GRID_BATCH_SIZE


def test_run_grid_with_nothing_eligible_returns_the_seed() -> None:
    decl = declaration()
    plan = _grid_plan(decl, seed=synthetic_point({"x": 2, "y": 2}))

    result = _run(decl, plan, Landscape(lambda p: metrics(1.0, net=-5.0)))

    assert result.stop_reason == "no_eligible"
    assert (result.winner["x"], result.winner["y"]) == (2, 2)
    assert result.winner_metrics == metrics(1.0, net=-5.0)
    assert "No combination" in result.stop_explanation


_EMA_PAIR = declaration(
    knob("fast", low="2", high="30", default="5"),
    knob("slow", low="3", high="40", default="10"),
    constraints=(KnobConstraint("fast", "<", "slow", "fast must be below slow"),),
)


def _pair_plan(**overrides):  # type: ignore[no-untyped-def]
    knobs = (KnobPlan("fast", "search", 3.0, 12.0, 5.0, step=1.0), KnobPlan("slow", "search", 8.0, 30.0, 10.0, step=1.0))
    return protocol(_EMA_PAIR, knobs=knobs, seed=synthetic_point({"fast": 5, "slow": 10}), **overrides)


def test_pair_grid_maps_the_planned_ranges_through_the_center_rows_first_knob() -> None:
    grid = pair_grid(_EMA_PAIR, _pair_plan(), synthetic_point({"fast": 8, "slow": 21}), "fast", "slow", canonicalize=synthetic_point)

    # Rows: fast over 3..12 -> 3, 5.25, 7.5, 9.75, 12 -> 3, 5, 8, 10, 12 (half-even); 8 is already the center.
    assert (grid.y_knob, grid.y_values) == ("fast", (3.0, 5.0, 8.0, 10.0, 12.0))
    # Columns: slow over 8..30 -> 8, 14, 19, 24, 30; 19 is nearest the center's 21 and is replaced by it.
    assert (grid.x_knob, grid.x_values) == ("slow", (8.0, 14.0, 21.0, 24.0, 30.0))
    assert len(grid.cells) == 25
    # Row-major: every column of the first row, then the next row.
    assert [(c.values["fast"], c.values["slow"]) for c in grid.cells[:5]] == [(3.0, 8.0), (3.0, 14.0), (3.0, 21.0), (3.0, 24.0), (3.0, 30.0)]
    by_cell = {(c.values["fast"], c.values["slow"]): c for c in grid.cells}
    assert by_cell[(8.0, 21.0)].point == synthetic_point({"fast": 8, "slow": 21})
    # fast < slow fails at (8, 8), (10, 8) and (12, 8).
    assert sorted(key for key, c in by_cell.items() if c.status == "invalid") == [(8.0, 8.0), (10.0, 8.0), (12.0, 8.0)]
    assert by_cell[(10.0, 8.0)].reason == "fast must be below slow"
    assert len(grid.testable) == 22


def test_pair_grid_breaks_a_nearest_tie_toward_the_smaller_value_and_keeps_an_off_range_center() -> None:
    grid = pair_grid(_EMA_PAIR, _pair_plan(), synthetic_point({"fast": 4, "slow": 35}), "fast", "slow", canonicalize=synthetic_point)

    # 4 is as near 3 as 5: the smaller, 3, makes way. 35 lies beyond the planned 30, which it replaces.
    assert grid.y_values == (4.0, 5.0, 8.0, 10.0, 12.0)
    assert grid.x_values == (8.0, 14.0, 19.0, 24.0, 35.0)


def test_pair_grid_refuses_a_knob_the_plan_does_not_search() -> None:
    plan = _pair_plan()
    held = dataclasses.replace(plan, knobs=(plan.knobs[0], KnobPlan("slow", "fixed", 8.0, 30.0, 10.0)))

    with pytest.raises(ValueError, match="slow is not a searched knob"):
        pair_grid(_EMA_PAIR, held, synthetic_point({"fast": 5, "slow": 10}), "fast", "slow", canonicalize=synthetic_point)


def test_neighbor_probes_step_one_knob_and_flag_the_domain_edge() -> None:
    decl = declaration(knob("gap", kind="decimal", high="2", quantum="0.01", neighbor="0.05"), knob("y"))

    below, above = neighbor_probes(decl, synthetic_point({"gap": 0.0, "y": 2}), "gap", canonicalize=synthetic_point)

    assert below.status == "outside_domain"
    assert above.status == "testable"
    assert above.point == synthetic_point({"gap": 0.05, "y": 2})
    assert above.values == {"gap": 0.05}


def test_run_grid_over_the_registered_ema_knobs_sends_canonical_points() -> None:
    decl = ema_declaration()
    seed = canonical_point("ema_crossover_signal", "SPY", {})
    plan = dataclasses.replace(
        protocol(decl, seed=seed),
        strategy_key="ema_crossover_signal",
        method="grid",
        knobs=(
            KnobPlan("gap", "search", 0.0, 0.6, 0.2, step=0.15),
            KnobPlan("rsi_min", "search", 40.0, 60.0, 50.0, step=10.0),
            *(
                KnobPlan(k.name, "fixed", float(k.default_low), float(k.default_high), float(k.default_value))
                for k in decl.knobs
                if k.name not in ("gap", "rsi_min")
            ),
        ),
    )
    landscape = Landscape(lambda p: p["gap"])

    result = run_grid(declaration=decl, protocol=plan, seed=seed, evaluate=landscape)

    assert len(landscape.sent) == 15
    assert all(point == canonical_point("ema_crossover_signal", "SPY", point) for point in landscape.sent)
    assert sorted({point["gap"] for point in landscape.sent}) == [0.0, 0.15, 0.3, 0.45, 0.6]
    assert result.winner["gap"] == 0.6


def test_max_grid_evaluations_is_the_product_of_searched_axes() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="4"), knob("z", default="1", searchable=False))
    plan = dataclasses.replace(_grid_plan(decl), knobs=(
        KnobPlan("x", "search", 0.0, 10.0, 0.0, step=2.0),
        KnobPlan("y", "search", 0.0, 4.0, 0.0, step=1.0),
        KnobPlan("z", "fixed", 0.0, 4.0, 1.0),
    ))

    assert max_grid_evaluations(plan) == 6 * 5


def test_pair_grid_around_the_registry_point_uses_the_registered_canonical_form() -> None:
    decl = ema_declaration()
    center = canonical_point("ema_crossover_signal", "SPY", {})
    plan = dataclasses.replace(protocol(decl, seed=center), strategy_key="ema_crossover_signal")

    grid = pair_grid(decl, plan, center, "rsi_min", "rsi_max")

    # rsi_min over 30..60 -> 30, 38, 45, 52, 60 with 52 replaced by 50; rsi_max over 60..90 with 68 replaced by 70.
    assert grid.y_values == (30.0, 38.0, 45.0, 50.0, 60.0)
    assert grid.x_values == (60.0, 70.0, 75.0, 82.0, 90.0)
    assert [c.values for c in grid.cells if c.status == "invalid"] == [{"rsi_min": 60.0, "rsi_max": 60.0}]
    assert all(cell.point == canonical_point("ema_crossover_signal", "SPY", cell.point) for cell in grid.testable)
    centre = next(cell for cell in grid.cells if cell.values == {"rsi_min": 50.0, "rsi_max": 70.0})
    assert centre.point == center
