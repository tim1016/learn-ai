"""Decision clock for continuity: when the next decision is due (spec #1921 §4.4).

A decision for bucket K fires when the consolidator receives the first source
minute of K+1, which closes 60 s after K's end -- except a session's last
bucket, which the runner force-flushes on the bar closing at the session
close. Only the regular session is supported: the canonical calendar proves
RTH; extended windows are broker-proven capabilities (session_authority), so
``decision_session="all"`` is refused here (controller ruling R1).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    next_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.marketdata.feed import DecisionSession
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
    if timeframe_ms <= 0 or timeframe_ms % SOURCE_BAR_MS != 0:
        raise ValueError(
            f"timeframe_ms must be a positive multiple of the {SOURCE_BAR_MS} ms "
            f"source bar; got {timeframe_ms}"
        )
    open_ms = session_open_ms_utc(session_date)
    close_ms = session_close_ms_utc(session_date)
    triggers: list[int] = []
    bucket_start = floor_to_period_ms_et(open_ms, timeframe_ms)
    while bucket_start < close_ms:
        bucket_end = bucket_start + timeframe_ms
        triggers.append(close_ms if bucket_end >= close_ms else bucket_end + SOURCE_BAR_MS)
        bucket_start = bucket_end
    return triggers


def next_trigger_ms(
    last_delivered_end_ms: int, *, timeframe_ms: int, decision_session: DecisionSession
) -> int:
    """The first decision instant strictly after ``last_delivered_end_ms``.

    Rolls forward across holidays and weekends until a trading day supplies a
    later trigger. ``decision_session="all"`` is refused (ruling R1).

    Formula:
        ``min{t in rth_trigger_instants(d) : t > last_delivered_end_ms}`` over
        trading days ``d`` from the ET date of ``last_delivered_end_ms`` forward.
    Reference:
        As ``rth_trigger_instants`` (spec §4.4); trading days from the canonical
        calendar ``app/lean_sidecar/trading_calendar.py``.
    Canonical implementation: this file.
    Validated against:
        ``tests/services/test_decision_clock.py::test_next_trigger_after_last_delivered_minute``,
        ``::test_next_trigger_rolls_to_the_next_session``, ``::test_one_minute_timeframe``
    """
    if decision_session != "rth":
        raise NotImplementedError(
            "decision_session='all' has no calendar-proven trigger set yet (ruling R1)"
        )
    session_date = ny_datetime(last_delivered_end_ms).date()
    if not is_trading_day(session_date):
        session_date = next_trading_day(session_date)
    while True:
        for trigger in rth_trigger_instants(session_date, timeframe_ms=timeframe_ms):
            if trigger > last_delivered_end_ms:
                return trigger
        session_date = next_trading_day(session_date)


def rth_next_trigger_function(timeframe_ms: int) -> Callable[[int], int]:
    """Bind ``timeframe_ms`` into the single-argument next-trigger callable the
    continuity loop and the bot layer schedule against."""
    return lambda last_end: next_trigger_ms(
        last_end, timeframe_ms=timeframe_ms, decision_session="rth"
    )
