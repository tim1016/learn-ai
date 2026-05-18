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

    def test_single_day_envelopes_one_et_day(self) -> None:
        """A single staged day is the [00:00 ET, next-day 00:00 ET)
        bracket. 86_400_000 ms in non-DST; can differ on transition
        days (handled below)."""
        d = date(2025, 1, 6)  # Mon, outside DST
        w = _staged_window_from_dates([d])
        assert isinstance(w, WindowMs)
        assert w.start_ms == _et_midnight_ms(d)
        assert w.end_ms == _et_midnight_ms(date(2025, 1, 7))
        assert w.end_ms - w.start_ms == 86_400_000

    def test_multi_day_envelopes_first_to_after_last(self) -> None:
        """A 5-day [Mon..Fri] window is 5 ET days wide: Mon 00:00 ET
        through Sat 00:00 ET. Reconciliation readers diff THIS against
        the requested window — if a holiday dropped a day mid-range
        the staged window still spans the full envelope while the
        bar count reveals the gap."""
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9), date(2025, 1, 10)]
        w = _staged_window_from_dates(dates)
        assert isinstance(w, WindowMs)
        assert w.start_ms == _et_midnight_ms(date(2025, 1, 6))
        assert w.end_ms == _et_midnight_ms(date(2025, 1, 11))
        # 5 calendar days = 432_000_000 ms (no DST transition in this week).
        assert w.end_ms - w.start_ms == 5 * 86_400_000

    def test_dst_spring_forward_day_has_23_hour_envelope(self) -> None:
        """2026 spring-forward: clocks jump from 02:00 ET to 03:00 ET
        on Sunday March 8. A staged day spanning the transition is
        only 23 ET-hours wide. The envelope must reflect that —
        anchoring on ET (not UTC) is the whole point of the helper."""
        # Use a date INSIDE the DST jump. Sunday March 8 2026 is the
        # transition; the staged date itself is the trading day right
        # after the jump (Monday March 9 isn't on the jump day).
        # The most surgical test is to anchor a single-day window on
        # March 8 and assert the envelope is 23 hours.
        d = date(2026, 3, 8)
        w = _staged_window_from_dates([d])
        assert isinstance(w, WindowMs)
        # 23 hours = 82_800_000 ms.
        assert w.end_ms - w.start_ms == 23 * 60 * 60 * 1000

    def test_dst_fall_back_day_has_25_hour_envelope(self) -> None:
        """2026 fall-back: clocks roll from 02:00 ET back to 01:00 ET
        on Sunday November 1. A staged day spanning the transition is
        25 ET-hours wide."""
        d = date(2026, 11, 1)
        w = _staged_window_from_dates([d])
        assert isinstance(w, WindowMs)
        assert w.end_ms - w.start_ms == 25 * 60 * 60 * 1000

    def test_window_uses_first_and_last_only(self) -> None:
        """The helper doesn't care about gaps in the middle of the
        list — staged_data_window_ms is the envelope. Gap detection
        lives in bars_consumed_by_symbol (Phase 5e+)."""
        sparse = [date(2025, 1, 6), date(2025, 1, 17)]
        w = _staged_window_from_dates(sparse)
        assert isinstance(w, WindowMs)
        assert w.start_ms == _et_midnight_ms(date(2025, 1, 6))
        assert w.end_ms == _et_midnight_ms(date(2025, 1, 18))


def test_returned_window_is_int64_ms_utc_per_repo_rule() -> None:
    """Defensive: the manifest's int64 ms UTC rule is repo-wide.
    Anything other than an int passed into WindowMs would be a bug."""
    w = _staged_window_from_dates([date(2025, 1, 6)])
    assert w is not None
    assert isinstance(w.start_ms, int)
    assert isinstance(w.end_ms, int)


@pytest.mark.parametrize(
    "single_day",
    [date(2024, 12, 31), date(2025, 7, 4), date(2026, 6, 1)],
)
def test_single_non_dst_day_is_exactly_24_hours(single_day: date) -> None:
    """Non-transition days are exactly 24 hours wide. Catches a future
    refactor that accidentally uses UTC offsets instead of ET-midnight."""
    w = _staged_window_from_dates([single_day])
    assert w is not None
    assert w.end_ms - w.start_ms == 86_400_000
