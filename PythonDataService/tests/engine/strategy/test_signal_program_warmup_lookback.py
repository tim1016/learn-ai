"""A seal's warmup lookback holds the history its own periods need (#2841, #2849).

``SignalProgramContract.resolved_warmup_lookback_sessions`` turns the longest
series a deploy builds into completed sessions of lookback. The rule is a fixed
worst-case count, so a seal reads the same on any day. These tests hold it to
the canonical NYSE calendar: on every minute of three and a half years of
possible starts, the window those sessions open holds at least the decision
bars the longest series needs.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from functools import cache

import numpy as np
import pytest

from app.engine.strategy.params import decision_timeframe_ms_for
from app.engine.strategy.registry import (
    _STRATEGY_REGISTRY,
    _regular_session_ms,
    bars_per_regular_session,
    warmup_sessions_holding,
)
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.marketdata.feed import MAX_WARMUP_LOOKBACK_SESSIONS, warmup_window_start_ms
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import ny_datetime
from tests._helpers.signal_program import LONG_PERIODS, OFF_VALIDATED_PERIODS

_MINUTE_MS = 60_000
_FIFTEEN_MINUTES_MS = 15 * _MINUTE_MS

# Every start in these three and a half years is swept. They hold each stretch
# the conversion must survive: four Thanksgiving weeks (2027's is the one
# nearest a clock change), Christmas on a Wednesday, a Thursday and a Friday
# (the Friday leaves an early close, a holiday and a weekend in a row), the
# 2025-01-09 day of mourning a week after New Year, every three-day weekend,
# and both clock changes.
_SWEEP_FIRST_DATE = date(2024, 7, 1)
_SWEEP_LAST_DATE = date(2027, 12, 31)
# Calendar days before the sweep that its earliest starts look back on; room
# for the longest lookback and every closure inside it.
_LOOKBACK_MARGIN_DAYS = 90

_BEYOND_THE_DEFAULT_LOOKBACK = [
    *[pytest.param(key, periods, id=f"{key}-long") for key, periods in sorted(LONG_PERIODS.items())],
    # Default periods on hourly bars: here the cadence, not a period, is what
    # the default lookback cannot hold.
    pytest.param("sma_crossover", {"resolution_minutes": 60}, id="sma_crossover-hourly"),
]
_POINTS = [
    *[pytest.param(key, {}, id=f"{key}-validated") for key in sorted(OFF_VALIDATED_PERIODS)],
    *[pytest.param(key, periods, id=f"{key}-off_validated") for key, periods in sorted(OFF_VALIDATED_PERIODS.items())],
    *_BEYOND_THE_DEFAULT_LOOKBACK,
]


@cache
def _starts_ms() -> np.ndarray:
    """Every minute of the sweep, as ``int64 ms UTC``: each instant a run could start at."""
    return np.arange(
        et_midnight_ms(_SWEEP_FIRST_DATE), et_midnight_ms(_SWEEP_LAST_DATE), _MINUTE_MS, dtype=np.int64
    )


@cache
def _sessions_ms() -> tuple[np.ndarray, np.ndarray]:
    """Every session the sweep can look back on: (opens, closes), ascending."""
    windows = session_windows_ms_utc(_SWEEP_FIRST_DATE - timedelta(days=_LOOKBACK_MARGIN_DAYS), _SWEEP_LAST_DATE)
    return (
        np.array([window.open_ms_utc for window in windows], dtype=np.int64),
        np.array([window.close_ms_utc for window in windows], dtype=np.int64),
    )


@cache
def _regular_hours_decision_bars(decision_ms: int) -> tuple[np.ndarray, np.ndarray]:
    """Every regular-hours decision bar the sweep can look back on: (first minute, close), ascending.

    One bar closes on each ``decision_ms`` boundary of the New York wall clock
    inside a session, which is where the runner's consolidator closes it. A
    bar that straddles the open holds only that session's minutes.
    """
    first_minutes: list[int] = []
    closes: list[int] = []
    first_session = _SWEEP_FIRST_DATE - timedelta(days=_LOOKBACK_MARGIN_DAYS)
    for window in session_windows_ms_utc(first_session, _SWEEP_LAST_DATE):
        offset = ny_datetime(window.open_ms_utc).utcoffset()
        assert offset is not None
        wall_clock_ms = int(offset.total_seconds() * 1000)
        close_ms = ((window.open_ms_utc + wall_clock_ms) // decision_ms + 1) * decision_ms - wall_clock_ms
        while close_ms <= window.close_ms_utc:
            first_minutes.append(max(close_ms - decision_ms, window.open_ms_utc))
            closes.append(close_ms)
            close_ms += decision_ms
    return np.array(first_minutes, dtype=np.int64), np.array(closes, dtype=np.int64)


def _window_starts_ms(lookback_sessions: int, starts: np.ndarray) -> np.ndarray:
    """The open of the ``lookback_sessions``-th most recent session completed at each start.

    A session is completed at the instant it closes.
    """
    opens, closes = _sessions_ms()
    completed = np.searchsorted(closes, starts, side="right")
    assert int(completed.min()) >= lookback_sessions
    return opens[completed - lookback_sessions]


def _fewest_whole_decision_bars(lookback_sessions: int, decision_ms: int) -> int:
    """The fewest whole regular-hours decision bars a ``lookback_sessions`` window holds, over every start in the sweep.

    A bar counts when the window holds all of its minutes: its first minute
    is not before the window's start and it has closed by the start instant.
    """
    first_minutes, closes = _regular_hours_decision_bars(decision_ms)
    starts = _starts_ms()
    closed_by_start = np.searchsorted(closes, starts, side="right")
    cut_or_before_window = np.searchsorted(first_minutes, _window_starts_ms(lookback_sessions, starts), side="left")
    return int((closed_by_start - cut_or_before_window).min())


def _longest_warmup(program_key: str, overrides: dict[str, int]) -> tuple[int, int, int, int]:
    """``(bars the longest series needs, decision bar ms, resolved lookback sessions, default lookback sessions)``."""
    registration = _STRATEGY_REGISTRY[program_key]
    contract = registration.signal_program_contract
    assert contract is not None
    params = registration.param_schema(**{**contract.validated_settings, **overrides})
    bars = max(series.bars_to_converge or 0 for series in contract.resolved_signals(params))
    decision_ms = decision_timeframe_ms_for(params, qualified_ms=contract.decision_timeframe_ms)
    return (
        bars,
        decision_ms,
        contract.resolved_warmup_lookback_sessions(params),
        contract.resolved_warmup_lookback_sessions(registration.param_schema()),
    )


def test_the_window_the_sweep_counts_bars_in_is_the_one_the_feed_opens() -> None:
    """The sweep's vectorised window start is ``warmup_window_start_ms``, at instants on every side of a close."""
    opens, closes = _sessions_ms()
    starts = _starts_ms()
    sampled = [
        int(instant)
        for close in closes[(closes >= starts[0]) & (closes <= starts[-1])][::37]
        for instant in (close - _MINUTE_MS, close, close + _MINUTE_MS)
    ]
    assert sampled

    for lookback_sessions in (1, 4, MAX_WARMUP_LOOKBACK_SESSIONS):
        expected = _window_starts_ms(lookback_sessions, np.array(sampled, dtype=np.int64))
        assert [warmup_window_start_ms(lookback_sessions, now_ms=instant) for instant in sampled] == expected.tolist()
    assert opens[0] < starts[0]


