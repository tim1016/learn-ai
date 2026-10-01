"""The exhaustive Grid procedure and the audit grids around a candidate."""

from __future__ import annotations

import dataclasses

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
    ema_declaration_in_schema,
    knob,
    metrics,
    plans_for,
    protocol,
    synthetic_point,
)

_BELOW = KnobConstraint("x", "<", "y", "x must be below y")


def _grid_plan(decl, **overrides):  # type: ignore[no-untyped-def]
    return protocol(decl, method="grid", knobs=plans_for(decl, grid_step=1.0), **overrides)


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
        knobs=(KnobPlan("gap", "search", 0.15, 0.6, 0.0, grid_step=0.15), KnobPlan("k", "fixed", 0.0, 4.0, 3.0)),
        seed=synthetic_point({"gap": 0.15, "k": 3}),
    )
    landscape = Landscape(lambda p: 1.0)

    _run(decl, plan, landscape)

    assert [point["gap"] for point in landscape.sent] == [0.15, 0.3, 0.45, 0.6]
    assert {point["k"] for point in landscape.sent} == {3}


def test_run_grid_budget_exhaustion_keeps_the_best_completed_batch() -> None:
    decl = declaration(knob("x", high="99"))
    plan = protocol(decl, method="grid", knobs=(KnobPlan("x", "search", 0.0, 99.0, 0.0, grid_step=1.0),))
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


def test_pair_grid_marks_cells_outside_the_domain_and_constraint_violations() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"), constraints=(_BELOW,))

    grid = pair_grid(decl, synthetic_point({"x": 1, "y": 3}), "x", "y", canonicalize=synthetic_point)

    assert grid.x_values == (-1.0, 0.0, 1.0, 2.0, 3.0)
    assert grid.y_values == (1.0, 2.0, 3.0, 4.0, 5.0)
    assert len(grid.cells) == 25
    by_xy = {(cell.values["x"], cell.values["y"]): cell for cell in grid.cells}
    assert by_xy[(-1.0, 3.0)].status == "outside_domain"
    assert by_xy[(-1.0, 3.0)].reason == "untested: outside the legal domain"
    assert by_xy[(2.0, 2.0)].status == "invalid"
    assert by_xy[(2.0, 2.0)].reason == "x must be below y"
    center = by_xy[(1.0, 3.0)]
    assert center.status == "testable"
    assert center.point == synthetic_point({"x": 1, "y": 3})
    assert sum(cell.status == "outside_domain" for cell in grid.cells) == 5
    # x < y fails where x >= y among in-domain cells: (1,1), (2,1), (3,1), (2,2), (3,2), (3,3).
    assert sum(cell.status == "invalid" for cell in grid.cells) == 6
    assert len(grid.testable) == 14
    # Row-major: every x for the first y, then the next y.
    assert [(c.values["x"], c.values["y"]) for c in grid.cells[:5]] == [(-1.0, 1.0), (0.0, 1.0), (1.0, 1.0), (2.0, 1.0), (3.0, 1.0)]


def test_neighbor_probes_step_one_knob_and_flag_the_domain_edge() -> None:
    decl = declaration(knob("gap", kind="decimal", high="2", quantum="0.01", neighbor="0.05"), knob("y"))

    below, above = neighbor_probes(decl, synthetic_point({"gap": 0.0, "y": 2}), "gap", canonicalize=synthetic_point)

    assert below.status == "outside_domain"
    assert above.status == "testable"
    assert above.point == synthetic_point({"gap": 0.05, "y": 2})
    assert above.values == {"gap": 0.05}


def test_run_grid_over_the_registered_ema_knobs_sends_canonical_points() -> None:
    decl = ema_declaration_in_schema()
    seed = canonical_point("ema_crossover_signal", "SPY", {})
    plan = dataclasses.replace(
        protocol(decl, seed=seed),
        strategy_key="ema_crossover_signal",
        method="grid",
        knobs=(
            KnobPlan("gap", "search", 0.0, 0.6, 0.2, grid_step=0.15),
            KnobPlan("rsi_min", "search", 40.0, 60.0, 50.0, grid_step=10.0),
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
        KnobPlan("x", "search", 0.0, 10.0, 0.0, grid_step=2.0),
        KnobPlan("y", "search", 0.0, 4.0, 0.0, grid_step=1.0),
        KnobPlan("z", "fixed", 0.0, 4.0, 1.0),
    ))

    assert max_grid_evaluations(plan) == 6 * 5


def test_pair_grid_around_the_registry_point_uses_the_registered_canonical_form() -> None:
    decl = ema_declaration_in_schema()
    center = canonical_point("ema_crossover_signal", "SPY", {})

    grid = pair_grid(decl, center, "rsi_min", "rsi_max", half_width=2)

    assert grid.x_values == (46.0, 48.0, 50.0, 52.0, 54.0)
    assert grid.y_values == (66.0, 68.0, 70.0, 72.0, 74.0)
    assert all(cell.status == "testable" for cell in grid.cells)
    assert all(cell.point == canonical_point("ema_crossover_signal", "SPY", cell.point) for cell in grid.testable)
    centre = next(cell for cell in grid.cells if cell.values == {"rsi_min": 50.0, "rsi_max": 70.0})
    assert centre.point == center
