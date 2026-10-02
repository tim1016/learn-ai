"""Expected trade frequency (ADR 0074 decision 9) against the canonical exchange calendar."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.research.golden_search.activity import activity_plan, minimum_trades, window_activity
from app.research.golden_search.budget import review_protocol
from app.research.golden_search.declarations import declaration_for
from app.research.golden_search.evaluator import StudyEvaluator
from app.research.golden_search.exam_rules import judge_exam
from app.research.golden_search.models import StudyRow
from app.research.golden_search.planning import protocol_from_request
from app.research.golden_search.procedure_history import fold_windows
from app.research.golden_search.protocol import GoldenSearchProtocol
from app.research.golden_search.selection import ineligibility
from app.research.golden_search.stages import StageContext, _validation
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search import metrics
from tests._helpers.golden_search_study import FakeEngine, frequency_plan_request, plan_request

# The canonical legacy plan's hash before expected trade frequency existed (master 54d35f6a).
LEGACY_PROTOCOL_HASH = "2a155fec7e1f37e4c6978d470de6731a2a03f2d13afe65f69a548a0c9cfdb783"
LEGACY_PARAMS = {"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0}


def _window(start: date, end: date) -> tuple[int, int]:
    return et_midnight_ms(start), et_midnight_ms(end)


@pytest.mark.parametrize(
    ("start", "end", "sessions", "minimum"),
    [
        # PRD #2811's table for a 251-session year (2026) at 50 per year.
        (date(2026, 1, 2), date(2026, 1, 13), 7, 2),
        (date(2026, 7, 1), date(2026, 9, 26), 61, 13),
        (date(2026, 1, 1), date(2026, 7, 1), 123, 25),
        (date(2026, 1, 1), date(2027, 1, 1), 251, 50),
        # A 250-session year is its own denominator, not 252.
        (date(2025, 1, 1), date(2026, 1, 1), 250, 50),
        (date(2024, 7, 1), date(2026, 7, 1), 501, 100),
    ],
)
def test_the_floor_scales_with_each_years_scheduled_sessions(start: date, end: date, sessions: int, minimum: int) -> None:
    result = window_activity(50, *_window(start, end))
    assert (result["trading_sessions"], result["minimum_trades"]) == (sessions, minimum)


def test_a_cross_year_window_sums_the_yearly_fractions_and_rounds_once() -> None:
    # Dec 30-31 of 2025 (250 sessions) and Jan 2 and 5 of 2026 (251): 50 x (2/250 + 2/251) = 0.798, so 1, not 1 + 1.
    result = window_activity(50, *_window(date(2025, 12, 30), date(2026, 1, 6)))
    assert result["years"] == [
        {"year": 2025, "selected_sessions": 2, "year_sessions": 250},
        {"year": 2026, "selected_sessions": 2, "year_sessions": 251},
    ]
    assert result["minimum_trades"] == 1


def test_holidays_are_excluded_early_closes_count_once_and_the_end_is_excluded() -> None:
    # Thanksgiving 2024 is closed; Black Friday's early close is one session; Dec 3 is the excluded end.
    result = window_activity(50, *_window(date(2024, 11, 28), date(2024, 12, 3)))
    assert result["trading_sessions"] == 2
    assert window_activity(50, *_window(date(2024, 11, 30), date(2024, 12, 2)))["trading_sessions"] == 0


@pytest.mark.parametrize("rate", [0, -1, 0.5, True])
def test_a_rate_that_is_not_a_positive_whole_number_is_refused(rate: int) -> None:
    with pytest.raises(ValueError, match="positive whole number"):
        window_activity(rate, *_window(date(2026, 1, 1), date(2026, 2, 1)))


def test_a_window_that_does_not_move_forward_is_refused() -> None:
    start, _ = _window(date(2026, 1, 1), date(2026, 2, 1))
    with pytest.raises(ValueError, match="end after it starts"):
        window_activity(50, start, start)


def test_a_plan_has_a_frequency_or_fixed_floors_never_both() -> None:
    def codes(request: dict[str, Any]) -> set[tuple[str, str]]:
        protocol = protocol_from_request(request)
        declaration = declaration_for(protocol.strategy_key)
        assert declaration is not None
        return {(item.code, item.field) for item in review_protocol(protocol, declaration).refusals}

    assert not codes(frequency_plan_request("SPY"))
    assert ("POLICY_INVALID", "expected_trades_per_year") in codes(plan_request("SPY", expected_trades_per_year=50))
    assert ("POLICY_INVALID", "expected_trades_per_year") in codes(frequency_plan_request("SPY", 0))
    legacy_without_floors = plan_request("SPY", exam_min_trades=None)
    assert ("POLICY_INVALID", "exam_min_trades") in codes(legacy_without_floors)


def test_the_receipt_freezes_the_rate_and_yearly_counts_against_later_edits_and_calendar_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    frozen = activity_plan(protocol)
    assert frozen is not None
    window = frozen["windows"][0]
    assert window["years"] == [{"year": 2025, "selected_sessions": 60, "year_sessions": 250}]
    assert window["minimum_trades"] == 12
    changed = replace(protocol, expected_trades_per_year=100)
    assert minimum_trades(changed, window["start_ms"], window["end_ms"]) == 24
    with pytest.raises(ValueError, match="does not match"):
        minimum_trades(changed, window["start_ms"], window["end_ms"], frozen=frozen)
    monkeypatch.setattr("app.research.golden_search.activity._year_sessions", lambda _year: 240)
    assert minimum_trades(protocol, window["start_ms"], window["end_ms"], frozen=frozen) == 12
    assert minimum_trades(protocol, window["start_ms"], window["end_ms"]) == 13
    with pytest.raises(ValueError, match="no activity policy"):
        minimum_trades(protocol, *_window(date(2026, 1, 1), date(2026, 2, 1)), frozen=frozen)


def test_the_forward_floor_covers_every_scheduled_fold_test() -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    frozen = activity_plan(protocol)
    assert frozen is not None
    windows = {window["key"]: window for window in frozen["windows"]}
    folds = fold_windows(protocol)
    tests = [window_activity(50, fold.test_start_ms, fold.test_end_ms) for fold in folds]
    assert windows["forward"]["trading_sessions"] == sum(test["trading_sessions"] for test in tests)
    assert {f"training_{fold.fold_index}" for fold in folds} <= windows.keys()
    assert {"development", "recent", "final"} <= windows.keys()


def test_a_legacy_plan_keeps_its_hash_and_its_two_fixed_floors() -> None:
    legacy = protocol_from_request(
        plan_request("SPY", exam_min_trades=7, seed=LEGACY_PARAMS, incumbent={"source": "registry", "qualification_id": None, "params": LEGACY_PARAMS})
    )
    assert legacy.protocol_hash() == LEGACY_PROTOCOL_HASH
    assert "expected_trades_per_year" not in legacy.as_dict()
    assert GoldenSearchProtocol.from_dict({**legacy.as_dict(), "expected_trades_per_year": None}).protocol_hash() == LEGACY_PROTOCOL_HASH
    assert minimum_trades(legacy, legacy.final_start_ms, legacy.final_end_ms, final=True) == 7
    assert minimum_trades(legacy, legacy.development_start_ms, legacy.development_end_ms) == 1
    assert activity_plan(legacy) is None


def test_an_unresolved_floor_never_reaches_selection() -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    with pytest.raises(ValueError, match="no trade floor"):
        ineligibility(metrics(1.5, trades=5), protocol.policy)


def test_stages_use_each_windows_floor_and_the_verdict_uses_all_forward_time(monkeypatch: pytest.MonkeyPatch) -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    declaration = declaration_for(protocol.strategy_key)
    assert declaration is not None
    row = cast(StudyRow, SimpleNamespace(id="frequency-study", receipt={"activity": activity_plan(protocol)}))
    ctx = StageContext(row, 0, protocol, declaration, FakeEngine(), [], lambda: None, lambda _: None, lambda *_: None, lambda _: None)

    class Evaluator:
        def evaluate(self, points: list[Any], **_kwargs: Any) -> list[Any]:
            return [metrics(1.5, trades=5) for _ in points]

    # Replace persistence only; the production Zoom, fold selection and verdict run.
    monkeypatch.setattr("app.research.golden_search.stages.with_connection", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("app.research.golden_search.stages.run_sync", lambda value: value)
    monkeypatch.setattr(StageContext, "trials", lambda *_args: None)
    monkeypatch.setattr(StageContext, "write", lambda self, patch: self.results.update(patch))
    verdict = _validation(ctx, cast(StudyEvaluator, Evaluator()))
    # Each one-month training window needs 5 trades at 50 per year, so 5 qualifies there.
    assert all(fold["status"] == "completed" for fold in ctx.results["validation"]["folds"])
    assert verdict.label == "still worked" and verdict.oos_trade_count == 10
    assert ctx.policy(ctx.development).min_trades == 12
    assert ineligibility(metrics(1.5, trades=5), ctx.policy(ctx.development)) == "TOO_FEW_TRADES"

    floor = ctx.trade_floor(ctx.final, final=True)
    assert floor == 5
    judgement = judge_exam(metrics(1.5, trades=4), metrics(1.0), policy=protocol.policy, exam_min_trades=floor, development_objective=1.5, frequency_policy=True)
    assert judgement.outcome == "not_enough_evidence"
    check = next(item for item in judgement.checks if item.code == "SAMPLE_FLOOR")
    assert check.label == "Expected trade frequency" and "does not establish statistical confidence" in check.detail


def test_a_frequency_plan_without_its_receipt_refuses_instead_of_falling_back() -> None:
    protocol = protocol_from_request(frequency_plan_request("SPY"))
    declaration = declaration_for(protocol.strategy_key)
    assert declaration is not None
    row = cast(StudyRow, SimpleNamespace(id="frequency-study", receipt={}))
    ctx = StageContext(row, 0, protocol, declaration, FakeEngine(), [], lambda: None, lambda _: None, lambda *_: None, lambda _: None)
    with pytest.raises(ValueError, match="missing its expected trade frequency"):
        ctx.trade_floor(ctx.development)