@pytest.mark.parametrize(("program_key", "overrides"), _POINTS)
def test_the_resolved_lookback_holds_the_longest_series_warmup_on_every_start(
    program_key: str, overrides: dict[str, int]
) -> None:
    """Whenever the bot starts, the history its seal names warms its longest series.

    Counted in regular hours only, the fewest bars a session holds: a run
    that also decides in extended hours sees more.
    """
    bars, decision_ms, lookback_sessions, _default_sessions = _longest_warmup(program_key, overrides)

    assert (
        _fewest_whole_decision_bars(lookback_sessions, decision_ms) >= bars
        or lookback_sessions == MAX_WARMUP_LOOKBACK_SESSIONS
    )


@pytest.mark.parametrize(
    ("bars", "decision_ms", "sessions"),
    [
        pytest.param(0, _FIFTEEN_MINUTES_MS, 0, id="no-bar-needs-no-session"),
        # 26 fifteen-minute bars in a regular session.
        pytest.param(1, _FIFTEEN_MINUTES_MS, 2, id="one-bar"),
        pytest.param(26, _FIFTEEN_MINUTES_MS, 2, id="exactly-one-session-of-bars"),
        pytest.param(27, _FIFTEEN_MINUTES_MS, 3, id="one-bar-past-a-session"),
        # SMA 50/200 on 15-minute bars: 8 sessions of bars, and one set aside
        # for the early close a window of 9 can hold (#2849).
        pytest.param(200, _FIFTEEN_MINUTES_MS, 9, id="sma-50-200"),
        pytest.param(109, _FIFTEEN_MINUTES_MS, 6, id="ema-default"),
        # A window of 17 sessions holds at most one early close; 18 can hold two.
        pytest.param(16 * 26, _FIFTEEN_MINUTES_MS, 17, id="sixteen-sessions-of-bars"),
        pytest.param(16 * 26 + 1, _FIFTEEN_MINUTES_MS, 19, id="past-sixteen-sessions-of-bars"),
        pytest.param(17 * 26, _FIFTEEN_MINUTES_MS, 19, id="seventeen-sessions-of-bars"),
        pytest.param(18 * 26, _FIFTEEN_MINUTES_MS, 20, id="eighteen-sessions-of-bars"),
        # 6 whole hourly bars in a regular session.
        pytest.param(6, 60 * _MINUTE_MS, 2, id="hourly-one-session"),
        pytest.param(7, 60 * _MINUTE_MS, 3, id="hourly-past-a-session"),
    ],
)
def test_warmup_sessions_holding_counts_the_sessions_whose_bars_cover_the_need(
    bars: int, decision_ms: int, sessions: int
) -> None:
    assert warmup_sessions_holding(bars, decision_ms) == sessions


