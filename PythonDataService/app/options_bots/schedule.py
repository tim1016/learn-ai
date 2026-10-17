"""Calendar-owned eligibility for a newly deployed daily Options bot (ADR 0079).

Formula: first session whose setup window fits and entry time is not before deployment.
Reference: ADR 0079, daily repetition and late deployment decisions.
Canonical implementation: this file, using the canonical calendar and setup window.
Validated against: tests/options_bots/test_schedule.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.lean_sidecar.trading_calendar import is_trading_day, session_window_for_date
from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.session_anchors import et_date_at_ms


@dataclass(frozen=True)
class OptionsSessionWindow:
    open_ms: int
    close_ms: int
    entry_ms: int
    exit_ms: int


def first_eligible_session(setup: OptionsBacktestSetup, *, at_ms: int) -> OptionsSessionWindow:
    """Use exchange sessions and half-day fit; never catch up a new late deployment."""
    day = et_date_at_ms(at_ms)
    for offset in range(370):
        candidate = day + timedelta(days=offset)
        if not is_trading_day(candidate):
            continue
        session = session_window_for_date(candidate)
        opened, closed = session.open_ms_utc, session.close_ms_utc
        if not setup.window_fits(session_open_ms=opened, session_close_ms=closed):
            continue
        entry = setup.entry.at(session_open_ms=opened, session_close_ms=closed)
        if entry < at_ms:
            continue
        return OptionsSessionWindow(
            opened, closed, entry, setup.exit.at(session_open_ms=opened, session_close_ms=closed),
        )
    raise ValueError("This Options setup has no eligible session within the next year.")
