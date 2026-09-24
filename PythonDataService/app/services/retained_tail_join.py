"""Join a resumed run's retained bars to the present before it warms up (#2314).

A later run of a strategy instance warms up on the bars its predecessors
retained, and those end wherever the last run stopped. A Stop at 10:07 and a
Resume at 13:00 leave a three-hour hole the live stream will never deliver,
and warming across it decides on indicators that skipped it.

The hole is filled from IBKR's 1-minute TRADES history, the same endpoint a
fresh deploy warms on. Measured against a full live day (SPY, 2026-09-24),
that history reproduced every live-assembled regular-hours minute exactly,
volume included, and was unchanged when fetched again hours later. It did not
reproduce every extended-hours minute (a cent off in 20 of 69), so a hole
containing an extended-hours minute the run decides on is refused rather than
filled, by owner decision.

Only minutes the run's decision session consumes are owed: a regular-hours
run owes the calendar's regular minutes in the hole, an extended run also
owes its declared window's extended minutes — which is what refuses it.

The fetch never reaches past the run's sealed warmup lookback, the same
window a fresh deploy warms on. A hole longer than that leaves the retained
bars before it outside the warmup horizon, so the run warms like a fresh
deploy: from the lookback's history alone, with ``warm_from_ms`` recording
where that warmup began so the replay proof replays the same bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from app.marketdata.feed import (
    RESUME_HOLE_AFTER_HOURS,
    RESUME_HOLE_UNFILLED,
    WARMUP_REFUSAL_REASONS,
    MarketDataBar,
    MarketDataFeed,
    MarketDataFeedError,
    warmup_window_start_ms,
)
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import declared_session_bounds
from app.services.source_bar_ledger import RetainedSourceBar, RetainedWarmupJoin, SourceBarLedger
from app.utils.session_anchors import et_date_at_ms

_MINUTE_MS = 60_000
_DAY_MS = 86_400_000


@dataclass(frozen=True)
class RetainedTailJoin:
    """How one resumed run's retained bars were joined to the present."""

    retained_end_ms: int
    joined_at_ms: int
    filled: tuple[MarketDataBar, ...]
    # ``None``: warm on every retained bar plus ``filled``. Otherwise the hole
    # outran the lookback, and warmup starts here, as a fresh deploy's does.
    warm_from_ms: int | None = None


def owed_regular_minute_ends(*, after_ms: int, now_ms: int) -> tuple[int, ...]:
    """Every regular-hours minute close in ``(after_ms, now_ms]``, from the calendar."""
    if now_ms <= after_ms:
        return ()
    return tuple(
        end_ms
        for window in session_windows_ms_utc(et_date_at_ms(after_ms), et_date_at_ms(now_ms))
        for end_ms in range(window.open_ms_utc + _MINUTE_MS, window.close_ms_utc + 1, _MINUTE_MS)
        if after_ms < end_ms <= now_ms
    )


def first_owed_extended_minute_end(
    session: RunDecisionSession, *, after_ms: int, now_ms: int
) -> int | None:
    """The first extended-hours minute close in ``(after_ms, now_ms]`` ``session`` decides on.

    ``None`` for a regular-hours session, which decides on none. Walks the
    calendar session by session and stops at the first hit, so a long hole
    costs one pass over its sessions, never one step per minute.
    """
    if session.window is None or now_ms <= after_ms:
        return None
    for window in session_windows_ms_utc(et_date_at_ms(after_ms), et_date_at_ms(now_ms)):
        bounds = declared_session_bounds(window.session_date, session.window)
        if bounds is None:
            raise ValueError(f"{window.session_date.isoformat()} is a session with no declared bounds")
        # A bar opening in [open, close) closes in (open, close]; the first
        # close after ``after_ms`` is one minute past it (bars are minute-aligned).
        for open_ms, close_ms in ((bounds.open_ms, bounds.rth_open_ms), (bounds.rth_close_ms, bounds.close_ms)):
            first_end = max(open_ms, after_ms) + _MINUTE_MS
            if first_end <= min(close_ms, now_ms):
                return first_end
    return None


