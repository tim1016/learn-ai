"""Regression test: SpyOpeningRangeBreakout fires at most one entry per day.

Background — this test exists specifically to prevent a re-regression of the
bug that surfaced during the QQQ ORB validation study (2026-04-18). Prior to
the fix, the Python ORB implementation had no per-day trade limit, so after
the 5-bar hold exit the strategy would immediately re-enter on the next bar
whose close > orb_high. That produced ~3–4× the trade count of the Pine
Script reference (EL 417 trades vs TV 137 for QQQ over one year).

The fix added a ``_traded_today`` instance flag mirroring the Pine Script's
``tradedToday``. This test exercises the same day-in-one-go scenario that
triggered the bug:

    * Feed a full RTH session of synthetic 15-min bars.
    * Bars 1–3 form a valid opening range with a tight spread.
    * Every remaining bar of the day closes strictly above the ORB high,
      which without the guard would fire many entries.

A correct implementation:
    * Enters exactly once (on bar 4).
    * Exits on bar 9 (5 bars later).
    * Does NOT re-enter on bars 10–26 even though close > orb_high.

Run with::

    cd PythonDataService
    python -m app.engine.tests.test_spy_orb_one_trade_per_day
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.algorithms.spy_orb import SpyOpeningRangeBreakout


# ---------------------------------------------------------------------------
# Minimal in-memory context stub — records every ctx call the strategy makes
# so the test can assert exactly what happened.
# ---------------------------------------------------------------------------
@dataclass
class _CtxStub:
    holdings_calls: list[tuple[str, Decimal]] = field(default_factory=list)
    liquidate_calls: list[str] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    def add_equity(self, symbol: str) -> str:
        return symbol

    def register_consolidator(self, *args: Any, **kwargs: Any) -> None:
        # No-op; the test drives the bar handler directly.
        pass

    def set_holdings(self, symbol: str, target: Decimal) -> None:
        self.holdings_calls.append((symbol, target))

    def liquidate(self, symbol: str) -> None:
        self.liquidate_calls.append(symbol)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def emit_insight(self, insight: Any) -> None:
        # The test doesn't inspect insights; just swallow them.
        pass


def _bar(when: datetime, open_: float, high: float, low: float, close: float) -> TradeBar:
    """Build a 15-minute TradeBar with end_time = when."""
    start = when - timedelta(minutes=15)
    return TradeBar(
        symbol="TEST",
        time=start,
        end_time=when,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=1000,
    )


def _rth_15min_bars_for(day: date) -> list[datetime]:
    """Return the 26 end_time timestamps for a full RTH 15-min session.

    9:45 (end of 9:30-9:45 bar) through 16:00 (end of 15:45-16:00 bar) = 26 bars.
    """
    out = []
    end = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=45)
    final = datetime.combine(day, datetime.min.time()).replace(hour=16, minute=0)
    while end <= final:
        out.append(end)
        end = end + timedelta(minutes=15)
    return out[:26]


def _run_one_day_scenario() -> tuple[_CtxStub, SpyOpeningRangeBreakout]:
    """Drive one RTH session through the strategy and return the recorded calls.

    Price shape — picked to force the bug-triggering scenario:

        Bars 1-3 (ORB formation):
            highs = 100.50, 100.70, 100.80  → orb_high = 100.80
            lows  = 100.30, 100.20, 100.40  → orb_low  = 100.20
            range_pct = (100.80 - 100.20) / 100.20 * 100 = 0.599%  (valid, in [0.30, 1.50])

        Bars 4-26 (post-ORB):
            Every bar closes strictly above orb_high (100.80), so without
            the `_traded_today` guard the strategy would re-enter after
            every exit.
    """
    ctx = _CtxStub()
    strat = SpyOpeningRangeBreakout(
        symbol="TEST",
        orb_bars=3,
        hold_bars=5,
        min_range_pct=0.30,
        max_range_pct=1.50,
    )
    # Bypass initialize() (it would try to install a consolidator); wire the
    # ctx and symbol manually.
    strat.ctx = ctx  # type: ignore[assignment]
    strat._symbol = "TEST"

    bar_times = _rth_15min_bars_for(date(2026, 1, 5))

    # Bars 1-3: opening range formation (highs/lows chosen for 0.60% range).
    orb_bars = [
        _bar(bar_times[0], 100.40, 100.50, 100.30, 100.45),
        _bar(bar_times[1], 100.45, 100.70, 100.20, 100.60),
        _bar(bar_times[2], 100.60, 100.80, 100.40, 100.75),
    ]
    for b in orb_bars:
        strat._on_fifteen_minute_bar(b)

    # Bars 4-26: every close strictly above orb_high = 100.80
    for i, t in enumerate(bar_times[3:], start=4):
        # Oscillate slightly so not every bar is identical, but always close > 100.80
        close_px = 101.00 + (i % 3) * 0.05  # 101.00, 101.05, 101.10, ...
        strat._on_fifteen_minute_bar(_bar(t, 100.90, close_px + 0.10, 100.85, close_px))

    return ctx, strat


def test_one_trade_per_day() -> None:
    ctx, strat = _run_one_day_scenario()

    # Exactly one entry (set_holdings target=1) and one exit (liquidate).
    assert len(ctx.holdings_calls) == 1, (
        f"Expected exactly 1 entry per day, got {len(ctx.holdings_calls)}.  Entries: {ctx.holdings_calls}"
    )
    assert len(ctx.liquidate_calls) == 1, (
        f"Expected exactly 1 exit per day, got {len(ctx.liquidate_calls)}.  Exits: {ctx.liquidate_calls}"
    )
    assert ctx.holdings_calls[0] == ("TEST", Decimal(1))

    # ORB state should show orb_valid, orb_complete, and traded_today all set.
    assert strat._orb_complete is True
    assert strat._orb_valid is True
    assert strat._traded_today is True


def test_new_day_resets_traded_today() -> None:
    """After end-of-day rollover the flag must reset so the next day trades."""
    _ctx, strat = _run_one_day_scenario()
    assert strat._traded_today is True

    # Simulate the first bar of the NEXT trading day.
    next_day_bar = _bar(
        datetime(2026, 1, 6, 9, 45),
        open_=101.00,
        high=101.20,
        low=100.90,
        close=101.10,
    )
    strat._on_fifteen_minute_bar(next_day_bar)

    # _reset_day should have cleared the flag.
    assert strat._traded_today is False, (
        "traded_today must reset on a new trading day, otherwise the strategy "
        "would only trade on the first day of the backtest."
    )
    assert strat._bar_of_day == 1  # fresh day


if __name__ == "__main__":
    try:
        test_one_trade_per_day()
        print("PASS: one entry and one exit per day")
        test_new_day_resets_traded_today()
        print("PASS: traded_today resets at new-day boundary")
        print("\nALL TESTS PASSED")
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
