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
        ("search", 73),
        ("recent", 73),
        ("validation", 3 * (73 + 1)),
        # 3 detail runs + 2 candidates · 2 neighbors · 2 knobs + one 5x5 pair grid + 3 candidates · 2 stress.
        ("evidence", 3 + 8 + 25 + 6),
        ("exam", 2),
        ("proof", 3),
    ]
    assert result.total_max == sum(stage.max_evaluations for stage in result.stages) == 415
    assert result.reserved_for_exam_and_proof == 5


def test_estimate_serial_seconds_use_each_stage_window_length() -> None:
    plan = protocol(declaration())

    result = estimate(plan, folds=3)

    # Grid Search's 1.4 s fixed + 0.3 s per month read (at least one month), per window, 30.4 days a month:
    #   search      73 x (1.4 + 0.3 x 366/30.4)              =  365.8644736842
    #   recent      73 x (1.4 + 0.3 x 184/30.4)              =  234.7526315789
    #   validation  3 x (73 x (1.4 + 0.3 x 6) + 1.4 + 0.3 x 2) =  706.8
    #   evidence    17 x (1.4 + 0.3 x 366/30.4)              =   85.2013157895
    #   exam+proof  5 x (1.4 + 0.3 x 90/30.4)                =   11.4407894737
    #   total                                                = 1404.0592105263; 60% = 842.4355263158
    assert result.serial_seconds_high == 1404.1
    assert result.serial_seconds_low == 842.4


def test_estimate_omits_the_recent_stage_and_audits_that_are_switched_off() -> None:
    plan = protocol(declaration(), recent_window=False, neighbor_audit=False, stress=())

    result = estimate(plan, folds=2)

    assert [stage.stage for stage in result.stages] == ["search", "validation", "evidence", "exam", "proof"]
    assert result.stages[2].max_evaluations == 3


def test_procedure_bound_for_grid_is_the_grid_product() -> None:
    decl = declaration(knob("x", high="10"), knob("y", high="10"))
    plan = protocol(decl, method="grid", knobs=plans_for(decl, grid_step=1.0))

    assert procedure_bound(plan) == 121
    assert estimate(plan, folds=3).stages[0].max_evaluations == 121


def test_review_protocol_refuses_a_plan_whose_bound_exceeds_the_cap() -> None:
    decl = declaration(*(knob(name, high="40") for name in "abcdef"))
    plan = protocol(decl, zoom=ZoomSettings(points=9, refinements=4, passes=5))

    review = review_protocol(plan, decl)

    assert review.estimate is not None
    assert review.estimate.total_max > plan.budget_cap
    assert [refusal.code for refusal in review.refusals] == ["WORKLOAD_LIMIT"]
    assert not review.lockable
    assert [fold.fold_index for fold in review.folds] == [0, 1, 2]
    assert review.as_dict()["refusals"][0]["code"] == "WORKLOAD_LIMIT"


def test_review_protocol_sizes_a_lockable_plan() -> None:
    decl = declaration()
    plan = protocol(decl, stress=(StressScenario("slip", "Extra slippage", slippage_add=0.02),))

    review = review_protocol(plan, decl)

    assert review.lockable
    assert review.estimate == estimate(plan, folds=3)


def test_review_protocol_skips_sizing_when_the_folds_are_refused() -> None:
    decl = declaration()
    plan = dataclasses.replace(protocol(decl), training_months=11)

    review = review_protocol(plan, decl)

    assert "FOLDS_INVALID" in {refusal.code for refusal in review.refusals}
    assert review.folds == ()
    assert review.estimate is None
