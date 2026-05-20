"""Regression: TrustedRunRequestModel rejects bad session-open windows.

Pins the contract that callers (test fixtures, probe scripts, real HTTP
clients) must construct ``end_ms_utc`` as 09:30 ET of the NEXT trading
day after the last full session, never as ``to_date + 1 calendar day``.
A Friday or holiday-eve to_date plus one day lands on a Saturday or
holiday; without this guard the router would silently stage a window
that the calendar treats as zero trading days.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import next_trading_day, session_open_ms_utc
from app.routers.lean_sidecar import TrustedRunRequestModel


def _common_kwargs() -> dict:
    return {
        "run_id": "test-window-validation",
        "symbol": "SPY",
        "starting_cash": 100_000.0,
        "template": "ema_crossover",
        "data_source": "polygon",
        "bar_minutes": 15,
        "session": "regular",
        "adjustment": "raw",
    }


def test_trusted_run_request_rejects_weekend_exclusive_end() -> None:
    """Fri 2025-01-17 + 1 calendar day = Sat 2025-01-18 (non-trading)."""
    from_date = date(2025, 1, 13)  # Mon
    bad_exclusive_end = date(2025, 1, 18)  # Sat

    with pytest.raises(ValueError, match="not a trading day"):
        TrustedRunRequestModel(
            start_ms_utc=session_open_ms_utc(from_date),
            end_ms_utc=session_open_ms_utc(bad_exclusive_end),
            **_common_kwargs(),
        )


def test_trusted_run_request_accepts_next_trading_day_exclusive_end() -> None:
    """Fri 2025-01-17 + MLK Day (Mon 2025-01-20) → Tue 2025-01-21."""
    from_date = date(2025, 1, 13)
    to_date = date(2025, 1, 17)
    exclusive_end = next_trading_day(to_date)
    assert exclusive_end == date(2025, 1, 21)

    model = TrustedRunRequestModel(
        start_ms_utc=session_open_ms_utc(from_date),
        end_ms_utc=session_open_ms_utc(exclusive_end),
        **_common_kwargs(),
    )

    assert model.start_ms_utc == session_open_ms_utc(from_date)
    assert model.end_ms_utc == session_open_ms_utc(exclusive_end)
