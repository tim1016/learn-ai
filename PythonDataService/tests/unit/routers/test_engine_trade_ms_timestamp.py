"""Regression: engine trade timestamps must stay numeric ms UTC on the wire."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.strategy.base import LoggedTrade
from app.routers.engine import _format_trade, _format_trade_record, _to_ms_utc


def test_to_ms_utc_tz_aware_et_produces_epoch_milliseconds() -> None:
    et = ZoneInfo("America/New_York")
    ts = datetime(2025, 5, 30, 14, 30, tzinfo=et)

    assert _to_ms_utc(ts) == 1_748_629_800_000


def test_to_ms_utc_tz_aware_et_in_winter_uses_est_offset() -> None:
    et = ZoneInfo("America/New_York")
    ts = datetime(2025, 1, 15, 14, 30, tzinfo=et)

    assert _to_ms_utc(ts) == 1_736_969_400_000


def test_to_ms_utc_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _to_ms_utc(datetime(2025, 5, 30, 14, 30))


def test_format_trade_emits_numeric_timestamps() -> None:
    et = ZoneInfo("America/New_York")
    trade = LoggedTrade(
        entry_time=datetime(2025, 5, 30, 14, 30, tzinfo=et),
        entry_price=Decimal("100.0"),
        exit_time=datetime(2025, 5, 30, 15, 30, tzinfo=et),
        exit_price=Decimal("101.0"),
        quantity=1,
        pnl_pts=Decimal("1.0"),
        pnl_pct=Decimal("0.01"),
        result="WIN",
        signal_reason="test",
    )

    formatted = _format_trade(1, trade)

    assert formatted.entry_time == 1_748_629_800_000
    assert formatted.exit_time == 1_748_633_400_000
    assert isinstance(formatted.entry_time, int)
    assert isinstance(formatted.exit_time, int)


def test_format_trade_record_emits_numeric_timestamps_for_lean_statistics() -> None:
    et = ZoneInfo("America/New_York")
    trade = LoggedTrade(
        entry_time=datetime(2025, 5, 30, 14, 30, tzinfo=et),
        entry_price=Decimal("100.0"),
        exit_time=datetime(2025, 5, 30, 15, 30, tzinfo=et),
        exit_price=Decimal("101.0"),
        quantity=1,
        pnl_pts=Decimal("1.0"),
        pnl_pct=Decimal("0.01"),
        result="WIN",
        signal_reason="test",
    )

    formatted = _format_trade_record(1, trade, cumulative_pnl_pct=0.01)

    assert formatted.entry_timestamp == 1_748_629_800_000
    assert formatted.exit_timestamp == 1_748_633_400_000
    assert isinstance(formatted.entry_timestamp, int)
    assert isinstance(formatted.exit_timestamp, int)
