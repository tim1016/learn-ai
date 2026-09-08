"""Decision clock for continuity: when the next decision is due (spec #1921 §4.4).

A decision for bucket K fires when the consolidator receives the first source
minute of K+1, which closes 60 s after K's end -- except a session's last
bucket, which the runner force-flushes on the bar closing at the session
close. Both sessions are supported: the canonical calendar proves RTH; the
extended session is the executing broker's declared window (ADR 0059 D5.2),
resolved through ``session_authority`` -- broker capability data, not a
session literal of this module's own. An absent window *is* the regular
session here, so this module has no "extended without a window" state to
guard; ``app/services/decision_session.py`` owns that invariant for a run,
and the force-flush instant lives there too (``RunDecisionSession.close_ms``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    next_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.services.session_authority import extended_session_bounds_ms
from app.utils.timestamps import ny_datetime, to_ms_utc

if TYPE_CHECKING:
    # Type-only, matching ``run_replay_proof.py``'s existing guard on this same
    # symbol: importing ``bot_binding_repository`` at runtime would drag the
    # whole broker/clerk stack into what is otherwise a pure calendar module,
    # for the sake of one annotation.
    from app.services.bot_binding_repository import BrokerBotBinding

SOURCE_BAR_MS = 60_000

_ET = ZoneInfo("America/New_York")
_EPOCH_NAIVE = datetime(1970, 1, 1)


def floor_to_period_ms_et(timestamp_ms: int, period_ms: int) -> int:
    """Floor ``timestamp_ms`` to ``period_ms`` on the America/New_York wall clock.

    Read the ET wall-clock reading for ``timestamp_ms``, floor it as if it were
    itself an epoch offset, then convert back to ``int64 ms UTC`` -- what LEAN's
    floor of a naive, already-exchange-local ``DateTime`` amounts to. For a
    period under one day this equals flooring raw UTC ms (the ET-UTC offset is
    always a whole number of hours); for a day or longer it does not, and
    flooring raw UTC ms would land on UTC midnight, mislabeling a session's
    bars with the previous ET trading date.

    Formula: ``floor(et_wall_clock_ms / period_ms) * period_ms``, re-anchored in ET.
    Reference:
        LEAN ``Common/Data/Consolidators/PeriodCountConsolidatorBase`` — the
        floor of a naive, already-exchange-local ``DateTime``
        (``dateTime.Ticks % interval.Ticks``), as transcribed in the module
        docstring of ``app/engine/consolidators/trade_bar_consolidator.py``.
    Canonical implementation:
        ``app/engine/consolidators/trade_bar_consolidator.py::_floor_to_period_ms``
    Validated against:
        ``tests/services/test_decision_clock.py::test_floor_to_period_ms_et_matches_the_consolidators_floor``

    **Why this duplicate exists** (CLAUDE.md guiding philosophy #5 permits a
    duplicate only for a real reason, with a parity test naming the canonical
    file). The canonical copy lives in a *sealed artifact*: both
    ``trade_bar_consolidator.py`` and ``app/utils/timestamps.py`` are listed in
    every program's ``artifact_paths`` in ``app/engine/strategy/registry.py``,
    so editing either changes the running artifact digest and
    ``prove_running_program_build`` then finds no compatible golden-qualification
    receipt -- Start admission refuses every deploy until all programs are
    re-qualified. Hosting the decision clock's floor here keeps the sealed
    digests untouched; the parity test above is what keeps the two honest
    (controller ruling P5).
    """
    if period_ms <= 0:
        raise ValueError("period_ms must be positive")
    naive_et = ny_datetime(timestamp_ms).replace(tzinfo=None)
    naive_et_ms = int((naive_et - _EPOCH_NAIVE).total_seconds() * 1000)
    floored = _EPOCH_NAIVE + timedelta(milliseconds=(naive_et_ms // period_ms) * period_ms)
    return to_ms_utc(floored.replace(tzinfo=_ET))


def decision_timeframe_ms_for_binding(binding: BrokerBotBinding) -> int | None:
    """The seal-attested decision clock width, when this instance carries one.

    ``decision_timeframe_ms`` lives on the sealed program's inner
    ``configured_signal.data`` contract (``app/schemas/signal_program_seal.py``);
    ``None`` for a compatibility-mode strategy with no seal.
    """
    seal = binding.sealed_program
    if seal is None:
        return None
    return int(seal.configured_signal.data.decision_timeframe_ms)


def _require_source_multiple(timeframe_ms: int) -> None:
    if timeframe_ms <= 0 or timeframe_ms % SOURCE_BAR_MS != 0:
        raise ValueError(
            f"timeframe_ms must be a positive multiple of the {SOURCE_BAR_MS} ms source bar; got {timeframe_ms}"
        )


def _trigger_instants(*, open_ms: int, close_ms: int, timeframe_ms: int) -> list[int]:
    triggers: list[int] = []
    bucket_start = floor_to_period_ms_et(open_ms, timeframe_ms)
    while bucket_start < close_ms:
        bucket_end = bucket_start + timeframe_ms
        triggers.append(close_ms if bucket_end >= close_ms else bucket_end + SOURCE_BAR_MS)
        bucket_start = bucket_end
    return triggers


def rth_trigger_instants(session_date: date, *, timeframe_ms: int) -> list[int]:
    """Every instant on ``session_date`` at which a regular-session decision is due.

    One entry per decision bucket, in ascending order: the close of the first
    source minute of the following bucket, except the session's last bucket,
    which is force-flushed at the session close.

    At a one-minute timeframe the session-close instant appears **twice**: the
    second-to-last bucket's follow-on minute closes exactly at the session
    close, and the last bucket is force-flushed there too. Callers that treat
    this as a schedule must tolerate the repeat (``next_trigger_ms`` does --
    it returns the first entry strictly greater than its argument).

    Formula:
        for each bucket ``[b, b + timeframe_ms)`` from ``floor_et(open)`` while
        ``b < close``: ``close`` if ``b + timeframe_ms >= close`` else
        ``b + timeframe_ms + 60_000``.
    Reference:
        Spec ``docs/superpowers/specs/2026-09-02-feed-reconnect-continuity-design.md``
        §4.4 -- ``app/engine/consolidators/trade_bar_consolidator.py`` emits
        bucket K on the first source minute of K+1, which closes 60 s after
        K's end; the live runner force-flushes the session's last bucket at
        the calendar close. Session bounds come from the canonical calendar.
    Canonical implementation: this file.
    Validated against:
        ``tests/services/test_decision_clock.py::test_rth_trigger_instants_regular_session``,
        ``::test_rth_trigger_instants_early_close``,
        ``::test_rth_trigger_instants_repeats_the_close_at_a_one_minute_timeframe``
    """
    _require_source_multiple(timeframe_ms)
    return _trigger_instants(
        open_ms=session_open_ms_utc(session_date),
        close_ms=session_close_ms_utc(session_date),
        timeframe_ms=timeframe_ms,
    )


def extended_trigger_instants(session_date: date, *, timeframe_ms: int, window: ExtendedHoursWindow) -> list[int]:
    """Every instant on ``session_date`` at which an extended-session decision is due.

    Same bucket rule as the regular session, applied to the broker's declared
    window: the run force-flushes the day's last bucket at the declared close,
    and the regular close is an ordinary bucket boundary inside the day.

    At a one-minute timeframe the declared-close instant appears **twice**,
    for the same reason as ``rth_trigger_instants``: the second-to-last
    bucket's follow-on minute closes exactly at the declared close, and the
    last bucket is force-flushed there too. Callers that treat this as a
    schedule must tolerate the repeat (``next_trigger_ms`` does).

    Formula:
        for each bucket ``[b, b + timeframe_ms)`` from ``floor_et(xh_open)`` while
        ``b < xh_close``: ``xh_close`` if ``b + timeframe_ms >= xh_close`` else
        ``b + timeframe_ms + 60_000``, where ``[xh_open, xh_close)`` =
        ``extended_session_bounds_ms(session_date, window)``.
    Reference:
        ADR 0059 Decision 5.2 (the clock triggers on the timeframe within the
        phases the binding includes); bucket rule as ``rth_trigger_instants``.
    Canonical implementation: this file.
    Validated against:
        tests/services/test_decision_clock.py::test_extended_trigger_instants_regular_day,
        ::test_extended_trigger_instants_early_close_is_unchanged
    """
    _require_source_multiple(timeframe_ms)
    bounds = extended_session_bounds_ms(session_date, window=window)
    return _trigger_instants(open_ms=bounds.open_ms, close_ms=bounds.close_ms, timeframe_ms=timeframe_ms)


def _schedule(*, timeframe_ms: int, window: ExtendedHoursWindow | None) -> Callable[[date], list[int]]:
    """A day's trigger instants: the regular session, or the declared window.

    ``window`` *is* the discriminator — an absent one means the regular
    session, and there is therefore no "extended without a window" state to
    guard against here. ``RunDecisionSession`` (``app/services/decision_session.py``)
    is where that invariant is established for a run.
    """
    if window is None:
        return lambda day: rth_trigger_instants(day, timeframe_ms=timeframe_ms)
    return lambda day: extended_trigger_instants(day, timeframe_ms=timeframe_ms, window=window)


def next_trigger_ms(
    last_delivered_end_ms: int,
    *,
    timeframe_ms: int,
    window: ExtendedHoursWindow | None = None,
) -> int:
    """The first decision instant strictly after ``last_delivered_end_ms``.

    Rolls forward across holidays and weekends until a trading day supplies a
    later trigger. ``window`` selects the session: absent is the regular one.

    Formula:
        ``min{t in S(d) : t > last_delivered_end_ms}`` over trading days ``d``
        from the ET date of ``last_delivered_end_ms`` forward, where ``S(d)``
        is ``rth_trigger_instants(d, ...)`` when ``window`` is absent and
        ``extended_trigger_instants(d, ..., window)`` when it is declared.
    Reference:
        As ``rth_trigger_instants``/``extended_trigger_instants`` (spec §4.4;
        ADR 0059 D5.2); trading days from the canonical calendar
        ``app/lean_sidecar/trading_calendar.py``.
    Canonical implementation: this file.
    Validated against:
        ``tests/services/test_decision_clock.py::test_next_trigger_after_last_delivered_minute``,
        ``::test_next_trigger_rolls_to_the_next_session``, ``::test_one_minute_timeframe``,
        ``::test_extended_next_trigger_rolls_across_the_weekend``,
        ``::test_extended_next_trigger_before_the_declared_open_is_the_first_bucket``
    """
    triggers_for = _schedule(timeframe_ms=timeframe_ms, window=window)
    session_date = ny_datetime(last_delivered_end_ms).date()
    if not is_trading_day(session_date):
        session_date = next_trading_day(session_date)
    while True:
        for trigger in triggers_for(session_date):
            if trigger > last_delivered_end_ms:
                return trigger
        session_date = next_trading_day(session_date)


def next_trigger_function(
    timeframe_ms: int, *, window: ExtendedHoursWindow | None = None
) -> Callable[[int], int]:
    """Bind the clock's parameters into the single-argument callable the continuity loop schedules against."""
    _require_source_multiple(timeframe_ms)  # fail at construction, not mid-run
    return lambda last_end: next_trigger_ms(last_end, timeframe_ms=timeframe_ms, window=window)