async def join_retained_tail(
    source: MarketDataFeed,
    *,
    symbol: str,
    session: RunDecisionSession,
    retained_end_ms: int,
    now_ms: int,
    lookback_days: int,
) -> RetainedTailJoin:
    """Fill the hole after ``retained_end_ms`` from IBKR history, or refuse the run.

    **Refusal facts span the whole hole.** An extended-hours minute the run
    decides on anywhere between the retained tail and now refuses the run
    (``RESUME_HOLE_AFTER_HOURS``, owner decision): the refusal is about the
    hole the bot sat through, not about how much of it warmup replays.

    **Data is clamped to the lookback.** A hole within the sealed lookback
    window (``warmup_window_start_ms``, the same window the fresh-warmup
    coverage rule owes) is fetched and owed from the retained tail. A longer one warms like a fresh
    deploy: the lookback's history is fetched under the source's own warmup
    coverage rule (the canonical one a fresh deploy obeys, which is also why
    an empty lookback with no owed session is admitted), warmup starts at its
    first bar (``warm_from_ms``), and only minutes after that are owed.

    ``RESUME_HOLE_UNFILLED`` refuses when history is missing an owed regular
    minute; the source's ``WARMUP_HISTORY_UNAVAILABLE`` propagates. Requiring
    every regular minute is safe for thin symbols: IBKR's 1-minute TRADES
    history returns a zero-volume bar for a no-trade minute (receipt:
    ``tests/fixtures/golden/ibkr-history-vs-live-minutes-2026-09-24``).

    Every closed bar history returns after the tail is kept, not only the
    owed ones: the ledger retains unfiltered observations, exactly as the
    live stream does, and the session filter applies downstream.
    """
    unfilled = RetainedTailJoin(retained_end_ms=retained_end_ms, joined_at_ms=now_ms, filled=())
    if now_ms <= retained_end_ms:
        return unfilled
    extended_end_ms = first_owed_extended_minute_end(session, after_ms=retained_end_ms, now_ms=now_ms)
    if extended_end_ms is not None:
        raise MarketDataFeedError(
            f"the run decides on extended-hours {symbol} minutes it did not observe (first "
            f"closing at {extended_end_ms}); IBKR history does not reproduce extended-hours "
            "minutes exactly, so the hole cannot be filled",
            reason=RESUME_HOLE_AFTER_HOURS,
        )
    owed = owed_regular_minute_ends(after_ms=retained_end_ms, now_ms=now_ms)
    if not owed:
        # Nothing the run decides on passed while it was stopped (a closed
        # night, a weekend): the retained bars already reach now, however long
        # the wall-clock gap -- no fetch, and no floor that would drop them.
        return unfilled
    warm_from_ms: int | None = None
    if retained_end_ms >= warmup_window_start_ms(lookback_days, now_ms=now_ms):
        # The smallest window reaching back to the tail; never past the lookback.
        hole_days = -(-(now_ms - retained_end_ms) // _DAY_MS)
        history = await source.recent_closed_bars(symbol, use_rth=False, lookback_days=hole_days)
    else:
        history = await source.recent_closed_bars(symbol, use_rth=False, lookback_days=lookback_days)
        first_start_ms = min((bar.start_ms for bar in history), default=now_ms)
        if first_start_ms > retained_end_ms:
            # History starts after the tail, so the stretch between them is
            # outside the lookback: warm from history alone, as a fresh deploy
            # would, and owe only what follows it. An empty lookback passed the
            # source's coverage rule, so it owes no session and warms from now.
            warm_from_ms = first_start_ms
            owed = tuple(end_ms for end_ms in owed if end_ms > first_start_ms)
        # Otherwise history reaches back to the tail and fills the whole hole.
    filled = tuple(
        sorted((bar for bar in history if bar.end_ms > retained_end_ms), key=lambda bar: bar.end_ms)
    )
    returned = {bar.end_ms for bar in filled}
    missing = [end_ms for end_ms in owed if end_ms not in returned]
    if missing:
        raise MarketDataFeedError(
            f"IBKR history returned {len(owed) - len(missing)} of the {len(owed)} regular-hours "
            f"{symbol} minutes the run owes (first missing closes at {missing[0]}); warming "
            "across the hole would decide on indicators that skipped it",
            reason=RESUME_HOLE_UNFILLED,
        )
    return RetainedTailJoin(
        retained_end_ms=retained_end_ms,
        joined_at_ms=now_ms,
        filled=filled,
        warm_from_ms=warm_from_ms,
    )


async def warmup_rows_after_join(
    source: MarketDataFeed,
    ledger: SourceBarLedger,
    *,
    run_id: str,
    session: RunDecisionSession,
    symbol: str,
    retained: Sequence[RetainedSourceBar],
    lookback_days: int,
    now_ms: int,
) -> list[RetainedSourceBar]:
    """Join a resumed run's retained bars to ``now_ms``, record it, and return what it warms on.

    The backfill and its join commit in one ledger transaction; a refusal is
    recorded before it is raised, so the run's evidence says why it never
    decided. The rows returned are every retained row plus the backfill that
    open at or after the instance's warm floor: a hole that outran the
    lookback (this run's, or an earlier run's) warmed from history alone, and
    nothing before that point is replayed again.
    """
    retained_end_ms = retained[-1].end_ms
    try:
        join = await join_retained_tail(
            source,
            symbol=symbol,
            session=session,
            retained_end_ms=retained_end_ms,
            now_ms=now_ms,
            lookback_days=lookback_days,
        )
    except MarketDataFeedError as exc:
        if exc.reason in WARMUP_REFUSAL_REASONS:
            ledger.record_warmup_join(
                RetainedWarmupJoin(
                    run_id=run_id,
                    outcome="refused",
                    retained_end_ms=retained_end_ms,
                    joined_at_ms=now_ms,
                    reason_code=exc.reason,
                )
            )
        raise
    filled_window = (
        {}
        if not join.filled
        else {
            "filled_count": len(join.filled),
            "filled_start_ms": join.filled[0].start_ms,
            "filled_end_ms": join.filled[-1].end_ms,
        }
    )
    filled = ledger.retain_warmup_join(
        join.filled,
        RetainedWarmupJoin(
            run_id=run_id,
            outcome="filled" if join.filled else "contiguous",
            retained_end_ms=retained_end_ms,
            joined_at_ms=now_ms,
            warm_from_ms=join.warm_from_ms,
            **filled_window,
        ),
    )
    floor_ms = ledger.warm_floor_ms(run_id=run_id)
    return [row for row in (*retained, *filled) if floor_ms is None or row.start_ms >= floor_ms]


__all__ = [
    "RetainedTailJoin",
    "first_owed_extended_minute_end",
    "join_retained_tail",
    "owed_regular_minute_ends",
    "warmup_rows_after_join",
]
