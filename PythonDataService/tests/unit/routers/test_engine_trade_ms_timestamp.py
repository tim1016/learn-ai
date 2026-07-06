"""Regression: engine trade timestamps must stay numeric ms UTC on the wire."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.engine.strategy.base import LoggedTrade
from app.routers.engine import _format_trade, _format_trade_record, _to_ms_utc


def test_to_ms_utc_tz_aware_et_in_summer_uses_edt_offset() -> None:
    """ET 14:30 (EDT, summer) -> UTC 18:30 as epoch ms."""
    et = ZoneInfo("America/New_York")
    ts = datetime(2025, 5, 30, 14, 30, tzinfo=et)

    assert _to_ms_utc(ts) == 1_748_629_800_000


def test_to_ms_utc_tz_aware_et_in_winter_uses_est_offset() -> None:
    """ET 14:30 (EST, winter) -> UTC 19:30 as epoch ms."""
    et = ZoneInfo("America/New_York")
    ts = datetime(2025, 1, 15, 14, 30, tzinfo=et)

    assert _to_ms_utc(ts) == 1_736_969_400_000


def test_to_ms_utc_rejects_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _to_ms_utc(datetime(2025, 5, 30, 14, 30))


@pytest.mark.parametrize(
    ("input_dt", "expected"),
    [
        (
            datetime(2025, 1, 13, 9, 30, tzinfo=UTC),
            1_736_760_600_000,
        ),
        (
            datetime(2025, 1, 13, 9, 30, 45, 123456, tzinfo=UTC),
            1_736_760_645_123,
        ),
    ],
)
def test_to_ms_utc_misc_inputs(input_dt: datetime, expected: int) -> None:
    assert _to_ms_utc(input_dt) == expected


def _logged_trade() -> LoggedTrade:
    et = ZoneInfo("America/New_York")
    return LoggedTrade(
        entry_time=datetime(2025, 5, 30, 14, 30, tzinfo=et),
        entry_price=Decimal("100.0"),
        exit_time=datetime(2025, 5, 30, 15, 30, tzinfo=et),
        exit_price=Decimal("101.0"),
        quantity=3,
        pnl_pts=Decimal("1.0"),
        pnl_pct=Decimal("0.01"),
        result="WIN",
        indicators={"ema5": Decimal("99.5")},
        signal_reason="test",
    )


def test_format_trade_emits_numeric_timestamps() -> None:
    formatted = _format_trade(1, _logged_trade())

    assert formatted.entry_time == 1_748_629_800_000
    assert formatted.exit_time == 1_748_633_400_000
    assert isinstance(formatted.entry_time, int)
    assert isinstance(formatted.exit_time, int)
    assert formatted.quantity == 3
    assert formatted.indicators == {"ema5": 99.5}


def test_format_trade_record_emits_numeric_timestamps_for_lean_statistics() -> None:
    formatted = _format_trade_record(1, _logged_trade(), cumulative_pnl_pct=0.01)

    assert formatted.entry_timestamp == 1_748_629_800_000
    assert formatted.exit_timestamp == 1_748_633_400_000
    assert isinstance(formatted.entry_timestamp, int)
    assert isinstance(formatted.exit_timestamp, int)
    assert formatted.cumulative_pnl_pct == pytest.approx(0.01, abs=1e-12, rel=0)
