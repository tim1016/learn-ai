"""One run's decision session: what it resolves to, decides on, and flushes at."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import MarketDataBar
from app.services.decision_clock import next_trigger_ms
from app.services.decision_session import RunDecisionSession
from app.utils.timestamps import to_ms_utc

_ET = ZoneInfo("America/New_York")
_TF = 15 * 60_000
_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_REGULAR = date(2026, 9, 2)  # Wednesday, regular session
_EARLY = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close
_SATURDAY = date(2026, 9, 5)


def _et(day: date, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))


def _bar(day: date, hour: int, minute: int, *, phase: str = "CLOSED") -> MarketDataBar:
    start = _et(day, hour, minute)
    one = Decimal("1")
    return MarketDataBar(
        symbol="SPY", start_ms=start, end_ms=start + 60_000, open=one, high=one, low=one,
        close=one, volume=1, fetched_at_ms=start + 60_000, feed_id="ibkr", session_phase=phase,
    )


def test_resolve_returns_the_regular_session_for_an_rth_run() -> None:
    session = RunDecisionSession.resolve(use_rth=True, window=None)

    assert session == RunDecisionSession(kind="rth", window=None)


def test_resolve_ignores_a_declared_window_for_an_rth_run() -> None:
    """An RTH run does not become extended because the authority happens to offer one."""
    session = RunDecisionSession.resolve(use_rth=True, window=_WINDOW)

    assert session is not None
    assert session.kind == "rth"
    assert session.window is None


def test_resolve_returns_the_extended_session_when_a_window_is_declared() -> None:
    session = RunDecisionSession.resolve(use_rth=False, window=_WINDOW)

    assert session == RunDecisionSession(kind="extended", window=_WINDOW)


def test_resolve_returns_none_for_an_extended_run_with_no_declared_window() -> None:
    """The one place "extended run, no window" is judged; every caller refuses on it."""
    assert RunDecisionSession.resolve(use_rth=False, window=None) is None


@pytest.mark.parametrize(
    ("kind", "window"),
    [("extended", None), ("rth", _WINDOW)],
)
def test_an_incoherent_session_cannot_be_constructed(kind: str, window: ExtendedHoursWindow | None) -> None:
    with pytest.raises(ValueError, match="declared window"):
        RunDecisionSession(kind=kind, window=window)  # type: ignore[arg-type]


def test_includes_reads_the_feed_label_for_a_regular_session_run() -> None:
    """Ruling R3: the RTH filter stays label-based, so labelled test bars keep working."""
    session = RunDecisionSession(kind="rth", window=None)

    assert session.includes(_bar(_REGULAR, 3, 0, phase="RTH")) is True
    assert session.includes(_bar(_REGULAR, 12, 0, phase="CLOSED")) is False


@pytest.mark.parametrize(
    ("hour", "minute", "included"),
    [(3, 59, False), (4, 0, True), (9, 30, True), (16, 0, True), (19, 59, True), (20, 0, False)],
)
def test_includes_compares_a_bars_open_against_the_declared_bounds(
    hour: int, minute: int, included: bool
) -> None:
    session = RunDecisionSession(kind="extended", window=_WINDOW)

    assert session.includes(_bar(_REGULAR, hour, minute, phase="CLOSED")) is included


def test_includes_is_false_on_a_non_trading_day() -> None:
    session = RunDecisionSession(kind="extended", window=_WINDOW)

    assert session.includes(_bar(_SATURDAY, 12, 0, phase="CLOSED")) is False


def test_includes_instant_covers_the_whole_declared_window_not_only_rth() -> None:
    """The continuity floor's predicate: a pre-market minute is inside an extended run."""
    extended = RunDecisionSession(kind="extended", window=_WINDOW)
    rth = RunDecisionSession(kind="rth", window=None)
    pre_market = _et(_REGULAR, 5, 0)

    assert extended.includes_instant(pre_market) is True
    assert rth.includes_instant(pre_market) is False
    assert rth.includes_instant(_et(_REGULAR, 10, 0)) is True
    assert extended.includes_instant(_et(_REGULAR, 21, 0)) is False


def test_close_ms_is_the_calendar_close_for_a_regular_session_run() -> None:
    session = RunDecisionSession(kind="rth", window=None)

    assert session.close_ms(_REGULAR) == session_close_ms_utc(_REGULAR)
    assert session.close_ms(_EARLY) == session_close_ms_utc(_EARLY)


def test_close_ms_is_the_declared_close_even_on_an_early_close_day() -> None:
    session = RunDecisionSession(kind="extended", window=_WINDOW)

    assert session.close_ms(_EARLY) == _et(_EARLY, 20, 0)


def test_close_ms_refuses_a_day_that_is_not_a_session() -> None:
    session = RunDecisionSession(kind="extended", window=_WINDOW)

    with pytest.raises(ValueError, match="not a trading day"):
        session.close_ms(_SATURDAY)


def test_next_trigger_function_delegates_to_the_clock_for_each_session() -> None:
    rth = RunDecisionSession(kind="rth", window=None).next_trigger_function(_TF)
    extended = RunDecisionSession(kind="extended", window=_WINDOW).next_trigger_function(_TF)
    at_1700 = _et(_REGULAR, 17, 0)

    assert rth(at_1700) == next_trigger_ms(at_1700, timeframe_ms=_TF)
    assert extended(at_1700) == next_trigger_ms(at_1700, timeframe_ms=_TF, window=_WINDOW)
    # 17:00 is outside the regular session but a live extended bucket boundary,
    # so the two clocks must disagree here or this would pass unbound.
    assert rth(at_1700) != extended(at_1700)
