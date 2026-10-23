"""The warmup window counts regular sessions, as IBKR's ``N D`` does (#2849).

The owner's read-only probe (SPY 1-minute TRADES, extended hours, 2026-10-03)
settled it: ``5 D`` ending Monday 2026-09-28 09:31 ET returned five sessions,
Tuesday 22 to Friday 25 in full and Monday 28 up to 09:30. So a lookback of
four completed sessions is what a ``5 D`` request returns beside the session
in progress. These tests hold the window to the canonical calendar's sessions
across a weekend, a holiday and an early close.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.marketdata.feed import MAX_WARMUP_LOOKBACK_SESSIONS, warmup_sessions_reaching, warmup_window_start_ms

_MINUTE_MS = 60_000
_HOUR_MS = 3_600_000


def _open_of(day: date) -> int:
    return session_open_ms_utc(day)


def _just_after_the_open(day: date) -> int:
    """09:31 ET on a session date: the instant the probe's requests ended at."""
    return session_open_ms_utc(day) + _MINUTE_MS


@pytest.mark.parametrize(
    ("now_ms", "lookback_sessions", "first_session"),
    [
        # The probe: ``5 D`` at Monday 09:31 returned Tue 22, Wed 23, Thu 24 and
        # Fri 25 in full, and Monday 28 to 09:30 -- four completed sessions and
        # the one running.
        pytest.param(_just_after_the_open(date(2026, 9, 28)), 4, date(2026, 9, 22), id="probe-monday-across-a-weekend"),
        # The probe: ``5 D`` at Thursday 09:31 returned Fri 25, Mon 28, Tue 29
        # and Wed 30 in full, and Thursday 1 to 09:30.
        pytest.param(_just_after_the_open(date(2026, 10, 1)), 4, date(2026, 9, 25), id="probe-thursday-over-a-weekend"),
        # Calendar days back from Monday would reach Thursday; sessions reach Friday.
        pytest.param(_just_after_the_open(date(2026, 9, 28)), 1, date(2026, 9, 25), id="monday-one-session-is-friday"),
        # Labor Day Monday 2026-09-07 is no session: Tuesday's one session back is Friday's,
        # and three back are Friday, Thursday and Wednesday.
        pytest.param(_just_after_the_open(date(2026, 9, 8)), 1, date(2026, 9, 4), id="tuesday-after-a-holiday-weekend"),
        pytest.param(_just_after_the_open(date(2026, 9, 8)), 3, date(2026, 9, 2), id="three-sessions-across-a-holiday-weekend"),
        # Thanksgiving Thursday 2026-11-26 is no session and the Friday after is an early close,
        # which still counts as one: two sessions back from Monday are that Friday and Wednesday.
        pytest.param(
            _just_after_the_open(date(2026, 11, 30)), 2, date(2026, 11, 25), id="two-sessions-across-thanksgiving-and-an-early-close"
        ),
        # After a close the session is complete: Friday 2026-09-25 at 16:00 ET is the first of one session back.
        pytest.param(session_close_ms_utc(date(2026, 9, 25)), 1, date(2026, 9, 25), id="at-the-close-the-session-is-completed"),
        pytest.param(
            session_close_ms_utc(date(2026, 9, 25)) - _MINUTE_MS, 1, date(2026, 9, 24), id="a-minute-before-the-close-it-is-not"
        ),
        # A weekend: the sessions are the last ones the week held.
        pytest.param(session_close_ms_utc(date(2026, 9, 25)) + 30 * _HOUR_MS, 2, date(2026, 9, 24), id="saturday-two-sessions"),
    ],
)
def test_the_window_opens_at_the_open_of_the_nth_most_recent_completed_session(
    now_ms: int, lookback_sessions: int, first_session: date
) -> None:
    assert warmup_window_start_ms(lookback_sessions, now_ms=now_ms) == _open_of(first_session)


def test_a_lookback_of_no_session_has_no_window() -> None:
    with pytest.raises(ValueError, match="positive"):
        warmup_window_start_ms(0, now_ms=_just_after_the_open(date(2026, 9, 28)))


@pytest.mark.parametrize(
    ("since_ms", "sessions"),
    [
        # Monday 09:31: the tail is in the running session, or in the last completed one.
        pytest.param(_just_after_the_open(date(2026, 9, 28)) - _MINUTE_MS, 1, id="the-tail-is-the-running-session-s-open"),
        pytest.param(session_close_ms_utc(date(2026, 9, 25)) - _MINUTE_MS, 1, id="the-tail-is-in-the-last-completed-session"),
        pytest.param(session_open_ms_utc(date(2026, 9, 25)), 1, id="the-tail-is-the-last-completed-session-s-open"),
        # One minute before that open, the window needs the session before it.
        pytest.param(session_open_ms_utc(date(2026, 9, 25)) - _MINUTE_MS, 2, id="the-tail-is-just-before-the-last-session-s-open"),
        pytest.param(session_open_ms_utc(date(2026, 9, 22)), 4, id="the-tail-is-four-sessions-back"),
    ],
)
def test_the_fewest_sessions_reaching_a_tail_are_the_ones_whose_window_starts_before_it(
    since_ms: int, sessions: int
) -> None:
    now_ms = _just_after_the_open(date(2026, 9, 28))

    assert warmup_sessions_reaching(since_ms, now_ms=now_ms, at_most=MAX_WARMUP_LOOKBACK_SESSIONS) == sessions
    assert warmup_window_start_ms(sessions, now_ms=now_ms) <= since_ms
    if sessions > 1:
        assert warmup_window_start_ms(sessions - 1, now_ms=now_ms) > since_ms


