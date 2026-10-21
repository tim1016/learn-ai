"""Longest retained interval that fits the shared walk-forward planner.

Formula: maximize end - start between trading dates with development =
  train + k * test months, k >= 1, followed by final months;
  equal spans prefer the latest end.
Reference: app/research/walk_forward_study/folds.py and
  app/research/options_golden_search/planning.py::final_window_start.
Canonical implementation: this file.
Validated against: tests/research/options_golden_search/test_history_guidance.py.
"""
from __future__ import annotations

from datetime import timedelta

from app.lean_sidecar.trading_calendar import expected_sessions, previous_completed_session_close_ms
from app.research.options_golden_search.planning import final_window_start
from app.research.walk_forward_study.folds import add_months
from app.schemas.options_golden_search import HistoryGuidance, HistoryGuidanceRequest
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms
from app.utils.timestamps import now_ms_utc


def history_guidance(request: HistoryGuidanceRequest, *, now_ms: int | None = None) -> HistoryGuidance:
    """Suggest dates only; retained bounds do not prove that every session is held."""
    def unavailable(message: str) -> HistoryGuidance:
        return HistoryGuidance(start_ms=None, end_ms=None, final_start_ms=None, fold_count=0, explanation=message)

    bounds = request.bounds
    if bounds.start_ms is None or bounds.end_ms is None:
        return unavailable("Retained history dates are unknown. Choose custom dates and use Check plan to verify coverage.")
    completed = previous_completed_session_close_ms(now_ms if now_ms is not None else now_ms_utc())
    first = et_date_at_ms(bounds.start_ms)
    until = min(et_date_at_ms(bounds.end_ms - 1) + timedelta(days=1), et_date_at_ms(completed) + timedelta(days=1))
    best = None
    sessions = expected_sessions(first, until - timedelta(days=1)) if first < until else []
    session_days = set(sessions)
    # Examine each possible calendar anchor: month-end clamping means the
    # earliest anchor does not always produce the longest valid interval.
    for start in sessions:
        months = (until.year - start.year) * 12 + until.month - start.month
        max_folds = (months - request.train_months - request.final_months) // request.test_months
        for count in range(max_folds, 0, -1):
            final_start = add_months(start, request.train_months + count * request.test_months)
            end = add_months(final_start, request.final_months)
            if end > until or end - timedelta(days=1) not in session_days or final_window_start(end, request.final_months) != final_start:
                continue
            candidate = ((end - start).days, end, start, final_start, count)
            if best is None or candidate > best:
                best = candidate
            break
    if best is None:
        return unavailable(
            f"Retained history is too short for {request.train_months} training months, "
            f"{request.test_months} forward-test months and {request.final_months} final-test months. "
            "Reduce the window lengths or capture more history, then reload the setup."
        )
    _, end, start, final_start, fold_count = best
    return HistoryGuidance(
        start_ms=et_midnight_ms(start), end_ms=et_midnight_ms(end), final_start_ms=et_midnight_ms(final_start),
        fold_count=fold_count,
        explanation=(
            f"The longest interval within retained history that fits {request.train_months} training months, "
            f"{fold_count} complete forward windows of {request.test_months} months and "
            f"{request.final_months} final-test months. Equal spans use the latest end date. "
            "Selected dates are inclusive ET trading dates; window lengths are calendar months. "
            "Training and forward windows use the same planner as Walk Forward Study. "
            "The final test is reserved after all forward windows. "
            "Check plan verifies missing history, activity requirements and the evaluation budget."
        ),
    )
