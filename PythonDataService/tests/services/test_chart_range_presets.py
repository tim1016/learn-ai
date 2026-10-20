"""Calendar-resolved chart range presets and the chart window's numeric authority.

Two contracts live here:

* ``resolve_range_presets`` — every preset is the last N **scheduled NYSE
  sessions**, so weekends, holidays, and the forming session are decided by
  the canonical calendar, never by client arithmetic. The goldens pin ``now``
  instants chosen to sit on the awkward edges: a Saturday, a Monday
  mid-session, a Monday before the open, and a week containing Labor Day
  2026. Anchors are ms-only: start = UTC midnight of the first trading date,
  end = the final UTC instant of the last trading date (23:59:59.999), so the
  same committed pair reads correctly for the chart's date-floor resolution
  AND for half-open instant consumers (the news query, dataset numeric
  overrides).
* ``resolve_request_dates`` — the ``start_ms_utc``/``end_ms_utc`` fields on
  ``POST /api/chart/data`` take per-field precedence over the date strings
  and floor to UTC calendar dates, the exact inverse of the Data Lab
  mapper's ``utcMsToIsoDate``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.services.chart_service import (
    RANGE_PRESETS,
    _utc_date_of_ms,
    _utc_day_end_ms,
    _utc_midnight_ms,
    resolve_range_presets,
    resolve_request_dates,
)

# 2026-09-12 is a Saturday; the NYSE sessions that week are Tue 09-08 (Mon
# 09-07 is Labor Day) through Fri 09-11.
SATURDAY_MS = int(datetime(2026, 9, 12, 18, 0, tzinfo=UTC).timestamp() * 1000)
# 2026-09-14 is a Monday, 15:00 UTC == 11:00 ET — mid regular session.
MONDAY_MID_SESSION_MS = int(datetime(2026, 9, 14, 15, 0, tzinfo=UTC).timestamp() * 1000)
# 2026-09-14 12:00 UTC == 08:00 ET (EDT) — a trading day whose session has
# not opened yet; nothing exists for today, so presets end at Friday.
MONDAY_PRE_OPEN_MS = int(datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp() * 1000)
# 2026-09-10 is a Thursday, 15:00 UTC == 11:00 ET — mid regular session.
THURSDAY_MID_SESSION_MS = int(datetime(2026, 9, 10, 15, 0, tzinfo=UTC).timestamp() * 1000)


def _by_key(now_ms: int, *, session: str = "rth") -> dict[str, dict]:
    return {preset["key"]: preset for preset in resolve_range_presets(now_ms, session=session)}


def test_saturday_presets_obey_the_wire_contract() -> None:
    """The list and every wire dict resolved on a Saturday."""
    presets = resolve_range_presets(SATURDAY_MS)
    by_key = {preset["key"]: preset for preset in presets}

    for key, _label, count in RANGE_PRESETS:
        if count is not None:
            assert by_key[key]["session_count"] == count, f"{key}: session count follows the preset table"
    assert [preset["key"] for preset in presets] == ["1D", "5D", "1M", "3M", "6M", "1Y", "2Y", "5Y"], (
        "5Y comes last, after 2Y"
    )
    for preset in presets:
        key = preset["key"]
        # Temporal wire values are int64 ms UTC only;
        # display strings are derived at the rendering boundary.
        assert "start_date" not in preset, f"{key}: wire dict carries no start_date string"
        assert "end_date" not in preset, f"{key}: wire dict carries no end_date string"
        # The exact inverse the chart request performs: a preset applied to a
        # Data Lab window resolves back to the same trading dates.
        from_date, to_date = resolve_request_dates(
            "1999-01-01",
            "1999-01-02",
            preset["start_ms_utc"],
            preset["end_ms_utc"],
        )
        assert date.fromisoformat(from_date) == _utc_date_of_ms(preset["start_ms_utc"]), (
            f"{key}: ms anchors round-trip through the chart resolution (start)"
        )
        assert date.fromisoformat(to_date) == _utc_date_of_ms(preset["end_ms_utc"]), (
            f"{key}: ms anchors round-trip through the chart resolution (end)"
        )
        # The news query and the dataset endpoints read the same committed ms
        # pair as half-open instants; equal endpoints would make a single-day
        # window empty there (and trip DatasetGenerationRequest's
        # start-before-end validator).
        assert preset["end_ms_utc"] > preset["start_ms_utc"], (
            f"{key}: window is non-degenerate for half-open readers"
        )


def test_saturday_presets_resolve_their_windows_from_the_calendar() -> None:
    by_key = _by_key(SATURDAY_MS)

    one_day = by_key["1D"]
    assert one_day["start_ms_utc"] == _utc_midnight_ms(date(2026, 9, 11)), "1D on a Saturday starts on Friday"
    assert one_day["end_ms_utc"] == _utc_day_end_ms(date(2026, 9, 11)), "1D on a Saturday ends on Friday"
    assert one_day["session_count"] == 1, "1D holds one session"

    # The week of 2026-09-07 has four sessions (Labor Day Monday) plus the
    # prior Friday — five sessions with a holiday simply absent from the span.
    one_week = by_key["5D"]
    assert one_week["start_ms_utc"] == _utc_midnight_ms(date(2026, 9, 4)), "5D skips the Labor Day holiday (start)"
    assert one_week["end_ms_utc"] == _utc_day_end_ms(date(2026, 9, 11)), "5D skips the Labor Day holiday (end)"
    assert one_week["session_count"] == 5, "5D holds five sessions"
    assert _utc_date_of_ms(one_week["start_ms_utc"]) == date(2026, 9, 4), "5D start reads back as its trading date"
    assert _utc_date_of_ms(one_week["end_ms_utc"]) == date(2026, 9, 11), "5D end reads back as its trading date"

    # 5Y starts on the provider's first day, two days inside five years, so every bar it asks for can be fetched.
    five_year = by_key["5Y"]
    # Five years before Saturday 2026-09-12 is 2021-09-12; two days inside is Tuesday the 14th.
    assert five_year["start_ms_utc"] == _utc_midnight_ms(date(2021, 9, 14)), "5Y starts on the provider's first day"
    assert five_year["end_ms_utc"] == _utc_day_end_ms(date(2026, 9, 11)), "5Y ends on the last session"
    assert five_year["session_count"] == len(expected_sessions(date(2021, 9, 14), date(2026, 9, 11))), (
        "5Y counts every session from the provider's first day"
    )
    assert five_year["label"] == "Past 5 years", "5Y label"

    one_month = by_key["1M"]
    assert one_month["estimated_bars_per_timeframe"]["1D"] == 21, "1M daily-bar estimate is the shared estimator's"
    assert one_month["estimated_bars_per_timeframe"]["1m"] > 0, "1M minute-bar estimate is positive"


def test_five_year_preset_starts_on_a_session_when_the_provider_first_day_is_closed() -> None:
    # Five years before Thursday 2026-09-10, two days inside, is Sunday 2021-09-12.
    preset = _by_key(THURSDAY_MID_SESSION_MS)["5Y"]

    assert preset["start_ms_utc"] == _utc_midnight_ms(date(2021, 9, 13))
    assert preset["end_ms_utc"] == _utc_day_end_ms(date(2026, 9, 10))


def test_mid_session_preset_includes_today() -> None:
    """The forming session is part of the window — the chart serves its live
    tail from the provider by design."""
    preset = _by_key(MONDAY_MID_SESSION_MS)["1D"]
    assert preset["start_ms_utc"] == _utc_midnight_ms(date(2026, 9, 14))
    assert preset["end_ms_utc"] == _utc_day_end_ms(date(2026, 9, 14))


def test_pre_open_trading_day_resolves_to_the_prior_session() -> None:
    """Before the 09:30 ET open no bar exists for today; a preset pointing
    at the empty session would be NO_DATA waiting to happen."""
    for preset in resolve_range_presets(MONDAY_PRE_OPEN_MS):
        assert _utc_date_of_ms(preset["end_ms_utc"]) == date(2026, 9, 11)


def test_extended_session_arg_flips_at_the_rth_open_too() -> None:
    """The calendar's session windows are RTH-shaped, so the begun-at test is
    the RTH open for both session arguments; the extended premarket edge is
    recorded in known-gaps §13a. Pinned here so the day it flips is a
    deliberate decision, not drift."""
    preset = _by_key(MONDAY_PRE_OPEN_MS, session="extended")["1D"]
    assert _utc_date_of_ms(preset["end_ms_utc"]) == date(2026, 9, 11)
    preset = _by_key(MONDAY_MID_SESSION_MS, session="extended")["1D"]
    assert _utc_date_of_ms(preset["end_ms_utc"]) == date(2026, 9, 14)


# ──────────────────────────────────────────────
# resolve_request_dates — numeric window authority
# ──────────────────────────────────────────────
AUG_1_MS = _utc_midnight_ms(date(2026, 8, 1))
SEP_11_MS = _utc_midnight_ms(date(2026, 9, 11))


@pytest.mark.parametrize(
    ("from_str", "to_str", "start_ms", "end_ms"),
    [
        pytest.param(
            "1999-01-01", "1999-12-31", AUG_1_MS, SEP_11_MS,
            id="numeric_fields_take_per_field_precedence_over_the_strings",
        ),
        pytest.param(
            "2026-08-01", "2026-09-11", None, SEP_11_MS,
            id="one_numeric_field_leaves_the_other_string_in_charge-end_ms_only",
        ),
        pytest.param(
            "2026-08-01", "2026-09-11", AUG_1_MS, None,
            id="one_numeric_field_leaves_the_other_string_in_charge-start_ms_only",
        ),
        pytest.param(
            "2026-08-01", "2026-09-11", None, None,
            id="strings_still_drive_the_window_when_no_ms_is_supplied",
        ),
        pytest.param(
            "1999-01-01", "2026-09-11", AUG_1_MS + (23 * 60 + 59) * 60_000, None,
            id="a_non_midnight_instant_floors_to_its_utc_calendar_date",
        ),
    ],
)
def test_resolve_request_dates_numeric_window_authority(
    from_str: str, to_str: str, start_ms: int | None, end_ms: int | None
) -> None:
    from_date, to_date = resolve_request_dates(from_str, to_str, start_ms, end_ms)
    assert (from_date, to_date) == ("2026-08-01", "2026-09-11"), "resolved (from_date, to_date)"


def test_an_inverted_resolved_window_is_refused() -> None:
    with pytest.raises(ValueError, match="inverted"):
        resolve_request_dates("2026-08-01", "2026-09-11", SEP_11_MS, AUG_1_MS)
