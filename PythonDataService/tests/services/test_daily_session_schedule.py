from __future__ import annotations

from datetime import UTC, date, datetime

from app.services.daily_session_schedule import (
    cohort_window_verdict,
    effective_stop_ms_for_date,
    start_boundary_verdict,
)


def _ms_utc(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp() * 1000)


def test_effective_stop_uses_default_force_flat_before_regular_close() -> None:
    stop = effective_stop_ms_for_date(date(2026, 7, 8), {"symbol": "SPY"})

    assert stop == _ms_utc(2026, 7, 8, 19, 55)


def test_effective_stop_is_disabled_for_extended_session_lifecycle_by_default() -> None:
    stop = effective_stop_ms_for_date(
        date(2026, 7, 8),
        {"allowed_sessions": ["RTH", "POST", "OVERNIGHT"]},
    )

    assert stop is None


def test_start_boundary_allows_extended_session_after_default_rth_stop() -> None:
    verdict = start_boundary_verdict(
        _ms_utc(2026, 7, 8, 21, 15),
        {"allowed_sessions": ["RTH", "POST"]},
    )

    assert verdict.allowed is True
    assert verdict.effective_stop_ms is None


def test_explicit_force_flat_still_blocks_extended_session_lifecycle() -> None:
    verdict = start_boundary_verdict(
        _ms_utc(2026, 7, 8, 23, 55),
        {"allowed_sessions": ["RTH", "POST"], "force_flat_at": "19:55"},
    )

    assert verdict.allowed is False
    assert verdict.reason_code == "SESSION_STOP_REACHED"
    assert verdict.effective_stop_ms == _ms_utc(2026, 7, 8, 23, 55)


def test_effective_stop_clamps_configured_stop_to_half_day_close() -> None:
    stop = effective_stop_ms_for_date(
        date(2026, 11, 27),
        {"force_flat_at": "15:55"},
    )

    assert stop == _ms_utc(2026, 11, 27, 18, 0)


def test_start_boundary_refuses_at_effective_stop_equality() -> None:
    verdict = start_boundary_verdict(
        _ms_utc(2026, 7, 8, 19, 55),
        {"force_flat_at": "15:55"},
    )

    assert verdict.allowed is False
    assert verdict.reason_code == "SESSION_STOP_REACHED"
    assert verdict.effective_stop_ms == _ms_utc(2026, 7, 8, 19, 55)


def test_start_boundary_allows_before_effective_stop() -> None:
    verdict = start_boundary_verdict(
        _ms_utc(2026, 7, 8, 19, 54),
        {"force_flat_at": "15:55"},
    )

    assert verdict.allowed is True
    assert verdict.session_date == "2026-07-08"


def test_cohort_window_refuses_when_staggered_validation_ends_after_stop() -> None:
    verdict = cohort_window_verdict(
        _ms_utc(2026, 7, 8, 19, 10),
        live_configs=({"force_flat_at": "15:55"},) * 3,
        required_window_ms=45 * 60 * 1_000 + 5_000,
    )

    assert verdict.allowed is False
    assert verdict.reason_code == "COHORT_WINDOW_EXCEEDS_SESSION_STOP"
    assert verdict.effective_stop_ms == _ms_utc(2026, 7, 8, 19, 55)
    assert verdict.required_window_end_ms == _ms_utc(2026, 7, 8, 19, 55) + 5_000


def test_cohort_window_allows_validation_that_ends_at_stop_boundary() -> None:
    verdict = cohort_window_verdict(
        _ms_utc(2026, 7, 8, 19, 9) + 55_000,
        live_configs=({"force_flat_at": "15:55"},) * 3,
        required_window_ms=45 * 60 * 1_000 + 5_000,
    )

    assert verdict.allowed is True
    assert verdict.effective_stop_ms == _ms_utc(2026, 7, 8, 19, 55)
