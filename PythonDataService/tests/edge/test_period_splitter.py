"""Tests for period_splitter."""

from __future__ import annotations

from datetime import UTC, datetime

from app.engine.edge.period_splitter import (
    APPROX_MS_PER_YEAR,
    calendar_year_buckets,
    rolling_windows,
    walk_forward,
)


def _ms(y: int, m: int = 1, d: int = 1) -> int:
    return int(datetime(y, m, d, tzinfo=UTC).timestamp() * 1000)


def test_rolling_windows_produces_expected_count():
    out = rolling_windows(
        start_ms=_ms(2020),
        end_ms=_ms(2024),
        window_years=2.0,
        step_months=6.0,
    )
    # 4 years, 2-year window, 6-month step → ~5 windows
    assert 4 <= len(out) <= 6
    for p in out:
        assert p.end_ms - p.start_ms == int(2.0 * APPROX_MS_PER_YEAR)


def test_rolling_windows_falls_back_when_history_too_short():
    out = rolling_windows(
        start_ms=_ms(2020),
        end_ms=_ms(2020, 6),  # 6 months
        window_years=2.0,
        step_months=6.0,
    )
    assert len(out) == 1
    assert out[0].start_ms == _ms(2020)
    assert out[0].end_ms == _ms(2020, 6)


def test_calendar_year_buckets_count():
    out = calendar_year_buckets(start_ms=_ms(2021), end_ms=_ms(2024))
    labels = [p.label for p in out]
    assert labels == ["cal_2021", "cal_2022", "cal_2023"]


def test_calendar_year_buckets_clipped_at_edges():
    out = calendar_year_buckets(start_ms=_ms(2022, 6), end_ms=_ms(2023, 6))
    assert out[0].start_ms == _ms(2022, 6)
    assert out[-1].end_ms == _ms(2023, 6)


def test_walk_forward_train_test_pairing():
    out = walk_forward(
        start_ms=_ms(2020),
        end_ms=_ms(2024),
        train_years=2.0,
        test_months=6.0,
    )
    for train, test in out:
        assert train.end_ms == test.start_ms
        assert test.end_ms - test.start_ms <= int(0.5 * APPROX_MS_PER_YEAR) + 1
