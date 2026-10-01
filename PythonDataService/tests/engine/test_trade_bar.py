"""Tests for app.engine.data.trade_bar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.engine.data.trade_bar import TradeBar


def _bar(period: timedelta) -> TradeBar:
    start = datetime(2024, 1, 1, 14, 30, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + period,
        open=Decimal("100.00"),
        high=Decimal("100.50"),
        low=Decimal("99.80"),
        close=Decimal("100.20"),
        volume=1_000_000,
    )


@pytest.mark.parametrize(
    "period,expected_seconds",
    [
        (timedelta(minutes=1), 60.0),
        (timedelta(minutes=15), 900.0),
        (timedelta(hours=1), 3600.0),
        (timedelta(days=1), 86400.0),
    ],
)
def test_period_seconds_matches_end_minus_time(period: timedelta, expected_seconds: float):
    bar = _bar(period)

    assert bar.period_seconds == pytest.approx(expected_seconds, abs=1e-12, rel=0)
