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
from dataclasses import dataclass

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

    async def record(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        try:
            return await self.policy.record_event(event)
        except Exception as exc:
            raise MarketDataFeedError(
                f"continuity evidence for {self.symbol} could not be written: {exc}",
                reason="CONTINUITY_EVIDENCE_UNWRITABLE",
            ) from exc

    def event(self, kind: ContinuityEventKind, **fields: object) -> FeedContinuityEvent:
        return FeedContinuityEvent(
            kind=kind, feed_id=self.feed_id, symbol=self.symbol, observed_at_ms=now_ms_utc(), **fields
        )


def _healthy(client: IbkrClient) -> bool:
    if not client.is_connected() or client.connection_lost:
        return False
    monitor = get_monitor()
    return monitor is None or monitor.recovery_state == "HEALTHY"


async def wait_for_healthy(client: IbkrClient, state: ContinuityState) -> None:
    """Block until the socket is healthy again or the consumer's deadline passes."""
    last_delivered_end_ms = state.last_delivered_end_ms
    if last_delivered_end_ms is None:
        raise MarketDataFeedError(
            f"continuity wait for {state.symbol} has no delivered bar to anchor a deadline on"
        )
    deadline_ms = state.policy.deadline_ms(last_delivered_end_ms)
    while not _healthy(client):
        if now_ms_utc() >= deadline_ms:
            await state.record(
                state.event(
                    "refused",
                    reason="DECISION_BAR_MISSED",
                    last_delivered_end_ms=last_delivered_end_ms,
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


async def resolve_unresolvable_window(state: ContinuityState, window_start_ms: int, window_end_ms: int) -> None:
    """Rule 3 for one minute nothing real-time can prove complete: gap outside the session, refusal inside."""
    if not inside_decision_session(state, window_start_ms):
        await state.record(
            state.event(
                "gap",
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                last_delivered_end_ms=state.last_delivered_end_ms,
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


def is_unresolvable(bar: IbkrMinuteBar) -> bool:
    """An emitted minute is unresolvable iff it spans an interruption and cannot be proven complete by count."""
    if not getattr(bar, "spans_interruption", False):
        return False
    if bar.session_phase != "RTH":
        return True
    count = getattr(bar, "contribution_count", None)
    return count is None or count < RTH_CONTRIBUTIONS_PER_MINUTE
