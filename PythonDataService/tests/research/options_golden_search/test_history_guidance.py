"""Date recommendations obey the same whole-fold contract as Walk Forward Study."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.options_golden_search.history_guidance import history_guidance
from app.research.options_golden_search.planning import final_window_start, freeze_protocol
from app.research.options_golden_search.protocol import Knob
from app.research.walk_forward_study.folds import whole_months_between
from app.schemas.options_golden_search import HistoryGuidance, HistoryGuidanceRequest
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms, et_midnight_ms
from tests.research.options_golden_search.test_protocol_search import setup


@pytest.mark.parametrize("first,last,train,test,final", [
    (date(2024, 1, 2), date(2026, 10, 17), 6, 2, 6),
    (date(2023, 1, 31), date(2026, 3, 1), 12, 3, 6),
    (date(2023, 2, 28), date(2025, 8, 31), 3, 1, 1),
    (date(2024, 1, 31), date(2025, 8, 29), 6, 1, 6),
])
def test_maximum_span_matches_shared_planner_and_frozen_options_protocol(first: date, last: date, train: int, test: int, final: int) -> None:
    request = HistoryGuidanceRequest(bounds={"start_ms": et_midnight_ms(first), "end_ms": et_midnight_ms(last)},
                                     train_months=train, test_months=test, final_months=final)
    answer = history_guidance(request, now_ms=et_midnight_ms(date(2026, 11, 1)))
    frozen = freeze_protocol(setup(), (Knob(path="legs.call_a.stop_loss.value", values=("10", "20")),), start_ms=answer.start_ms, end_ms=answer.end_ms,
                             train_months=train, test_months=test, final_months=final)
    assert len(frozen.folds) == answer.fold_count
    assert frozen.final.start_ms == answer.final_start_ms == frozen.folds[-1].forward.end_ms
    assert first <= et_date_at_ms(answer.start_ms) < et_date_at_ms(answer.end_ms) <= last
    # Enumerate every possible session end independently of the recommendation's
    # month-addition construction, then apply the canonical planner's predicates.
    best = (0, first)
    sessions = expected_sessions(first, last - timedelta(days=1))
    session_days = set(sessions)
    assert et_date_at_ms(answer.start_ms) in session_days
    assert et_date_at_ms(answer.end_ms - 1) in session_days
    for last_session in sessions:
        end = last_session + timedelta(days=1)
        final_start = final_window_start(end, final)
        if final_start is None:
            continue
        for start in sessions:
            if start >= final_start:
                break
            development = whole_months_between(start, final_start)
            if development is None or development < train + test or (development - train) % test != 0:
                continue
            best = max(best, ((end - start).days, end))
    assert ((et_date_at_ms(answer.end_ms) - et_date_at_ms(answer.start_ms)).days, et_date_at_ms(answer.end_ms)) == best


@pytest.mark.parametrize("bounds", [
    {"start_ms": None, "end_ms": None},
    {"start_ms": et_midnight_ms(date(2026, 9, 1)), "end_ms": et_midnight_ms(date(2026, 10, 1))},
    {"start_ms": MAX_TIMESTAMP_MS, "end_ms": MAX_TIMESTAMP_MS},
])
def test_unknown_or_short_history_never_fabricates_dates(bounds: dict[str, int | None]) -> None:
    answer = history_guidance(HistoryGuidanceRequest(bounds=bounds, train_months=6, test_months=2, final_months=6),
                              now_ms=et_midnight_ms(date(2026, 10, 21)))
    assert answer.start_ms is answer.end_ms is answer.final_start_ms is None
    assert answer.fold_count == 0
    assert answer.explanation


def test_retained_current_session_is_excluded_until_it_has_closed() -> None:
    request = HistoryGuidanceRequest(bounds={"start_ms": et_midnight_ms(date(2024, 1, 1)), "end_ms": et_midnight_ms(date(2027, 1, 1))},
                                     train_months=6, test_months=2, final_months=6)
    before_open = history_guidance(request, now_ms=et_midnight_ms(date(2026, 10, 20)))
    assert et_date_at_ms(before_open.end_ms) <= date(2026, 10, 20)


def test_end_bound_at_the_schema_ceiling_is_clamped_to_the_last_completed_session() -> None:
    now_ms = et_midnight_ms(date(2026, 10, 21))
    start_ms = et_midnight_ms(date(2024, 1, 2))

    def ask(end_ms: int) -> HistoryGuidance:
        request = HistoryGuidanceRequest(bounds={"start_ms": start_ms, "end_ms": end_ms}, train_months=6, test_months=2, final_months=6)
        return history_guidance(request, now_ms=now_ms)

    answer = ask(MAX_TIMESTAMP_MS)
    assert answer == ask(et_midnight_ms(date(2027, 1, 1)))
    assert answer.fold_count > 0
    assert et_date_at_ms(answer.end_ms) <= date(2026, 10, 21)
