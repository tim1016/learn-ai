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
