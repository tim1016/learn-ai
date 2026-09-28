"""The ET calendar day that contains one instant (ADR 0022 anchors).

One helper, two consumers: fee reconciliation bills a trade date on its ET
calendar day, and the live envelope's day P&L is "today" on the same day.

The two ends come from ``session_anchors`` -- ``et_day_end_ms`` is already the
canonical "next ET midnight", so this adds only the ``at_ms -> ET date`` step
its callers were repeating.

The account Activity periods (PRD #2560) are windows of those same ET days:
``today`` is the ET day containing the instant, and ``30d``/``60d`` reach back
to the ET midnight opening the 30th/60th most recent NYSE session. Which days
are sessions comes from the canonical calendar module, never a weekday rule.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from app.lean_sidecar.trading_calendar import expected_sessions
from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms

ActivityPeriod = Literal["today", "30d", "60d"]

#: How many NYSE sessions each trailing Activity period spans, today included
#: when today is a session.
ACTIVITY_PERIOD_SESSIONS: dict[ActivityPeriod, int] = {"30d": 30, "60d": 60}


def et_day_window_ms(at_ms: int) -> tuple[int, int]:
    """``[ET midnight, next ET midnight)`` of the ET date containing ``at_ms``."""
    trade_date = et_date_at_ms(at_ms)
    return et_midnight_ms(trade_date), et_day_end_ms(trade_date)


def activity_period_start_ms(period: ActivityPeriod, now_ms: int) -> int:
    """The ET midnight an Activity period opens at, for an instant ``now_ms``.

    ``today`` opens at the ET midnight of ``now_ms``'s ET date, the same day
    fees are billed on. ``30d`` and ``60d`` open at the ET midnight of the
    earliest of the last 30 or 60 NYSE sessions on or before that date, so a
    weekend, a holiday or a DST change never shortens or stretches a period.
    The period runs from there to ``now_ms``.
    """
    today = et_date_at_ms(now_ms)
    if period == "today":
        return et_midnight_ms(today)
    count = ACTIVITY_PERIOD_SESSIONS[period]
    # Two calendar days per session plus a fortnight covers any weekend and
    # holiday cluster; the calendar decides which of them are sessions.
    sessions = expected_sessions(today - timedelta(days=2 * count + 14), today)
    if len(sessions) < count:
        raise LookupError(f"the calendar has fewer than {count} NYSE sessions before {today.isoformat()}")
    return et_midnight_ms(sessions[-count])


__all__ = [
    "ACTIVITY_PERIOD_SESSIONS",
    "ActivityPeriod",
    "activity_period_start_ms",
    "et_day_window_ms",
]
