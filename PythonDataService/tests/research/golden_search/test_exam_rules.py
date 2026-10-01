"""The final test's frozen rules: every outcome branch, and null is never a pass."""

from __future__ import annotations

import math

import numpy as np

from app.research.golden_search.exam_rules import judge_exam, retention
from app.research.golden_search.protocol import SelectionPolicy
from app.research.golden_search.selection import Metrics
from tests._helpers.golden_search import metrics

_POLICY = SelectionPolicy(objective="sharpe_ratio", min_trades=30, max_drawdown_ceiling=0.2)


def _judge(candidate: Metrics, incumbent: Metrics | None = None, development: float | None = 2.0):  # type: ignore[no-untyped-def]
    return judge_exam(
        candidate,
        metrics(1.0) if incumbent is None else incumbent,
        policy=_POLICY,
        exam_min_trades=30,
        development_objective=development,
    )


def _statuses(judgement) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {check.code: check.status for check in judgement.checks}


def test_judge_exam_meets_rules_when_every_check_passes() -> None:
    judgement = _judge(metrics(1.5, trades=30, net=10.0, drawdown=0.2))

    assert judgement.outcome == "meets_rules"
    assert _statuses(judgement) == {
        "NET_POSITIVE": "pass",
        "DRAWDOWN_WITHIN": "pass",
        "SAMPLE_FLOOR": "pass",
        "BEATS_INCUMBENT": "pass",
    }
    # Retention is descriptive: 1.5 / 2.0.
    assert np.isclose(judgement.retention, 0.75, atol=1e-9, rtol=0)


def test_judge_exam_could_not_evaluate_a_failed_run() -> None:
    judgement = _judge(Metrics.failed("engine refused the window"))

    assert judgement.outcome == "could_not_evaluate"
    assert set(_statuses(judgement).values()) == {"not_available"}
    assert "engine refused the window" in judgement.checks[0].detail
    assert judgement.retention is None


def test_judge_exam_too_few_trades_is_not_enough_evidence_even_when_it_also_lost() -> None:
    judgement = _judge(metrics(1.5, trades=29, net=-10.0))

    assert judgement.outcome == "not_enough_evidence"
    assert _statuses(judgement)["SAMPLE_FLOOR"] == "fail"
    assert _statuses(judgement)["NET_POSITIVE"] == "fail"


def test_judge_exam_undefined_objective_is_not_enough_evidence() -> None:
    judgement = _judge(metrics(math.nan, trades=40))

    assert judgement.outcome == "not_enough_evidence"
    assert _statuses(judgement)["BEATS_INCUMBENT"] == "not_available"
    assert judgement.retention is None


def test_judge_exam_any_failed_check_does_not_meet_the_rules() -> None:
    assert _judge(metrics(1.5, net=0.0)).outcome == "does_not_meet_rules"
    assert _judge(metrics(1.5, drawdown=0.25)).outcome == "does_not_meet_rules"
    losing_to_incumbent = _judge(metrics(0.9), incumbent=metrics(1.0))
    assert losing_to_incumbent.outcome == "does_not_meet_rules"
    assert _statuses(losing_to_incumbent)["BEATS_INCUMBENT"] == "fail"


def test_judge_exam_a_missing_or_failed_incumbent_is_never_a_pass() -> None:
    for incumbent in (Metrics.failed("no data"), metrics(None)):
        judgement = _judge(metrics(1.5), incumbent=incumbent)

        assert _statuses(judgement)["BEATS_INCUMBENT"] == "not_available"
        assert judgement.outcome == "not_enough_evidence"
    no_incumbent = judge_exam(metrics(1.5), None, policy=_POLICY, exam_min_trades=30, development_objective=2.0)
    assert no_incumbent.outcome == "not_enough_evidence"


def test_judge_exam_undefined_drawdown_or_net_is_not_available_not_a_pass() -> None:
    assert _statuses(_judge(metrics(1.5, drawdown=None)))["DRAWDOWN_WITHIN"] == "not_available"
    assert _judge(metrics(1.5, drawdown=None)).outcome == "not_enough_evidence"
    assert _judge(metrics(1.5, net=None)).outcome == "not_enough_evidence"


def test_retention_is_defined_only_for_a_positive_development_objective() -> None:
    assert np.isclose(retention(0.5, 2.0), 0.25, atol=1e-9, rtol=0)
    assert np.isclose(retention(-0.5, 2.0), -0.25, atol=1e-9, rtol=0)
    assert retention(1.0, 0.0) is None
    assert retention(1.0, -1.0) is None
    assert retention(None, 1.0) is None
    assert retention(1.0, math.inf) is None
