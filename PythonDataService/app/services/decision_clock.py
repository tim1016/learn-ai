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
from datetime import date
from typing import TYPE_CHECKING

from app.lean_sidecar.trading_calendar import (
    is_trading_day,
    next_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.marketdata.feed import DecisionSession
from app.utils.timestamps import floor_to_period_ms_et, ny_datetime

if TYPE_CHECKING:
    # Type-only, matching ``run_replay_proof.py``'s existing guard on this same
    # symbol: importing ``bot_binding_repository`` at runtime would drag the
    # whole broker/clerk stack into what is otherwise a pure calendar module,
    # for the sake of one annotation.
    from app.services.bot_binding_repository import BrokerBotBinding

SOURCE_BAR_MS = 60_000


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
    """
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
