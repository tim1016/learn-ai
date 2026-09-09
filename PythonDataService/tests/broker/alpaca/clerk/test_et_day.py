"""The ET calendar day that contains one instant."""

from __future__ import annotations

from datetime import date

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.services.session_authority import et_minute_of_day_ms
from app.utils.session_anchors import et_midnight_ms


def test_the_window_is_the_et_calendar_day_containing_the_instant() -> None:
    noon_et = et_minute_of_day_ms(date(2026, 9, 8), 12 * 60)
    start, end = et_day_window_ms(noon_et)
    assert start == et_midnight_ms(date(2026, 9, 8))
    assert end == et_midnight_ms(date(2026, 9, 9))
    # 23:30 ET on 2026-09-08 is 03:30 UTC on 2026-09-09 and still the ET 8th.
    late = et_minute_of_day_ms(date(2026, 9, 8), 23 * 60 + 30)
    assert et_day_window_ms(late) == (start, end)
