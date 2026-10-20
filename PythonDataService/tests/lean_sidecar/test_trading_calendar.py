"""TradingCalendar — single calendar source of truth for P2.5.

Per ADR 0022 (d), both the
``TrustedRunRequestModel`` validator and the staging iteration must
consult ONE calendar so they cannot drift. This module exposes the
four primitives both consumers need.

Backed by ``pandas_market_calendars`` (already in requirements-light).

DST test surface is mandatory — every assertion that crosses
2026-03-08 (EST→EDT) or 2026-11-01 (EDT→EST) must produce the right
UTC ms. Fixed-offset conversions are silent 1-hour bugs.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_ET = ZoneInfo("America/New_York")


class TestIsTradingDay:
    @pytest.mark.parametrize(
        ("session", "expected"),
        [
            # Tuesday, Jan 6 2026 — boring weekday, no holiday.
            pytest.param(date(2026, 1, 6), True, id="weekday_is_trading_day"),
            # Saturday + Sunday, Jan 10/11 2026.
            pytest.param(date(2026, 1, 10), False, id="saturday_is_not_trading_day"),
            pytest.param(date(2026, 1, 11), False, id="sunday_is_not_trading_day"),
            # 2026-01-19 — MLK Day (third Monday of January 2026).
            pytest.param(date(2026, 1, 19), False, id="federal_holiday_is_not_trading_day"),
            # 2026-12-25 — Christmas, Friday.
            pytest.param(date(2026, 12, 25), False, id="christmas_day_is_not_trading_day"),
        ],
    )
    def test_is_trading_day(self, session: date, expected: bool) -> None:
        from app.lean_sidecar.trading_calendar import is_trading_day

        assert is_trading_day(session) is expected


class TestIsEarlyClose:
    """Half-days the NYSE schedules a 13:00 ET close on.

    Standard half-days: Black Friday (day after Thanksgiving), and
    the day before Christmas / Independence Day when they fall on a
    weekday with a session.
    """

    @pytest.mark.parametrize(
        ("session", "expected"),
        [
            # 2026-11-27 — Black Friday (day after US Thanksgiving).
            pytest.param(date(2026, 11, 27), True, id="black_friday_is_early_close"),
            # 2026-12-24 — Christmas Eve, Thursday, NYSE early close.
            pytest.param(date(2026, 12, 24), True, id="christmas_eve_2026_is_early_close"),
            pytest.param(date(2026, 1, 6), False, id="normal_weekday_is_not_early_close"),
            # Non-session days are categorically not early-close.
            pytest.param(date(2026, 1, 10), False, id="weekend_is_not_early_close"),
            # MLK Day is a full holiday, not a half-day.
            pytest.param(date(2026, 1, 19), False, id="holiday_is_not_early_close"),
        ],
    )
    def test_is_early_close(self, session: date, expected: bool) -> None:
        from app.lean_sidecar.trading_calendar import is_early_close

        assert is_early_close(session) is expected


class TestNextTradingDay:
    @pytest.mark.parametrize(
        ("start", "expected"),
        [
            # Friday Jan 9 → Monday Jan 12 (skip Sat/Sun).
            pytest.param(date(2026, 1, 9), date(2026, 1, 12), id="skips_weekend"),
            # Friday Jan 16 → Tuesday Jan 20 (skip Sat/Sun + MLK Mon).
            pytest.param(date(2026, 1, 16), date(2026, 1, 20), id="skips_holiday"),
            # Tuesday Jan 6 → Wednesday Jan 7.
            pytest.param(date(2026, 1, 6), date(2026, 1, 7), id="after_trading_day"),
        ],
    )
    def test_next_trading_day(self, start: date, expected: date) -> None:
        from app.lean_sidecar.trading_calendar import next_trading_day

        assert next_trading_day(start) == expected


class TestSessionOpenMsUtc:
    """The 09:30-ET-to-UTC-ms conversion MUST go through the NY zone,
    never a fixed offset — DST days produce different UTC ms."""

    @pytest.mark.parametrize(
        ("session", "utc_hour"),
        [
            # 2026-01-06 09:30 ET = 14:30 UTC (EST, UTC-5).
            pytest.param(date(2026, 1, 6), 14, id="winter_session_open_uses_est"),
            # 2026-07-15 09:30 ET = 13:30 UTC (EDT, UTC-4).
            pytest.param(date(2026, 7, 15), 13, id="summer_session_open_uses_edt"),
            # DST start: EST→EDT happens 2am ET on 2026-03-08. The session
            # open on Monday 2026-03-09 is 09:30 EDT = 13:30 UTC.
            pytest.param(date(2026, 3, 9), 13, id="dst_start_2026_03_08"),
            # DST end: EDT→EST happens 2am ET on 2026-11-01 (Sunday).
            # Monday 2026-11-02 09:30 EST = 14:30 UTC.
            pytest.param(date(2026, 11, 2), 14, id="dst_end_2026_11_02"),
        ],
    )
    def test_session_open_goes_through_ny_zone(self, session: date, utc_hour: int) -> None:
        from app.lean_sidecar.trading_calendar import session_open_ms_utc

        open_et = datetime(session.year, session.month, session.day, 9, 30, tzinfo=_ET)
        expected = int(open_et.timestamp() * 1000)
        assert session_open_ms_utc(session) == expected, "session open is 09:30 ET resolved through the NY zone"
        # Sanity: the 09:30 ET wall clock lands on the expected UTC hour.
        assert open_et.astimezone(ZoneInfo("UTC")).hour == utc_hour, "09:30 ET maps to the expected UTC hour"

    def test_dst_transition_differs_by_one_hour(self) -> None:
        """The before-and-after DST flip session-opens must differ by
        the DST hour — a fixed-offset bug would make them identical."""
        from app.lean_sidecar.trading_calendar import session_open_ms_utc

        # Friday 2026-03-06 (still EST) vs Monday 2026-03-09 (now EDT).
        # Calendar-wise the gap is 3 days * 86_400_000 ms = 259_200_000 ms.
        # In real UTC ms, because DST cuts an hour, the gap is
        # 259_200_000 - 3_600_000 = 255_600_000 ms.
        delta = session_open_ms_utc(date(2026, 3, 9)) - session_open_ms_utc(date(2026, 3, 6))
        assert delta == 255_600_000


class TestBlockedDatesInRange:
    """The blocked-dates endpoint's weekends + holidays helper keeps
    BE and UI picker semantics aligned."""

    def test_returns_weekend_dates(self) -> None:
        from app.lean_sidecar.trading_calendar import blocked_dates_in_range

        # 2026-01-05 (Mon) → 2026-01-12 (Mon), inclusive — covers a
        # weekend.
        blocked = blocked_dates_in_range(date(2026, 1, 5), date(2026, 1, 12))
        assert date(2026, 1, 10) in blocked  # Sat
        assert date(2026, 1, 11) in blocked  # Sun
        assert date(2026, 1, 6) not in blocked  # trading day

    def test_returns_holiday(self) -> None:
        from app.lean_sidecar.trading_calendar import blocked_dates_in_range

        blocked = blocked_dates_in_range(date(2026, 1, 16), date(2026, 1, 20))
        assert date(2026, 1, 19) in blocked  # MLK Day

    def test_does_not_block_half_day_session(self) -> None:
        """Half-days are trading sessions, so the blocked-dates helper
        leaves them selectable while still blocking the adjacent
        holiday/weekend dates."""
        from app.lean_sidecar.trading_calendar import blocked_dates_in_range

        blocked = blocked_dates_in_range(date(2026, 11, 23), date(2026, 11, 30))
        # 2026-11-26 = Thanksgiving (holiday).
        # 2026-11-27 = Black Friday (half-day).
        assert blocked[date(2026, 11, 26)] == "holiday"
        assert date(2026, 11, 27) not in blocked


class TestRegularSessionMaskMsUtc:
    def test_matches_session_boundaries(self) -> None:
        from app.lean_sidecar.trading_calendar import (
            regular_session_mask_ms_utc,
            session_close_ms_utc,
            session_open_ms_utc,
        )

        session = date(2026, 1, 6)
        open_ms = session_open_ms_utc(session)
        close_ms = session_close_ms_utc(session)
        values = pd.Series([open_ms, close_ms - 1, close_ms, open_ms - 1])

        mask = regular_session_mask_ms_utc(values)

        assert mask.tolist() == [True, True, False, False]


class TestHolidayNamesInRange:
    def test_includes_ad_hoc_nyse_closures(self) -> None:
        from app.lean_sidecar.trading_calendar import holiday_names_in_range

        names = holiday_names_in_range(date(2012, 10, 29), date(2012, 10, 30))

        assert names[date(2012, 10, 29)] == "NYSE ad-hoc closure"
        assert names[date(2012, 10, 30)] == "NYSE ad-hoc closure"
