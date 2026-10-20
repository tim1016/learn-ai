"""The per-stage evaluation bound, the serial time estimate and the workload refusal."""

from __future__ import annotations

import dataclasses

from app.research.golden_search.budget import estimate, procedure_bound, review_protocol
from app.research.golden_search.protocol import StressScenario, ZoomSettings
from app.research.golden_search.zoom import max_evaluations
from tests._helpers.golden_search import declaration, knob, plans_for, protocol


def test_estimate_pins_each_stage_bound_and_their_sum() -> None:
    decl = declaration()
    plan = protocol(decl, pair_audits=(("x", "y"),))

    result = estimate(plan, folds=3)

    # Zoom bound with 2 searched knobs, 5 points, 2 refinements, 2 passes: 1 + 2·2·3·6 = 73.
    assert max_evaluations(plan) == 73
    assert [(stage.stage, stage.max_evaluations) for stage in result.stages] == [
        # The all-period procedure, then one 5x5 pair audit around its winner.
        ("search", 73 + 25),
        ("recent", 73),
        # Each fold: its procedure, its winner's test run and the incumbent's benchmark run.
        ("validation", 3 * (73 + 2)),
        # 3 detail runs + 2 candidates · 2 neighbors · 2 knobs + 3 candidates · 2 stress.
        ("evidence", 3 + 8 + 6),
        # The incumbent and both fits.
        ("exam", 3),
        ("proof", 3),
    ]
    assert result.total_max == sum(stage.max_evaluations for stage in result.stages) == 419
    assert result.reserved_for_exam_and_proof == 6


def test_estimate_serial_seconds_use_each_stage_window_length() -> None:
    plan = protocol(declaration())

    result = estimate(plan, folds=3)

    # Grid Search's 1.4 s fixed + 0.3 s per month read (at least one month), per window, 30.4 days a month:
    #   search      73 x (1.4 + 0.3 x 366/30.4)              =  365.8644736842
    #   recent      73 x (1.4 + 0.3 x 184/30.4)              =  234.7526315789
    #   validation  3 x (73 x (1.4 + 0.3 x 6) + 2 x (1.4 + 0.3 x 2)) =  712.8
    #   evidence    17 x (1.4 + 0.3 x 366/30.4)              =   85.2013157895
    #   exam+proof  6 x (1.4 + 0.3 x 90/30.4)                =   13.7289473684
    #   total                                                = 1412.3473684211; 60% = 847.4084210526
    assert result.serial_seconds_high == 1412.3
    assert result.serial_seconds_low == 847.4


def test_estimate_omits_the_recent_stage_and_audits_that_are_switched_off() -> None:
    plan = protocol(declaration(), recent_window=False, neighbor_audit=False, stress=())

    result = estimate(plan, folds=2)

    assert [stage.stage for stage in result.stages] == ["search", "validation", "evidence", "exam", "proof"]
    assert result.stages[2].max_evaluations == 3
    # Without a recent fit the final test runs the incumbent and the all-period fit.
    assert result.stages[3].max_evaluations == 2 and result.reserved_for_exam_and_proof == 5


def test_procedure_bound_for_grid_is_the_grid_product() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"))
    plan = protocol(decl, method="grid", knobs=plans_for(decl, step=1.0))

    assert procedure_bound(plan) == 121
    assert estimate(plan, folds=3).stages[0].max_evaluations == 121


def test_review_protocol_sizes_a_plan_and_refuses_one_it_cannot_run() -> None:
    heavy_decl = declaration(*(knob(name, high="40") for name in "abcdef"))
    heavy_plan = protocol(heavy_decl, zoom=ZoomSettings(points=9, refinements=4, passes=5))

    over_cap = review_protocol(heavy_plan, heavy_decl)

    assert over_cap.estimate is not None, "a plan whose bound exceeds the cap is still sized"
    assert over_cap.estimate.total_max > heavy_plan.budget_cap, "the bound exceeds the cap"
    assert [refusal.code for refusal in over_cap.refusals] == ["WORKLOAD_LIMIT"], "the only refusal is the workload limit"
    assert not over_cap.lockable, "a plan over the cap is not lockable"
    assert [fold.fold_index for fold in over_cap.folds] == [0, 1, 2], "the refused plan still lists its folds"
    assert over_cap.as_dict()["refusals"][0]["code"] == "WORKLOAD_LIMIT", "the workload limit is on the wire form"

    lockable_decl = declaration()
    lockable_plan = protocol(lockable_decl, stress=(StressScenario("slip", "Extra slippage", slippage_add=0.02),))

    lockable = review_protocol(lockable_plan, lockable_decl)

    assert lockable.lockable, "a plan within the cap is lockable"
    assert lockable.estimate == estimate(lockable_plan, folds=3), "a lockable plan is sized by the estimate"

    unfolded_decl = declaration()
    unfolded_plan = dataclasses.replace(protocol(unfolded_decl), training_months=11)

    unfolded = review_protocol(unfolded_plan, unfolded_decl)

    assert "FOLDS_INVALID" in {refusal.code for refusal in unfolded.refusals}, "refused folds are named"
    assert unfolded.folds == (), "refused folds list no fold"
    assert unfolded.estimate is None, "sizing is skipped when the folds are refused"