@pytest.mark.parametrize("decision_minutes", [391, 480, 1440])
def test_warmup_sessions_holding_has_no_answer_for_a_bar_longer_than_a_session(decision_minutes: int) -> None:
    assert bars_per_regular_session(decision_minutes * _MINUTE_MS) == 0
    assert warmup_sessions_holding(1, decision_minutes * _MINUTE_MS) is None


def test_sessions_that_hold_the_need_never_come_up_short_of_early_closes_on_the_calendar() -> None:
    """No run of consecutive sessions holds more early closes than the rule sets aside for it.

    An early close counts for nothing, so the sessions the rule adds are the
    early closes a window can hold: one up to 17 sessions, two from 18. Swept
    over every window of every length up to the longest a lookback can
    reach, across 26 years of the canonical calendar. The 18 is the tightest
    it can be: the calendar has windows of 18 sessions with two.
    """
    regular_ms = _regular_session_ms()
    windows = session_windows_ms_utc(date(2015, 1, 1), date(2040, 12, 31))
    early = np.array([window.close_ms_utc - window.open_ms_utc < regular_ms for window in windows], dtype=np.int64)
    prefix = np.concatenate(([0], np.cumsum(early)))

    most_early_closes = {
        sessions: int((prefix[sessions:] - prefix[:-sessions]).max()) for sessions in range(1, MAX_WARMUP_LOOKBACK_SESSIONS + 3)
    }

    assert all(count == 1 for sessions, count in most_early_closes.items() if sessions <= 17)
    assert all(count == 2 for sessions, count in most_early_closes.items() if sessions >= 18)
    for bars in range(1, bars_per_regular_session(_FIFTEEN_MINUTES_MS) * 19):
        sessions = warmup_sessions_holding(bars, _FIFTEEN_MINUTES_MS)
        assert sessions is not None
        full_sessions = -(-bars // bars_per_regular_session(_FIFTEEN_MINUTES_MS))
        assert sessions - most_early_closes[sessions] >= full_sessions


def test_ema_previous_crossover_lookback_reaches_back_to_the_oldest_recent_cross() -> None:
    """Whenever the bot starts, its replay shows whether a cross is recent, at every legal length and limit.

    A cross is known only against the relation the bar before it
    established, so the oldest recent cross sits ``cross_max_age_bars`` bars
    back and needs the slow EMA ready one bar earlier still.
    """
    registration = _STRATEGY_REGISTRY["ema_previous_crossover"]
    contract = registration.signal_program_contract
    assert contract is not None
    properties = registration.param_schema.model_json_schema()["properties"]
    longest_limit = properties["cross_max_age_bars"]["maximum"]
    slow_lengths = range(properties["slow_period"]["minimum"], properties["slow_period"]["maximum"] + 1)

    for slow_period in slow_lengths:
        params = registration.param_schema(
            **{**contract.validated_settings, "fast_period": 2, "slow_period": slow_period}
        )
        lookback_sessions = contract.resolved_warmup_lookback_sessions(params)

        assert _fewest_whole_decision_bars(lookback_sessions, contract.decision_timeframe_ms) >= slow_period + longest_limit


@pytest.mark.parametrize(("program_key", "overrides"), _BEYOND_THE_DEFAULT_LOOKBACK)
def test_the_default_lookback_cannot_warm_a_deploy_beyond_it(program_key: str, overrides: dict[str, int]) -> None:
    """What #2841 repaired: on some start the default lookback leaves these deploys unready.

    It also keeps the test above honest, since a point the default already
    warms would pass it without the lookback following anything.
    """
    bars, decision_ms, lookback_sessions, default_sessions = _longest_warmup(program_key, overrides)

    assert lookback_sessions >= default_sessions
    if _fewest_whole_decision_bars(default_sessions, decision_ms) < bars:
        assert lookback_sessions > default_sessions


@pytest.mark.parametrize("resolution_minutes", [391, 480, 1440])
def test_a_decision_bar_longer_than_a_session_has_no_sized_lookback(resolution_minutes: int) -> None:
    """Long bars load the cap, then spawn; they are no longer a deploy refusal."""
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    contract = registration.signal_program_contract
    assert contract is not None
    assert (
        contract.resolved_warmup_lookback_sessions(registration.param_schema(resolution_minutes=resolution_minutes))
        == MAX_WARMUP_LOOKBACK_SESSIONS
    )


def test_a_program_with_no_series_has_nothing_to_size_on_any_bar() -> None:
    """``deployment_validation`` builds no series, so no bar length leaves it unwarmed or refuses it.

    No registered program pairs that shape with a configurable bar, so the
    long bar is put on a copy of its contract.
    """
    registration = _STRATEGY_REGISTRY["deployment_validation"]
    assert registration.signal_program_contract is not None
    contract = replace(registration.signal_program_contract, decision_timeframe_ms=1440 * _MINUTE_MS)
    defaults = registration.param_schema()

    assert contract.resolved_signals(defaults) == ()
    assert contract.resolved_warmup_lookback_sessions(defaults) == 1


@pytest.mark.parametrize(
    ("overrides", "unheld_bars"),
    [
        # Nineteen sessions hold 17 sessions of 26 bars (two set aside): 442.
        pytest.param({"long_window": 442}, 0, id="the-most-the-ceiling-holds"),
        pytest.param({"long_window": 443}, 1, id="one-bar-past-it"),
        pytest.param({"long_window": 500}, 58, id="sma-500"),
    ],
)
def test_the_bars_the_ceiling_cannot_hold_are_left_to_the_live_stream(
    overrides: dict[str, int], unheld_bars: int
) -> None:
    registration = _STRATEGY_REGISTRY["sma_crossover"]
    contract = registration.signal_program_contract
    assert contract is not None
    params = registration.param_schema(**overrides)

    assert contract.convergence_bars(params) == overrides["long_window"]
    assert contract.live_convergence_bars(params) == unheld_bars
    assert contract.resolved_warmup_lookback_sessions(params) == MAX_WARMUP_LOOKBACK_SESSIONS
