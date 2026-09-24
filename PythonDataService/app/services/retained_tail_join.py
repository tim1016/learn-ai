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
    MarketDataBar,
    MarketDataFeed,
    MarketDataFeedError,
)
from app.services.decision_session import RunDecisionSession
from app.services.session_authority import declared_session_bounds
from app.utils.session_anchors import et_date_at_ms

_MINUTE_MS = 60_000


@dataclass(frozen=True)
class OwedHole:
    """The minutes between the retained tail and ``now_ms`` the run decides on.

    Minutes are named by their close (``end_ms``), the instant a bar for them
    exists. ``extended_minute_end_ms`` is the first owed extended-hours minute,
    or ``None`` when the hole holds none.
    """

    regular_minute_ends_ms: tuple[int, ...]
    extended_minute_end_ms: int | None


@dataclass(frozen=True)
class RetainedTailJoin:
    """How one resumed run's retained bars were joined to the present."""

    retained_end_ms: int
    joined_at_ms: int
    filled: tuple[MarketDataBar, ...]
    # ``None``: warm on every retained bar plus ``filled``. Otherwise the hole
    # outran the lookback, and warmup starts at the first history bar.
    warm_from_ms: int | None = None


def owed_hole(
    session: RunDecisionSession, *, retained_end_ms: int, now_ms: int
) -> OwedHole:
    """Every minute closing in ``(retained_end_ms, now_ms]`` that ``session`` decides on."""
    if now_ms <= retained_end_ms:
        return OwedHole(regular_minute_ends_ms=(), extended_minute_end_ms=None)
    first_date, last_date = et_date_at_ms(retained_end_ms), et_date_at_ms(now_ms)
    regular: list[int] = []
    extended: int | None = None
    for window in session_windows_ms_utc(first_date, last_date):
        regular.extend(
            end_ms
            for end_ms in range(window.open_ms_utc + _MINUTE_MS, window.close_ms_utc + 1, _MINUTE_MS)
            if retained_end_ms < end_ms <= now_ms
        )
        if extended is None and session.window is not None:
            bounds = declared_session_bounds(window.session_date, session.window)
            assert bounds is not None  # a calendar session is a trading day
            extended = _first_owed_end(
                ((bounds.open_ms, bounds.rth_open_ms), (bounds.rth_close_ms, bounds.close_ms)),
                retained_end_ms=retained_end_ms,
                now_ms=now_ms,
            )
    return OwedHole(regular_minute_ends_ms=tuple(regular), extended_minute_end_ms=extended)


def _first_owed_end(
    spans: Sequence[tuple[int, int]], *, retained_end_ms: int, now_ms: int
) -> int | None:
    """The first minute close in any ``[open, close)`` span inside the hole."""
    for open_ms, close_ms in spans:
        first_end = max(open_ms + _MINUTE_MS, retained_end_ms + _MINUTE_MS)
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

    Raises ``MarketDataFeedError`` with ``RESUME_HOLE_AFTER_HOURS`` when the
    session owes an extended-hours minute, with ``RESUME_HOLE_UNFILLED`` when
    history is missing an owed regular minute, and propagates the source's
    own ``WARMUP_HISTORY_UNAVAILABLE`` when history cannot be fetched.

    Every closed bar history returns after the tail is kept, not only the
    owed ones: the ledger retains unfiltered observations, exactly as the
    live stream does, and the session filter applies downstream.
    """
    hole = owed_hole(session, retained_end_ms=retained_end_ms, now_ms=now_ms)
    if hole.extended_minute_end_ms is not None:
        raise MarketDataFeedError(
            f"the retained {symbol} bars end at {retained_end_ms} and the run decides on "
            f"extended-hours minutes after it (first closing at {hole.extended_minute_end_ms}); "
            "IBKR history does not reproduce extended-hours minutes exactly, so the hole "
            "cannot be filled",
            reason=RESUME_HOLE_AFTER_HOURS,
        )
    if not hole.regular_minute_ends_ms:
        return RetainedTailJoin(retained_end_ms=retained_end_ms, joined_at_ms=now_ms, filled=())
    hole_days = (et_date_at_ms(now_ms) - et_date_at_ms(retained_end_ms)).days + 1
    outruns_lookback = hole_days > lookback_days
    history = await source.recent_closed_bars(
        symbol, use_rth=False, lookback_days=min(hole_days, lookback_days)
    )
    warm_from_ms = min((bar.start_ms for bar in history), default=None) if outruns_lookback else None
    filled = tuple(
        sorted((bar for bar in history if bar.end_ms > retained_end_ms), key=lambda bar: bar.end_ms)
    )
    owed_after_ms = retained_end_ms if warm_from_ms is None else max(retained_end_ms, warm_from_ms)
    owed = [end_ms for end_ms in hole.regular_minute_ends_ms if end_ms > owed_after_ms]
    returned = {bar.end_ms for bar in filled}
    missing = [end_ms for end_ms in owed if end_ms not in returned]
    if missing or (outruns_lookback and not filled):
        raise MarketDataFeedError(
            f"IBKR history returned {len(owed) - len(missing)} of the {len(owed)} regular-hours "
            f"{symbol} minutes the run owes after its retained bars end at {retained_end_ms}"
            + (f" (first missing closes at {missing[0]})" if missing else "")
            + "; warming across the hole would decide on indicators that skipped it",
            reason=RESUME_HOLE_UNFILLED,
        )
    return RetainedTailJoin(
        retained_end_ms=retained_end_ms,
        joined_at_ms=now_ms,
        filled=filled,
        warm_from_ms=warm_from_ms,
    )


__all__ = ["OwedHole", "RetainedTailJoin", "join_retained_tail", "owed_hole"]
