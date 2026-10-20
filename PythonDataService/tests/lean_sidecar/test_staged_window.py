"""Phase 5d — unit tests for _staged_window_from_dates.

The helper builds the staged-data window (closes half of invariant
#16) from the trading-date sequence the orchestrator iterated. Pure
function over a small surface, so the tests focus on:
- envelope shape (first date 00:00 ET → last date + 1 day 00:00 ET)
- DST stability (ET-midnight reference, not UTC-fixed-offset)
- single-day case
- empty-list returns None (not a zero-length window)
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.lean_sidecar.manifest import WindowMs
from app.services.lean_sidecar_service import _staged_window_from_dates

_ET = ZoneInfo("America/New_York")


def _et_midnight_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=_ET).timestamp() * 1000)


class TestStagedWindowFromDates:
    def test_empty_list_returns_none(self) -> None:
        """An empty staged-dates list must NOT produce a WindowMs.
        WindowMs's invariant rejects zero-length windows; returning
        None is the honest "no data was staged" signal."""
        assert _staged_window_from_dates([]) is None

    @pytest.mark.parametrize(
        ("dates", "first_day", "after_last_day", "width_ms"),
        [
            # A single staged day is the [00:00 ET, next-day 00:00 ET)
            # bracket: Mon 2025-01-06, outside DST, 86_400_000 ms.
            pytest.param(
                (date(2025, 1, 6),),
                date(2025, 1, 6),
                date(2025, 1, 7),
                86_400_000,
                id="single_day_envelopes_one_et_day",
            ),
            # A 5-day [Mon..Fri] window is 5 ET days wide: Mon 00:00 ET
            # through Sat 00:00 ET (no DST transition in this week,
            # 5 calendar days = 432_000_000 ms). Reconciliation readers
            # diff THIS against the requested window — if a holiday
            # dropped a day mid-range the staged window still spans the
            # full envelope while the bar count reveals the gap.
            pytest.param(
                (date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9), date(2025, 1, 10)),
                date(2025, 1, 6),
                date(2025, 1, 11),
                5 * 86_400_000,
                id="multi_day_envelopes_first_to_after_last",
            ),
            # 2026 spring-forward: clocks jump from 02:00 ET to 03:00 ET on
            # Sunday March 8. A staged day spanning the transition is only
            # 23 ET-hours (82_800_000 ms) wide; anchoring on ET (not UTC)
            # is the whole point of the helper.
            pytest.param(
                (date(2026, 3, 8),),
                date(2026, 3, 8),
                date(2026, 3, 9),
                23 * 60 * 60 * 1000,
                id="dst_spring_forward_day_has_23_hour_envelope",
            ),
            # 2026 fall-back: clocks roll from 02:00 ET back to 01:00 ET on
            # Sunday November 1. The staged day is 25 ET-hours wide.
            pytest.param(
                (date(2026, 11, 1),),
                date(2026, 11, 1),
                date(2026, 11, 2),
                25 * 60 * 60 * 1000,
                id="dst_fall_back_day_has_25_hour_envelope",
            ),
            # The helper doesn't care about gaps in the middle of the list —
            # staged_data_window_ms is the envelope. Gap detection lives in
            # bars_consumed_by_symbol (Phase 5e+).
            pytest.param(
                (date(2025, 1, 6), date(2025, 1, 17)),
                date(2025, 1, 6),
                date(2025, 1, 18),
                12 * 86_400_000,
                id="window_uses_first_and_last_only",
            ),
        ],
    )
    def test_window_envelopes_first_date_to_day_after_last(
        self,
        dates: tuple[date, ...],
        first_day: date,
        after_last_day: date,
        width_ms: int,
    ) -> None:
        w = _staged_window_from_dates(list(dates))

        assert isinstance(w, WindowMs), "returns a WindowMs"
        assert w.start_ms == _et_midnight_ms(first_day), "start is 00:00 ET of the first staged date"
        assert w.end_ms == _et_midnight_ms(after_last_day), "end is 00:00 ET of the day after the last staged date"
        assert w.end_ms - w.start_ms == width_ms, "envelope width in ET hours"
        # Defensive: the manifest's int64 ms UTC rule is repo-wide. Anything
        # other than an int passed into WindowMs would be a bug.
        assert isinstance(w.start_ms, int), "start_ms is an int (int64 ms UTC)"
        assert isinstance(w.end_ms, int), "end_ms is an int (int64 ms UTC)"