def test_a_tail_the_window_does_not_reach_is_refused() -> None:
    now_ms = _just_after_the_open(date(2026, 9, 28))

    with pytest.raises(ValueError, match="no window of at most 3 sessions"):
        warmup_sessions_reaching(session_open_ms_utc(date(2026, 9, 22)), now_ms=now_ms, at_most=3)


def _reaching_by_the_window_definition(since_ms: int, now_ms: int, at_most: int) -> int | None:
    """The fewest sessions whose ``warmup_window_start_ms`` is at or before ``since_ms``, or ``None``."""
    for sessions in range(1, at_most + 1):
        if warmup_window_start_ms(sessions, now_ms=now_ms) <= since_ms:
            return sessions
    return None


_THURSDAY_MIDDAY = session_open_ms_utc(date(2026, 9, 24)) + 3 * _HOUR_MS + 30_000


@pytest.mark.parametrize(
    ("since_ms", "now_ms"),
    [
        # Mid-session: the tail and now in the same running session, then the day before.
        pytest.param(session_open_ms_utc(date(2026, 9, 24)) + 37 * _MINUTE_MS, _THURSDAY_MIDDAY, id="mid-session"),
        pytest.param(session_open_ms_utc(date(2026, 9, 23)) + 30 * _MINUTE_MS, _THURSDAY_MIDDAY, id="the-day-before"),
        # A weekend between them, with the tail in a session, after its close and over the weekend.
        pytest.param(session_open_ms_utc(date(2026, 9, 25)) + _HOUR_MS, _just_after_the_open(date(2026, 9, 28)), id="weekend-from-friday-session"),
        pytest.param(session_close_ms_utc(date(2026, 9, 25)) + _HOUR_MS, _just_after_the_open(date(2026, 9, 28)), id="weekend-from-friday-after-hours"),
        pytest.param(session_close_ms_utc(date(2026, 9, 25)) + 30 * _HOUR_MS, _just_after_the_open(date(2026, 9, 28)), id="weekend-from-saturday"),
        pytest.param(session_open_ms_utc(date(2026, 9, 22)) + _HOUR_MS, _just_after_the_open(date(2026, 9, 28)), id="weekend-from-tuesday"),
        # Labor Day Monday 2026-09-07 is no session.
        pytest.param(session_open_ms_utc(date(2026, 9, 3)) + _HOUR_MS, session_open_ms_utc(date(2026, 9, 8)) - _HOUR_MS, id="holiday-from-thursday"),
        pytest.param(session_close_ms_utc(date(2026, 9, 4)), session_open_ms_utc(date(2026, 9, 8)) - _HOUR_MS, id="holiday-from-friday-close"),
        pytest.param(session_open_ms_utc(date(2026, 9, 7)) + 3 * _HOUR_MS, session_open_ms_utc(date(2026, 9, 8)) - _HOUR_MS, id="holiday-from-the-holiday"),
        # The day after Thanksgiving closes at 13:00: a tail before, at and after its close.
        pytest.param(session_close_ms_utc(date(2026, 11, 27)) - _MINUTE_MS, _just_after_the_open(date(2026, 11, 30)), id="early-close-last-minute"),
        pytest.param(session_close_ms_utc(date(2026, 11, 27)), _just_after_the_open(date(2026, 11, 30)), id="early-close-at-the-close"),
        pytest.param(session_close_ms_utc(date(2026, 11, 27)) + _HOUR_MS, _just_after_the_open(date(2026, 11, 30)), id="early-close-after-it"),
        pytest.param(session_close_ms_utc(date(2026, 11, 25)) - _MINUTE_MS, _just_after_the_open(date(2026, 11, 30)), id="across-thanksgiving"),
        # Exactly on a session's open and on the open it counts from.
        pytest.param(session_open_ms_utc(date(2026, 9, 25)), _just_after_the_open(date(2026, 9, 28)), id="tail-on-an-open"),
        # A tail the window has not left behind: at or after now.
        pytest.param(_just_after_the_open(date(2026, 9, 28)), _just_after_the_open(date(2026, 9, 28)), id="tail-is-now"),
        pytest.param(_just_after_the_open(date(2026, 9, 28)) + _HOUR_MS, _just_after_the_open(date(2026, 9, 28)), id="tail-after-now"),
        # More sessions back than the cap, so it is refused.
        pytest.param(session_open_ms_utc(date(2026, 8, 3)), _just_after_the_open(date(2026, 9, 28)), id="beyond-the-cap"),
    ],
)
def test_the_sessions_reaching_a_tail_are_those_whose_window_the_definition_starts_before_it(
    since_ms: int, now_ms: int
) -> None:
    """One pass over the calendar agrees with asking ``warmup_window_start_ms`` for each lookback in turn."""
    at_most = MAX_WARMUP_LOOKBACK_SESSIONS
    expected = _reaching_by_the_window_definition(since_ms, now_ms, at_most)

    if expected is None:
        with pytest.raises(ValueError, match=f"no window of at most {at_most} sessions"):
            warmup_sessions_reaching(since_ms, now_ms=now_ms, at_most=at_most)
    else:
        assert warmup_sessions_reaching(since_ms, now_ms=now_ms, at_most=at_most) == expected
