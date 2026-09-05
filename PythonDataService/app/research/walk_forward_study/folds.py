"""Fold generation for a registry-strategy walk-forward study (PRD #1925, "Method and folds").

Formula: the study range ``[start, end)`` in whole calendar months from
``start``; with training length ``T`` months and test length ``M`` months the
range is valid iff ``total − T`` is a positive exact multiple of ``M``. Fold
``i`` trains on ``[start + i·M, start + i·M + T)`` and tests on
``[start + i·M + T, start + (i+1)·M + T)``. Months are added to the study
start (start-anchored, never repeated addition, so no drift accumulates),
clamped to the month end where the target day does not exist, and each
boundary is snapped to the first NYSE session on or after it on the
canonical calendar. Because shared boundaries snap identically, test windows
are contiguous and non-overlapping and every month after the first training
window is scored exactly once. A range that does not divide is refused with
the nearest valid end dates named — the spec-path splitter silently dropped
such a tail, which falsified the continuous-coverage claim.
Reference: PRD https://github.com/tim1016/learn-ai/issues/1925 revision 7.
Canonical implementation: this file.
Validated against: tests/research/walk_forward_study/test_folds.py.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from app.lean_sidecar.trading_calendar import expected_sessions


class FoldPlanError(ValueError):
    """The range, training length and test length do not produce whole folds."""

    def __init__(self, message: str, *, nearest_valid_ends: tuple[date, ...] = ()) -> None:
        super().__init__(message)
        self.nearest_valid_ends = nearest_valid_ends


@dataclass(frozen=True)
class FoldPlan:
    """One fold's calendar boundaries; ends are exclusive."""

    fold_index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def add_months(anchor: date, months: int) -> date:
    """``anchor`` plus ``months`` calendar months, clamped to the target month's last day."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def whole_months_between(start: date, end_exclusive: date) -> int | None:
    """Number of whole months from ``start`` to ``end_exclusive``, or ``None`` if it is not exact."""
    if end_exclusive <= start:
        return None
    months = (end_exclusive.year - start.year) * 12 + (end_exclusive.month - start.month)
    for candidate in (months, months - 1, months + 1):
        if candidate > 0 and add_months(start, candidate) == end_exclusive:
            return candidate
    return None


def snap_to_session(day: date) -> date:
    """The first NYSE session on or after ``day``."""
    sessions = expected_sessions(day, day + timedelta(days=14))
    if not sessions:
        raise FoldPlanError(f"no trading session within two weeks of {day.isoformat()}")
    return sessions[0]


def plan_folds(*, start: date, end_exclusive: date, training_months: int, test_months: int) -> list[FoldPlan]:
    """Whole folds over ``[start, end_exclusive)``; refuses anything else, naming the nearest valid ends."""
    if training_months < 1 or test_months < 1:
        raise FoldPlanError("training and test lengths must be at least one month")
    total = whole_months_between(start, end_exclusive)
    if total is None:
        below = max((m for m in range(1, 600) if add_months(start, m) <= end_exclusive), default=None)
        nearest = tuple(add_months(start, m) for m in ((below, below + 1) if below else (1,)))
        raise FoldPlanError(
            f"the range must be a whole number of months from {start.isoformat()}; nearest valid end dates: "
            + ", ".join(day.isoformat() for day in nearest),
            nearest_valid_ends=nearest,
        )
    remaining = total - training_months
    if remaining < test_months:
        needed = add_months(start, training_months + test_months)
        raise FoldPlanError(
            f"{total} months leave no room for a single fold after {training_months} months of training and "
            f"{test_months} months of test; the earliest valid end date is {needed.isoformat()}",
            nearest_valid_ends=(needed,),
        )
    folds_below = remaining // test_months
    if remaining % test_months != 0:
        nearest = (
            add_months(start, training_months + folds_below * test_months),
            add_months(start, training_months + (folds_below + 1) * test_months),
        )
        raise FoldPlanError(
            f"{total} months do not divide into whole folds of {test_months} months after {training_months} "
            f"months of training; nearest valid end dates: {nearest[0].isoformat()}, {nearest[1].isoformat()}",
            nearest_valid_ends=nearest,
        )
    plans: list[FoldPlan] = []
    for index in range(folds_below):
        offset = index * test_months
        plans.append(
            FoldPlan(
                fold_index=index,
                train_start=snap_to_session(add_months(start, offset)),
                train_end=snap_to_session(add_months(start, offset + training_months)),
                test_start=snap_to_session(add_months(start, offset + training_months)),
                test_end=snap_to_session(add_months(start, offset + training_months + test_months)),
            )
        )
    return plans
