"""The ET calendar day that contains one instant (ADR 0022 anchors).

One helper, two consumers: fee reconciliation bills a trade date on its ET
calendar day, and the live envelope's day P&L is "today" on the same day.

The two ends come from ``session_anchors`` -- ``et_day_end_ms`` is already the
canonical "next ET midnight", so this adds only the ``at_ms -> ET date`` step
its callers were repeating.
"""

from __future__ import annotations

from app.utils.session_anchors import et_date_at_ms, et_day_end_ms, et_midnight_ms


def et_day_window_ms(at_ms: int) -> tuple[int, int]:
    """``[ET midnight, next ET midnight)`` of the ET date containing ``at_ms``."""
    trade_date = et_date_at_ms(at_ms)
    return et_midnight_ms(trade_date), et_day_end_ms(trade_date)


__all__ = ["et_day_window_ms"]
