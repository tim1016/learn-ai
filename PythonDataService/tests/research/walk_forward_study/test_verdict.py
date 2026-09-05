"""The frozen verdict (PRD #1925), including both counterexamples that shaped it."""

from __future__ import annotations

import pytest

from app.research.walk_forward_study.verdict import FoldEvidence, compute_verdict


def _fold(i: int, train: float | None, test: float | None, *, trades: int = 10, status: str = "completed") -> FoldEvidence:
    return FoldEvidence(fold_index=i, status=status, train_sharpe=train, test_sharpe=test, test_trades=trades)  # type: ignore[arg-type]


def _study(train: list[float | None], test: list[float | None], **kwargs):
    return [_fold(i, tr, te, **kwargs) for i, (tr, te) in enumerate(zip(train, test, strict=True))]


def test_a_healthy_study_still_worked_and_discloses_coverage() -> None:
    verdict = compute_verdict(_study([1, 1, 1, 1], [0.8, 0.7, 0.9, 0.6]), min_trades=5)

    assert verdict.label == "still worked"
    assert verdict.study_retention == pytest.approx(0.75, abs=1e-12, rel=0)
    assert verdict.based_on == "based on 4 of 4 folds"


def test_survivorship_counterexample_is_could_not_be_judged_not_still_worked() -> None:
    """Ten folds, nine with negative training Sharpe: one survivor must not carry the study."""
    train = [1.0] + [-1.0] * 9
    test = [1.0] + [-0.1] * 9

    verdict = compute_verdict(_study(train, test), min_trades=5)

    assert verdict.label == "could not be judged"
    assert verdict.defined_folds == 1 and verdict.successful_folds == 10
    assert "fewer than half" in verdict.reason


def test_near_zero_denominator_counterexample_is_got_worse_not_still_worked() -> None:
    train = [0.0001, 2, 2, 2, 2, 2]
    test = [0.001, 0.1, 0.1, 0.1, 0.1, 0.1]

    verdict = compute_verdict(_study(train, test), min_trades=5)

    assert verdict.label == "got worse"
    assert verdict.study_retention == pytest.approx(0.05, abs=1e-12, rel=0)


def test_a_failed_fold_outranks_strong_retention() -> None:
    folds = _study([1, 1, 1], [1, 1, 1])
    folds[1] = _fold(1, None, None, status="failed")

    verdict = compute_verdict(folds, min_trades=5)

    assert verdict.label == "could not be judged"
    assert "1 of 3 folds failed" in verdict.reason


def test_the_trade_floor_outranks_strong_retention_and_counts_winner_cells_only() -> None:
    verdict = compute_verdict(_study([1, 1], [1, 1], trades=2), min_trades=5)

    assert verdict.label == "too few trades"
    assert verdict.oos_trade_count == 4


def test_a_non_positive_median_test_sharpe_outranks_the_threshold_comparison() -> None:
    verdict = compute_verdict(_study([1, 1, 1], [-0.2, 0.1, -0.3]), min_trades=1)

    assert verdict.label == "stopped working"


@pytest.mark.parametrize(("retention", "label"), [(0.5, "still worked"), (0.49, "got worse")])
def test_both_sides_of_the_threshold_including_exactly_half(retention: float, label: str) -> None:
    verdict = compute_verdict(_study([1, 1], [retention, retention]), min_trades=1)

    assert verdict.label == label


def test_no_defined_test_sharpe_after_coverage_is_could_not_be_judged() -> None:
    # Coverage passes on retention? Retention needs test Sharpe too, so build the
    # gap the rule closes: defined retentions exist but a later filter is empty
    # cannot happen; rule 5 is reached when every defined test Sharpe is None,
    # which implies D == 0 first. Pin the ordering by asserting rule 2 fires.
    verdict = compute_verdict(_study([1, 1], [None, None]), min_trades=1)

    assert verdict.label == "could not be judged"
    assert verdict.defined_folds == 0


def test_no_folds_is_could_not_be_judged() -> None:
    assert compute_verdict([], min_trades=1).label == "could not be judged"


def test_rates_not_totals_a_longer_training_window_is_not_flattered() -> None:
    """A 6-month training window with cumulative profit 6 and a 3-month test with profit 2 would
    read as 'kept a third' on totals; on Sharpe the same fold retains 0.8."""
    verdict = compute_verdict(_study([1.0], [0.8]), min_trades=1)

    assert verdict.label == "still worked"
    assert verdict.study_retention == pytest.approx(0.8, abs=1e-12, rel=0)
