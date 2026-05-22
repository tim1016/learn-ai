"""Regression test for Bug B (DECISION_MISMATCH): session filter at the reader.

Before this fix, ``LeanMinuteDataReader`` honored only the data root and
returned every minute bar in the zip — including extended-hours bars when
the Polygon-sourced cache included them. Strategies that did not filter at
the consolidator therefore saw 04:00-20:00 ET data while the LEAN sidecar
(``AddEquity(..., extendedMarketHours=False)``) saw only 09:30-16:00 ET,
producing entirely different trade plans for the same ``DataPolicy`` value.

See ``.claude/rules/numerical-rigor.md`` → ``DECISION_MISMATCH`` and the
divergence trace at ``StrategyExecutions`` rows 41/42 (run on 2026-05-21).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from app.engine.data.lean_format import LeanMinuteDataReader, write_lean_day_zip
from app.engine.data.trade_bar import TradeBar

_ET = ZoneInfo("America/New_York")


def _bar(symbol: str, when: datetime) -> TradeBar:
    """Build a trivial 1-minute TradeBar at the supplied ET wall-clock time."""
    return TradeBar(
        symbol=symbol,
        time=when,
        end_time=when + timedelta(minutes=1),
        open=Decimal("100"),
        high=Decimal("100.5"),
        low=Decimal("99.5"),
        close=Decimal("100"),
        volume=1000,
    )


def _seed_day(root: Path, symbol: str, trading_day: date) -> None:
    """Stage one day of minute bars spanning 04:00-19:59 ET (premarket through after-hours).

    16 hours × 60 = 960 bars: 06:30 of premarket (04:00-09:30), 06:30 of
    regular hours (09:30-16:00), 04:00 of after-hours (16:00-20:00).
    """
    bars: list[TradeBar] = []
    start = datetime(trading_day.year, trading_day.month, trading_day.day, 4, 0, tzinfo=_ET)
    for i in range(16 * 60):  # 04:00-20:00 ET, one bar per minute
        bars.append(_bar(symbol, start + timedelta(minutes=i)))
    write_lean_day_zip(root, symbol, trading_day, bars)


def test_reader_default_session_drops_extended_hours(tmp_path: Path) -> None:
    _seed_day(tmp_path, "SPY", date(2025, 1, 6))  # Monday — full session

    reader = LeanMinuteDataReader(tmp_path)
    bars = reader.read_day("SPY", date(2025, 1, 6))

    # 09:30-16:00 ET, one bar per minute, half-open on the close side ⇒ 6h30m = 390 bars
    assert len(bars) == 390
    first = bars[0].time.astimezone(_ET)
    last = bars[-1].time.astimezone(_ET)
    assert (first.hour, first.minute) == (9, 30), f"first bar at {first}"
    assert (last.hour, last.minute) == (15, 59), f"last bar at {last}"


def test_reader_extended_session_keeps_premarket_and_after_hours(tmp_path: Path) -> None:
    _seed_day(tmp_path, "SPY", date(2025, 1, 6))

    reader = LeanMinuteDataReader(tmp_path, session="extended")
    bars = reader.read_day("SPY", date(2025, 1, 6))

    # 04:00-20:00 ET = 16h × 60 = 960 bars; nothing filtered out.
    assert len(bars) == 960
    first = bars[0].time.astimezone(_ET)
    last = bars[-1].time.astimezone(_ET)
    assert (first.hour, first.minute) == (4, 0)
    assert (last.hour, last.minute) == (19, 59)


def test_reader_half_day_respects_early_close(tmp_path: Path) -> None:
    """Black-Friday-style early close: NYSE closes at 13:00 ET (780 minutes).

    Confirms the filter uses ``session_close_minute_et`` from the
    trading-calendar helper, not a hard-coded 16:00 ET.
    """
    # 2024-11-29 (Black Friday) is a known half-day close at 13:00 ET.
    half_day = date(2024, 11, 29)
    _seed_day(tmp_path, "SPY", half_day)

    reader = LeanMinuteDataReader(tmp_path)
    bars = reader.read_day("SPY", half_day)

    # 09:30-13:00 ET, half-open ⇒ 3h30m = 210 bars
    assert len(bars) == 210
    last = bars[-1].time.astimezone(_ET)
    assert (last.hour, last.minute) == (12, 59), f"last bar at {last}"
