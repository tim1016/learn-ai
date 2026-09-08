"""An extended binding decides only on bars inside the declared window; an RTH binding is unchanged."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.marketdata.feed import MarketDataBar
from app.services.decision_session import RunDecisionSession
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_DAY = date(2026, 9, 2)
_EXTENDED = RunDecisionSession(kind="extended", window=_WINDOW)
_RTH = RunDecisionSession(kind="rth", window=None)


def _bar(hour: int, minute: int, *, phase: str = "CLOSED") -> MarketDataBar:
    start = to_ms_utc(datetime(_DAY.year, _DAY.month, _DAY.day, hour, minute, tzinfo=_ET))
    one = Decimal("1")
    return MarketDataBar(
        symbol="SPY", start_ms=start, end_ms=start + 60_000, open=one, high=one, low=one, close=one,
        volume=1, fetched_at_ms=start + 60_000, feed_id="ibkr", session_phase=phase,
    )


@pytest.mark.parametrize(
    ("hour", "minute", "included"),
    [(3, 59, False), (4, 0, True), (9, 29, True), (9, 30, True), (15, 59, True), (16, 0, True), (19, 59, True), (20, 0, False)],
)
def test_extended_binding_decides_inside_the_declared_window_only(hour: int, minute: int, included: bool) -> None:
    assert _EXTENDED.includes(_bar(hour, minute)) is included


def test_an_extended_session_cannot_exist_without_a_window() -> None:
    """The state the old ``extended_window=None`` filter answered by deciding on nothing."""
    assert RunDecisionSession.resolve(use_rth=False, window=None) is None
    with pytest.raises(ValueError, match="declared window"):
        RunDecisionSession(kind="extended", window=None)


def test_rth_binding_still_trusts_the_feed_label() -> None:
    # Unchanged behaviour: RTH filtering is by the feed's label, so labelled test bars keep working.
    assert _RTH.includes(_bar(3, 0, phase="RTH")) is True
    assert _RTH.includes(_bar(12, 0, phase="CLOSED")) is False


def test_a_non_trading_day_is_outside_the_declared_window() -> None:
    saturday = date(2026, 9, 5)
    start = to_ms_utc(datetime(saturday.year, saturday.month, saturday.day, 12, 0, tzinfo=_ET))
    one = Decimal("1")
    bar = MarketDataBar(
        symbol="SPY", start_ms=start, end_ms=start + 60_000, open=one, high=one, low=one, close=one,
        volume=1, fetched_at_ms=start + 60_000, feed_id="ibkr", session_phase="CLOSED",
    )

    assert _EXTENDED.includes(bar) is False
