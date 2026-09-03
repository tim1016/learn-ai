"""Continuity helpers for IbkrMarketDataFeed.stream_bars (spec #1921 §4.2 rules 3, 7, 9).

The feed keeps the generator; this module owns the wait under the consumer's
deadline, the resolution of minutes an interruption touched, and the awaited
event emission. No historical substitution exists here: a granted window is
refused with ``SUBSTITUTION_PATH_UNAVAILABLE`` (controller ruling R3).

Every temporal value is ``int64 ms UTC``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.minute_assembler import RTH_CONTRIBUTIONS_PER_MINUTE
from app.marketdata.feed import (
    ContinuityEventKind,
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
    record_continuity_event,
)
from app.services.session_authority import session_state_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

WAIT_POLL_S = 0.25
SOURCE_BAR_MS = 60_000


@dataclass
class ContinuityState:
    """Per-consumer continuity bookkeeping owned by one stream_bars call."""

    feed_id: str
    symbol: str
    policy: ContinuityPolicy
    last_delivered_end_ms: int | None = None
    generation: int = 0
    last_recovered_ref: ContinuityEventRef | None = None
    #: Deadline the in-flight interruption recorded, and the only one any wait
    #: for that interruption may enforce. ``None`` until one is observed.
    interruption_deadline_ms: int | None = None
    #: Minute starts an interruption touched: the open minute the assembler
    #: still held when delivery stopped. Whether a minute's contributions span
    #: generations is a property of the merge, but whether an interruption
    #: *touched* it is a fact only the loop can see -- an interruption that
    #: outlives the open minute leaves it with one generation's contributions
    #: and nothing on the bar itself records that it was cut short (ruling P9).
    touched_minute_starts: set[int] = field(default_factory=set)
    #: Whether the next emitted bar must be scanned for wholly-missed minutes
    #: behind it. Set on recovery, cleared once that bar resolves: a gap that no
    #: interruption explains is an ordinary gap, and ordinary gaps are
    #: non-fatal (ruling P11).
    scan_missed_windows: bool = False

    async def record(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        return await record_continuity_event(self.policy, event)

    def event(self, kind: ContinuityEventKind, **fields: object) -> FeedContinuityEvent:
        return FeedContinuityEvent(
            kind=kind, feed_id=self.feed_id, symbol=self.symbol, observed_at_ms=now_ms_utc(), **fields
        )


def _healthy(client: IbkrClient) -> bool:
    if not client.is_connected() or client.connection_lost:
        return False
    monitor = get_monitor()
    return monitor is None or monitor.recovery_state == "HEALTHY"


async def wait_for_healthy(client: IbkrClient, state: ContinuityState, *, deadline_ms: int) -> None:
    """Block until the socket is healthy again or ``deadline_ms`` passes.

    The deadline is the caller's, computed once when the interruption was
    observed and recorded on that ``interruption`` event, so the wait the
    journal describes is the wait that is actually enforced. Re-deriving it here
    from the live ``last_delivered_end_ms`` would silently extend it by a whole
    decision interval whenever the minute flushed after that event crossed a
    decision trigger — and the journal would still claim the shorter one.
    """
    while not _healthy(client):
        if now_ms_utc() >= deadline_ms:
            await state.record(
                state.event(
                    "refused",
                    reason="DECISION_BAR_MISSED",
                    last_delivered_end_ms=state.last_delivered_end_ms,
                    deadline_ms=deadline_ms,
                )
            )
            logger.error(
                "Feed continuity refused: decision bar missed",
                extra={
                    "action": "marketdata_continuity_refused",
                    "symbol": state.symbol,
                    "reason": "DECISION_BAR_MISSED",
                    "deadline_ms": deadline_ms,
                },
            )
            raise MarketDataFeedError(
                f"{state.symbol} was not recovered before {deadline_ms}", reason="DECISION_BAR_MISSED"
            )
        await asyncio.sleep(WAIT_POLL_S)


def inside_decision_session(state: ContinuityState, window_start_ms: int) -> bool:
    """Whether the consumer's decision clock treats ``window_start_ms`` as decidable."""
    return state.policy.decision_session == "rth" and session_state_at_ms(now_ms=window_start_ms).phase == "RTH"


