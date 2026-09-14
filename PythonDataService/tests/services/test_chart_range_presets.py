"""Calendar-resolved chart range presets and the chart window's numeric authority.

Two contracts live here:

* ``resolve_range_presets`` — every preset is the last N **scheduled NYSE
  sessions**, so weekends, holidays, and the forming session are decided by
  the canonical calendar, never by client arithmetic. The goldens pin ``now``
  instants chosen to sit on the awkward edges: a Saturday, a Monday
  mid-session, and a week containing Labor Day 2026.
* ``resolve_request_dates`` — the ``start_ms_utc``/``end_ms_utc`` fields on
  ``POST /api/chart/data`` take per-field precedence over the date strings
  and floor to UTC calendar dates, the exact inverse of the Data Lab
  mapper's ``utcMsToIsoDate`` (data-lab workspace redesign PRD §12).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.services.chart_service import (
    RANGE_PRESETS,
    _utc_midnight_ms,
    resolve_range_presets,
    resolve_request_dates,
)

# 2026-09-12 is a Saturday; the NYSE sessions that week are Tue 09-08 (Mon
# 09-07 is Labor Day) through Fri 09-11.
SATURDAY_MS = int(datetime(2026, 9, 12, 18, 0, tzinfo=UTC).timestamp() * 1000)
# 2026-09-14 is a Monday, 15:00 UTC == 11:00 ET — mid regular session.
MONDAY_MID_SESSION_MS = int(datetime(2026, 9, 14, 15, 0, tzinfo=UTC).timestamp() * 1000)


def _by_key(now_ms: int) -> dict[str, dict]:
    return {preset["key"]: preset for preset in resolve_range_presets(now_ms)}


def test_presets_cover_the_full_key_table() -> None:
    presets = resolve_range_presets(SATURDAY_MS)
    assert [p["key"] for p in presets] == [key for key, _label, _count in RANGE_PRESETS]


def test_one_day_preset_on_a_saturday_resolves_to_friday() -> None:
    preset = _by_key(SATURDAY_MS)["1D"]
    assert preset["start_date"] == "2026-09-11"
    assert preset["end_date"] == "2026-09-11"
    assert preset["session_count"] == 1


def test_one_week_preset_skips_the_labor_day_holiday() -> None:
    """The week of 2026-09-07 has four sessions (Labor Day Monday) plus the
    prior Friday — five sessions with a holiday simply absent from the span."""
    preset = _by_key(SATURDAY_MS)["5D"]
    assert preset["start_date"] == "2026-09-04"
    assert preset["end_date"] == "2026-09-11"
    assert preset["session_count"] == 5
    assert "2026-09-07" not in (preset["start_date"], preset["end_date"])


def test_session_counts_follow_the_preset_table() -> None:
    presets = {p["key"]: p for p in resolve_range_presets(SATURDAY_MS)}
    for key, _label, count in RANGE_PRESETS:
        assert presets[key]["session_count"] == count


def test_mid_session_preset_includes_today() -> None:
    """The forming session is part of the window — the chart serves its live
    tail from the provider by design."""
    preset = _by_key(MONDAY_MID_SESSION_MS)["1D"]
    assert preset["start_date"] == "2026-09-14"
    assert preset["end_date"] == "2026-09-14"


def test_preset_ms_anchors_are_utc_midnight_and_round_trip() -> None:
    for preset in resolve_range_presets(SATURDAY_MS):
        assert preset["start_ms_utc"] == _utc_midnight_ms(date.fromisoformat(preset["start_date"]))
        assert preset["end_ms_utc"] == _utc_midnight_ms(date.fromisoformat(preset["end_date"]))
        # The exact inverse the chart request performs: a preset applied to a
        # Data Lab window resolves back to the same trading dates.
        from_date, to_date = resolve_request_dates(
            "1999-01-01",
            "1999-01-02",
            preset["start_ms_utc"],
            preset["end_ms_utc"],
        )
        assert (from_date, to_date) == (preset["start_date"], preset["end_date"])


def test_preset_estimates_come_from_the_shared_estimator() -> None:
    preset = _by_key(SATURDAY_MS)["1M"]
    assert preset["estimated_bars_per_timeframe"]["1D"] == 21
    assert preset["estimated_bars_per_timeframe"]["1m"] > 0


# ──────────────────────────────────────────────
# resolve_request_dates — numeric window authority
# ──────────────────────────────────────────────
AUG_1_MS = _utc_midnight_ms(date(2026, 8, 1))
SEP_11_MS = _utc_midnight_ms(date(2026, 9, 11))


def test_numeric_fields_take_per_field_precedence_over_the_strings() -> None:
    from_date, to_date = resolve_request_dates("1999-01-01", "1999-12-31", AUG_1_MS, SEP_11_MS)
    assert (from_date, to_date) == ("2026-08-01", "2026-09-11")


def test_one_numeric_field_leaves_the_other_string_in_charge() -> None:
    from_date, to_date = resolve_request_dates("2026-08-01", "2026-09-11", None, SEP_11_MS)
    assert (from_date, to_date) == ("2026-08-01", "2026-09-11")
    from_date, to_date = resolve_request_dates("2026-08-01", "2026-09-11", AUG_1_MS, None)
    assert (from_date, to_date) == ("2026-08-01", "2026-09-11")


def test_strings_still_drive_the_window_when_no_ms_is_supplied() -> None:
    from_date, to_date = resolve_request_dates("2026-08-01", "2026-09-11", None, None)
    assert (from_date, to_date) == ("2026-08-01", "2026-09-11")


def test_a_non_midnight_instant_floors_to_its_utc_calendar_date() -> None:
    intraday = AUG_1_MS + (23 * 60 + 59) * 60_000
    from_date, to_date = resolve_request_dates("1999-01-01", "2026-09-11", intraday, None)
    assert (from_date, to_date) == ("2026-08-01", "2026-09-11")


def test_an_inverted_resolved_window_is_refused() -> None:
    with pytest.raises(ValueError, match="inverted"):
        resolve_request_dates("2026-08-01", "2026-09-11", SEP_11_MS, AUG_1_MS)
