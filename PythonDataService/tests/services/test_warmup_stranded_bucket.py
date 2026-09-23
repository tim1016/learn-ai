"""#2303: warmup's last complete bucket is decided in the replay, never by the first live bar.

The warmup replay used to only ``update`` the consolidators, which fire a
bucket lazily, on the first bar of the *next* bucket. A warmup ending on a
bucket's close therefore left that bucket working, and the first live bar --
the next session's 09:30 print -- fired it under the ``DECIDE`` mode warmup had
captured for its closing minute. Pause could not stop it, it was decided days
late, and a Resume decided an already-decided ``evaluation_id`` again.

Every probe below is the issue's own: RTH one-minute history for 2026-09-17,
18 and 21, then one live bar at 09:30 ET on 2026-09-22. Session boundaries come
from the canonical calendar.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest

from app.lean_sidecar.trading_calendar import session_close_ms_utc, session_open_ms_utc
from app.marketdata.feed import MarketDataBar
from app.services.bot_runtime import PauseAwareFeed
from app.services.bot_trade_strategy import StrategyEvaluation, strategy_evaluations
from tests._helpers.bot_runner.ema_parity import _ema_signal_evaluation_id
from tests.services.test_candidate_uncaptured_at_crash import _binding, _PhaseFeed

_HISTORY_SESSIONS = (date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 21))
_LIVE_SESSION = date(2026, 9, 22)
_LAST_WARMUP_CLOSE_MS = session_close_ms_utc(_HISTORY_SESSIONS[-1])


def _minute(end_ms: int, close: Decimal) -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=100,
        fetched_at_ms=end_ms,
        feed_id="fake-phase",
        session_phase="RTH",
    )


def _session_minutes(session: date, first_close: Decimal) -> list[MarketDataBar]:
    open_ms, close_ms = session_open_ms_utc(session), session_close_ms_utc(session)
    return [
        _minute(end_ms, first_close + Decimal(index) / 100)
        for index, end_ms in enumerate(range(open_ms + 60_000, close_ms + 1, 60_000))
    ]


def _history() -> list[MarketDataBar]:
    bars: list[MarketDataBar] = []
    for offset, session in enumerate(_HISTORY_SESSIONS):
        bars.extend(_session_minutes(session, Decimal(500 + offset)))
    assert bars[-1].end_ms == _LAST_WARMUP_CLOSE_MS  # warmup ends on a bucket's close
    return bars


def _first_live_bar() -> MarketDataBar:
    return _minute(session_open_ms_utc(_LIVE_SESSION) + 60_000, Decimal("510"))


async def _drain(stream: AsyncIterator[StrategyEvaluation]) -> list[StrategyEvaluation]:
    return [evaluation async for evaluation in stream]


@pytest.mark.asyncio
async def test_the_first_live_bar_never_fires_the_last_warmup_bucket() -> None:
    feed = _PhaseFeed(retained_bars=_history(), live_bars=[_first_live_bar()])

    evaluations = await _drain(strategy_evaluations(_binding(run_id="run-1"), feed))

    # Before the fix: one DECIDE evaluation for 2026-09-21 16:00 ET, fired by
    # the 09:30 bar of 2026-09-22.
    assert [e.decision_bar_close_ms for e in evaluations] == []


@pytest.mark.asyncio
async def test_a_paused_run_never_receives_the_stranded_bucket_as_decide() -> None:
    paused = asyncio.Event()  # never set: the run is paused, observe-only
    feed = PauseAwareFeed(_PhaseFeed(retained_bars=_history(), live_bars=[_first_live_bar()]), paused)
    assert feed.observe_only

    evaluations = await _drain(strategy_evaluations(_binding(run_id="run-1"), feed))

    assert not any(
        e.decision_bar_close_ms == _LAST_WARMUP_CLOSE_MS and e.evaluation_mode.value == "DECIDE"
        for e in evaluations
    ), "Pause failed open: the stranded warmup bucket still arrived as DECIDE"


@pytest.mark.asyncio
async def test_resume_never_redecides_a_bucket_the_earlier_run_decided() -> None:
    """FR-016: the bucket is inside the replay, so its captured disposition is reapplied."""
    decided = _ema_signal_evaluation_id(_LAST_WARMUP_CLOSE_MS)
    feed = _PhaseFeed(retained_bars=_history(), live_bars=[_first_live_bar()])

    evaluations = await _drain(
        strategy_evaluations(
            _binding(run_id="run-2"), feed, captured_decisions={decided: "no_action"}
        )
    )

    assert decided not in {e.evaluation_id for e in evaluations}
