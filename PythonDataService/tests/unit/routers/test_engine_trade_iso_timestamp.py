"""Regression: engine trade timestamps must be .NET-parsable ISO-8601 UTC.

Pre-fix behavior: ``_format_trade`` emitted ``"%Y-%m-%d %H:%M"`` (space
separator, no seconds, no Z designator). ``Backend/StudiesApi.cs:ParseUtc``
uses ``DateTimeOffset.ParseExact`` with the strict patterns
``yyyy-MM-ddTHH:mm:ss'Z'`` and ``yyyy-MM-ddTHH:mm:ss.ffffff'Z'``. The two
disagreed, so every Python engine ``POST /api/studies`` was silently
500'ing with ``System.FormatException`` after the parser was hardened —
backtests ran fine, history rows never landed.

This test pins the wire-format contract.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.routers.engine import _to_utc_iso

# Mirror of the regex Backend/StudiesApi.cs:ParseUtc accepts.
_DOTNET_ISO_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_to_utc_iso_tz_aware_et_produces_dotnet_compatible_string() -> None:
    """ET 14:30 (EDT, summer) → UTC 18:30 with Z designator."""
    et = ZoneInfo("America/New_York")
    ts = datetime(2025, 5, 30, 14, 30, tzinfo=et)

    out = _to_utc_iso(ts)

    assert out == "2025-05-30T18:30:00Z"
    assert _DOTNET_ISO_PATTERN.match(out) is not None


def test_to_utc_iso_tz_aware_et_in_winter_uses_est_offset() -> None:
    """ET 14:30 (EST, winter) → UTC 19:30 (+5 offset)."""
    et = ZoneInfo("America/New_York")
    ts = datetime(2025, 1, 15, 14, 30, tzinfo=et)

    out = _to_utc_iso(ts)

    assert out == "2025-01-15T19:30:00Z"
    assert _DOTNET_ISO_PATTERN.match(out) is not None


def test_to_utc_iso_naive_treated_as_utc_for_defensive_compat() -> None:
    """A naive datetime is treated as UTC rather than raising — the
    engine's bar pipeline normally yields tz-aware ET, but a strategy
    that bypasses the standard pipeline must not silently fail the save."""
    naive = datetime(2025, 5, 30, 14, 30)

    out = _to_utc_iso(naive)

    assert out == "2025-05-30T14:30:00Z"
    assert _DOTNET_ISO_PATTERN.match(out) is not None


@pytest.mark.parametrize(
    "input_dt, expected",
    [
        # Already-UTC tz-aware passes through cleanly
        (
            datetime(2025, 1, 13, 9, 30, tzinfo=ZoneInfo("UTC")),
            "2025-01-13T09:30:00Z",
        ),
        # Sub-second component truncated (seconds resolution matches .NET regex)
        (
            datetime(2025, 1, 13, 9, 30, 45, 123456, tzinfo=ZoneInfo("UTC")),
            "2025-01-13T09:30:45Z",
        ),
    ],
)
def test_to_utc_iso_misc_inputs(input_dt: datetime, expected: str) -> None:
    out = _to_utc_iso(input_dt)
    assert out == expected
    assert _DOTNET_ISO_PATTERN.match(out) is not None


def test_format_trade_emits_iso_timestamps_via_to_utc_iso() -> None:
    """End-to-end: _format_trade output is .NET-parsable."""
    from decimal import Decimal

    from app.engine.strategy.base import LoggedTrade
    from app.routers.engine import _format_trade

    et = ZoneInfo("America/New_York")
    trade = LoggedTrade(
        entry_time=datetime(2025, 5, 30, 14, 30, tzinfo=et),
        entry_price=Decimal("100.0"),
        exit_time=datetime(2025, 5, 30, 15, 30, tzinfo=et),
        exit_price=Decimal("101.0"),
        pnl_pts=Decimal("1.0"),
        pnl_pct=Decimal("0.01"),
        result="WIN",
        signal_reason="test",
    )

    formatted = _format_trade(1, trade)

    assert _DOTNET_ISO_PATTERN.match(formatted.entry_time) is not None
    assert _DOTNET_ISO_PATTERN.match(formatted.exit_time) is not None
    assert formatted.entry_time == "2025-05-30T18:30:00Z"
    assert formatted.exit_time == "2025-05-30T19:30:00Z"