async def resolve_unresolvable_window(
    state: ContinuityState,
    window_start_ms: int,
    window_end_ms: int,
    *,
    contribution_count: int | None = None,
) -> None:
    """Rule 3 for one window nothing real-time can prove complete: gap outside the session, refusal inside.

    ``contribution_count`` is how many 5-second contributions the emitted
    minute actually held, when the window is one such minute. A window nothing
    was ever assembled for carries ``None`` -- absent is a different fact from
    zero, and a coalesced multi-minute window has no single count.
    """
    if not inside_decision_session(state, window_start_ms):
        await state.record(
            state.event(
                "gap",
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                last_delivered_end_ms=state.last_delivered_end_ms,
                contribution_count=contribution_count,
            )
        )
        logger.warning(
            "Feed continuity omitted an unresolvable minute outside the decision session",
            extra={"action": "marketdata_gap_omitted", "symbol": state.symbol, "window_start_ms": window_start_ms},
        )
        state.last_delivered_end_ms = window_end_ms
        return
    verdict = state.policy.substitution_grant(window_start_ms, window_end_ms)
    reason = verdict.reason if isinstance(verdict, SubstitutionRefusal) else "SUBSTITUTION_PATH_UNAVAILABLE"
    if isinstance(verdict, SubstitutionGrant):
        logger.error(
            "A substitution grant was offered but no substitution path exists (fail closed)",
            extra={"action": "marketdata_continuity_refused", "symbol": state.symbol, "reason": reason},
        )
    await state.record(
        state.event(
            "refused",
            reason=reason,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            last_delivered_end_ms=state.last_delivered_end_ms,
            contribution_count=contribution_count,
        )
    )
    logger.error(
        "Feed continuity refused: minute cannot be proven complete",
        extra={"action": "marketdata_continuity_refused", "symbol": state.symbol, "reason": reason,
               "window_start_ms": window_start_ms},
    )
    raise MarketDataFeedError(
        f"minute {window_start_ms}..{window_end_ms} for {state.symbol} cannot be proven complete", reason=reason
    )


def session_uniform_windows(
    state: ContinuityState, start_ms: int, end_ms: int
) -> list[tuple[int, int]]:
    """Split ``[start_ms, end_ms)`` into the fewest windows of one session verdict each.

    Contiguous unresolvable minutes are one window, not N (ruling P11): a
    consumer that may one day authorize a substitution needs to be asked about
    the *episode*, and a reader of the journal needs one fact per episode. The
    only reason to split is that ``inside_decision_session`` disagrees across
    the range -- a window straddling the RTH open is half a refusal and half a
    gap, so it is cut at the boundary.
    """
    windows: list[tuple[int, int]] = []
    run_start: int | None = None
    run_inside = False
    for minute_start_ms in range(start_ms, end_ms, SOURCE_BAR_MS):
        inside = inside_decision_session(state, minute_start_ms)
        if run_start is None:
            run_start, run_inside = minute_start_ms, inside
        elif inside != run_inside:
            windows.append((run_start, minute_start_ms))
            run_start, run_inside = minute_start_ms, inside
    if run_start is not None:
        windows.append((run_start, end_ms))
    return windows


async def resolve_missed_windows(state: ContinuityState, until_start_ms: int) -> None:
    """Resolve every minute an interruption swallowed whole, coalesced by session verdict.

    Runs only for the first emitted bar after a recorded interruption. A gap
    with no interruption behind it is an ordinary gap, which the port promises
    is non-fatal (spec §6) -- scanning on every bar would make one fatal.
    """
    last_delivered_end_ms = state.last_delivered_end_ms
    if last_delivered_end_ms is None or until_start_ms <= last_delivered_end_ms:
        return
    for window_start_ms, window_end_ms in session_uniform_windows(
        state, last_delivered_end_ms, until_start_ms
    ):
        await resolve_unresolvable_window(state, window_start_ms, window_end_ms)


def is_unresolvable(bar: IbkrMinuteBar, *, interruption_touched: bool = False) -> bool:
    """Whether an emitted minute an interruption touched cannot be proven complete.

    A minute is *touched* when its contributions span connection generations
    (``spans_interruption``) or when the loop saw it open as delivery stopped
    (``interruption_touched``, ruling P9). Untouched minutes -- including the
    first minute of generation 1, which a mid-minute deploy leaves short
    (ruling R2) -- keep today's behaviour.
    """
    if not (bar.spans_interruption or interruption_touched):
        return False
    if bar.session_phase != "RTH":
        return True
    return bar.contribution_count is None or bar.contribution_count < RTH_CONTRIBUTIONS_PER_MINUTE
