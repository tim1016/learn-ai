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
_RTH_SESSION = RunDecisionSession(kind="rth", window=None)
_EXTENDED_SESSION = RunDecisionSession(kind="extended", window=_WINDOW)


def _et(day: date, hour: int, minute: int) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))


def _bar(day: date, hour: int, minute: int, *, phase: str = "CLOSED") -> MarketDataBar:
    start = _et(day, hour, minute)
    one = Decimal("1")
    return MarketDataBar(
        symbol="SPY", start_ms=start, end_ms=start + 60_000, open=one, high=one, low=one,
        close=one, volume=1, fetched_at_ms=start + 60_000, feed_id="ibkr", session_phase=phase,
    )


@pytest.mark.parametrize(
    ("use_rth", "window", "expected"),
    [
        pytest.param(True, None, _RTH_SESSION, id="rth-run-resolves-the-regular-session"),
        # An RTH run does not become extended because the authority happens to offer one.
        pytest.param(True, _WINDOW, _RTH_SESSION, id="rth-run-ignores-a-declared-window"),
        pytest.param(False, _WINDOW, _EXTENDED_SESSION, id="extended-run-resolves-the-extended-session-when-a-window-is-declared"),
        # The one place "extended run, no window" is judged; every caller refuses on it.
        pytest.param(False, None, None, id="extended-run-with-no-declared-window-resolves-to-none"),
    ],
)
def test_resolve_returns_the_session_the_run_decides_on(
    use_rth: bool, window: ExtendedHoursWindow | None, expected: RunDecisionSession | None
) -> None:
    session = RunDecisionSession.resolve(use_rth=use_rth, window=window)

    assert (session is None) == (expected is None), "a session resolves exactly when the run has one to decide on"
    assert session == expected, "the run resolves to the session it decides on"


@pytest.mark.parametrize(
    ("kind", "window"),
    [("extended", None), ("rth", _WINDOW)],
)
def test_an_incoherent_session_cannot_be_constructed(kind: str, window: ExtendedHoursWindow | None) -> None:
    with pytest.raises(ValueError, match="declared window"):
        RunDecisionSession(kind=kind, window=window)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("session", "day", "hour", "minute", "phase", "included"),
    [
        # Ruling R3: the RTH filter stays label-based, so labelled test bars keep working.
        pytest.param(_RTH_SESSION, _REGULAR, 3, 0, "RTH", True, id="rth-run-keeps-a-bar-the-feed-labels-rth"),
        pytest.param(_RTH_SESSION, _REGULAR, 12, 0, "CLOSED", False, id="rth-run-drops-a-bar-the-feed-labels-closed"),
        pytest.param(_EXTENDED_SESSION, _REGULAR, 3, 59, "CLOSED", False, id="extended-run-drops-the-bar-opening-at-03-59"),
        pytest.param(_EXTENDED_SESSION, _REGULAR, 4, 0, "CLOSED", True, id="extended-run-keeps-the-bar-opening-at-04-00"),
        pytest.param(_EXTENDED_SESSION, _REGULAR, 9, 30, "CLOSED", True, id="extended-run-keeps-the-bar-opening-at-09-30"),
        pytest.param(_EXTENDED_SESSION, _REGULAR, 16, 0, "CLOSED", True, id="extended-run-keeps-the-bar-opening-at-16-00"),
        pytest.param(_EXTENDED_SESSION, _REGULAR, 19, 59, "CLOSED", True, id="extended-run-keeps-the-bar-opening-at-19-59"),
        pytest.param(_EXTENDED_SESSION, _REGULAR, 20, 0, "CLOSED", False, id="extended-run-drops-the-bar-opening-at-20-00"),
        pytest.param(_EXTENDED_SESSION, _SATURDAY, 12, 0, "CLOSED", False, id="extended-run-drops-a-bar-on-a-non-trading-day"),
    ],
)
def test_includes_decides_on_a_bar_by_the_runs_session(
    session: RunDecisionSession, day: date, hour: int, minute: int, phase: str, included: bool
) -> None:
    assert session.includes(_bar(day, hour, minute, phase=phase)) is included, (
        "the run decides on the bar exactly when it is inside the run's session"
    )


def test_includes_instant_covers_the_whole_declared_window_not_only_rth() -> None:
    """The continuity floor's predicate: a pre-market minute is inside an extended run."""
    extended = RunDecisionSession(kind="extended", window=_WINDOW)
    rth = RunDecisionSession(kind="rth", window=None)
    pre_market = _et(_REGULAR, 5, 0)

    assert extended.includes_instant(pre_market) is True
    assert rth.includes_instant(pre_market) is False
    assert rth.includes_instant(_et(_REGULAR, 10, 0)) is True
    assert extended.includes_instant(_et(_REGULAR, 21, 0)) is False


@pytest.mark.parametrize(
    ("session", "day", "expected"),
    [
        pytest.param(_RTH_SESSION, _REGULAR, session_close_ms_utc(_REGULAR), id="regular-session-run-closes-at-the-calendar-close"),
        pytest.param(_RTH_SESSION, _EARLY, session_close_ms_utc(_EARLY), id="regular-session-run-closes-at-the-calendar-early-close"),
        pytest.param(_EXTENDED_SESSION, _EARLY, _et(_EARLY, 20, 0), id="extended-run-closes-at-the-declared-close-even-on-an-early-close-day"),
    ],
)
def test_close_ms_is_the_close_of_the_runs_session(session: RunDecisionSession, day: date, expected: int) -> None:
    assert session.close_ms(day) == expected, "the run flushes at its own session's close"


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
