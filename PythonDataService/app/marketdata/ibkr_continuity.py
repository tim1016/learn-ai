"""Continuity for IbkrMarketDataFeed.stream_bars (spec #1921 §4.2 rules 3, 7, 9).

The feed keeps the generator and the ``IbkrMinuteBar -> MarketDataBar``
translation at the port boundary; :class:`ContinuityLoop` owns everything
between them --- the one interruption a stream can have in flight, the wait
under the consumer's deadline, the resolution of the minutes that interruption
touched or swallowed, and the awaited event emission that gates all of it. No
historical substitution exists here: a granted window is refused with
``SUBSTITUTION_PATH_UNAVAILABLE`` (controller ruling R3).

Every temporal value is ``int64 ms UTC``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.broker.ibkr.auto_reconnect_monitor import get_monitor
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.broker.ibkr.bars import (
    IBKRBarRequestDeadlineExceeded,
    IBKRBarStreamError,
    IBKRBarSubscriptionStalled,
)
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.minute_assembler import MinuteAssembler
from app.marketdata.feed import (
    ContinuityEventKind,
    ContinuityEventRef,
    ContinuityPolicy,
    FeedContinuityEvent,
    GapCause,
    InterruptionCause,
    MarketDataFeedError,
    SubstitutionGrant,
    SubstitutionRefusal,
    record_continuity_event,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

WAIT_POLL_S = 0.25
SOURCE_BAR_MS = 60_000

MINUTE_INCOMPLETE_REASON_CODE = "MINUTE_INCOMPLETE"
"""Why a stream ended on a minute no interruption touched: a regular-session
minute held fewer 5-second prints than the calendar says it owes (#2364). One
fact, one code, on both feed paths -- an untouched short minute is not an
interruption episode, so no substitution grant is asked about it."""


@dataclass
class _OpenInterruption:
    """The one interruption a stream can have in flight, and all of its state.

    Held from the moment the deadline is anchored until a bar is *actually
    delivered*: the three facts below open and close together, so there is no
    combination of them that has to be guarded against.
    """

    #: Deadline this interruption recorded, and the only one any wait for it
    #: may enforce.
    deadline_ms: int
    #: The minute the assembler still held open when delivery stopped, when the
    #: interruption outlived it. Such a minute emits holding one generation's
    #: contributions, and nothing on the bar records that it was cut short --
    #: whether an interruption *touched* it is a fact only this loop can see
    #: (ruling P9). ``None`` when there was none -- a minute complete by count
    #: was already emitted by the assembler before delivery stopped.
    touched_minute_start_ms: int | None = None
    #: The recovery that ended the wait -- and, by being set at all, the fact
    #: that the next emitted bar must be scanned for wholly-missed minutes
    #: behind it. A gap no interruption explains is an ordinary gap, and
    #: ordinary gaps are non-fatal (ruling P11).
    recovered_ref: ContinuityEventRef | None = None
    #: The minute the first post-recovery source bar landed in (spec §4.2
    #: rule 4: that bar's timestamp decides which minute the live stream can
    #: complete). Every print of that minute before the landing was lost to
    #: the interruption, and nothing on the bar records it -- like the open
    #: minute, it holds one generation's contributions and ``spans_interruption``
    #: reads false. ``None`` until the resubscribed line delivers.
    landing_minute_start_ms: int | None = None

    def touches(self, minute_start_ms: int) -> bool:
        """Whether this interruption cut ``minute_start_ms`` short at either end."""
        return minute_start_ms in (self.touched_minute_start_ms, self.landing_minute_start_ms)


@dataclass(frozen=True)
class ResolvedBar:
    """One assembled minute the loop will let through, and what explains it.

    ``continuity_event_ref`` is the ``recovered`` event a bar assembled across
    the interruption is evidence of -- its contributions span connection
    generations, or the loop saw the interruption cut it open or land in it --
    and it is what makes the bar ``realtime_across_reconnect`` at the port.
    ``None`` for a bar no interruption touched.
    """

    bar: IbkrMinuteBar
    continuity_event_ref: str | None = None


def _healthy(client: IbkrClient) -> bool:
    if not client.is_connected() or client.connection_lost:
        return False
    monitor = get_monitor()
    return monitor is None or monitor.recovery_state == "HEALTHY"


def _interruption_cause(exc: IBKRBarStreamError) -> InterruptionCause:
    """Name the survivable failure in the port's vocabulary."""
    return "stall" if isinstance(exc, IBKRBarSubscriptionStalled) else exc.cause


@dataclass
class ContinuityLoop:
    """One ``stream_bars`` call's continuity contract and the choreography it owns.

    The feed's retry loop resubscribes and yields; every ordering rule the
    spec pins lives here. Ruling P6 -- a minute already complete when the
    socket died is a delivered bar, and the deadline derives from it -- now
    holds by construction: the assembler emits a minute on its twelfth print
    (#2345), so it was delivered before the interruption opened. Spec §4.2
    rule 9 -- no bar reaches the consumer before the evidence explaining it --
    is kept by :meth:`open_interruption` recording the ``interruption`` before
    the loop resubscribes, so every bar after it follows its evidence.
    """

    client: IbkrClient
    feed_id: str
    symbol: str
    policy: ContinuityPolicy
    #: One assembler outlives every resubscribe, so the minute open when the
    #: socket died is finished by the socket that replaces it.
    assembler: MinuteAssembler = field(default_factory=MinuteAssembler, init=False)
    last_delivered_end_ms: int | None = field(default=None, init=False)
    generation: int = field(init=False)
    interruption: _OpenInterruption | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.generation = self.client.connection_generation

    # -- the three steps the feed's retry loop drives -----------------------

    async def resolve_emitted(self, ibkr_bar: IbkrMinuteBar) -> ResolvedBar | None:
        """Return the deliverable form of one assembled minute, or ``None``.

        Until the interruption is closed out by a bar that is actually
        delivered, minutes it swallowed whole -- everything between the last
        delivered bar and this one -- are resolved first and in order, then the
        bar itself. ``None`` means the minute was omitted as a recorded gap; an
        unresolvable minute inside the decision session raises instead.
        """
        interruption = self.interruption
        if interruption is not None and interruption.recovered_ref is not None:
            await self._resolve_missed_windows(ibkr_bar.start_ms)
        touched = interruption is not None and interruption.touches(ibkr_bar.start_ms)
        # One classifier for both feed paths (``MinuteAssembler.completeness``):
        # a touched minute proves itself by count alone, in every session
        # phase; an untouched one owes the calendar its prints (#2364).
        completeness = self.assembler.completeness(ibkr_bar, touched=touched)
        if completeness == "short_join":
            # The stream joined this minute partway through (ruling R2): its
            # earlier prints were never seen, so it is short, but nothing was
            # promised before it -- a gap, never a decision input.
            await self._record_gap(
                ibkr_bar.start_ms,
                ibkr_bar.end_ms,
                contribution_count=ibkr_bar.contribution_count,
                cause="stream_joined",
            )
            return None
        if completeness == "unprovable":
            if touched or ibkr_bar.spans_interruption:
                await self._resolve_unresolvable_episode(ibkr_bar)
            else:
                # A regular-session minute short of the calendar with no
                # interruption behind it: no episode, no substitution question.
                await self._resolve_unresolvable_window(
                    ibkr_bar.start_ms,
                    ibkr_bar.end_ms,
                    contribution_count=ibkr_bar.contribution_count,
                    refusal_reason=MINUTE_INCOMPLETE_REASON_CODE,
                )
            # The interruption stays open: this bar was omitted, not delivered,
            # so whatever it swallowed behind the *next* bar has yet to be
            # resolved (ruling P14). Closing it here would lose every minute
            # between an omitted gap and the next real bar -- reachable
            # whenever an interruption straddles the session open, since the
            # run streams with use_rth=False and sees pre-market minutes before
            # its first RTH one.
            return None
        recovered_ref = interruption.recovered_ref if interruption is not None else None
        # The loop's fact, not only the generation set: a minute the
        # interruption cut open or landed in was assembled across it even when
        # every print came over one socket generation (a 1100 -> 1102 restore).
        explained_by = (
            recovered_ref.ref()
            if recovered_ref is not None and (touched or ibkr_bar.spans_interruption)
            else None
        )
        # Delivered: the interruption is closed out and a later gap is an
        # ordinary gap again.
        self.interruption = None
        self.last_delivered_end_ms = ibkr_bar.end_ms
        return ResolvedBar(bar=ibkr_bar, continuity_event_ref=explained_by)

    async def open_interruption(self, exc: IBKRBarStreamError) -> None:
        """Anchor the deadline and record the interruption.

        Ruling P6: a minute already complete when the socket died moves the
        watermark the deadline derives from. The assembler emits such a minute
        on its twelfth print, so it has already been delivered and the
        watermark already moved; the open minute, if any, is short and becomes
        the one the interruption touched. A sink that cannot be written raises
        out of this call before the loop resubscribes, so no later bar can
        precede the evidence explaining it (spec §4.2 rule 9).
        """
        if self.last_delivered_end_ms is None:
            if self.assembler.open_minute_start_ms is None:
                # Rule 6: nothing delivered and nothing part-assembled, so
                # there is no continuity to preserve. Fail as today.
                raise MarketDataFeedError(str(exc)) from exc
            self.last_delivered_end_ms = self.assembler.open_minute_start_ms
        self.interruption = interruption = _OpenInterruption(
            deadline_ms=self.policy.deadline_ms(self.last_delivered_end_ms),
            # The interruption outlived the open minute: ruling P9's fact,
            # which only this loop can see.
            touched_minute_start_ms=self.assembler.open_minute_start_ms,
        )
        await self._record_interruption(interruption, _interruption_cause(exc))

    async def await_recovery(self) -> None:
        """Wait the open interruption out under its own deadline, then record it."""
        interruption = self.interruption
        if interruption is None:
            raise MarketDataFeedError(
                f"{self.symbol} was asked to recover with no interruption open"
            )
        await self._wait_for_healthy(deadline_ms=interruption.deadline_ms)
        new_generation = self.client.connection_generation
        interruption.recovered_ref = await self._record(
            self._event(
                "recovered",
                generation_from=self.generation,
                generation_to=new_generation,
                last_delivered_end_ms=self.last_delivered_end_ms,
            )
        )
        logger.info(
            "Feed continuity recovered",
            extra={
                "action": "marketdata_interruption_recovered",
                "feed_id": self.feed_id,
                "symbol": self.symbol,
                "generation_to": new_generation,
            },
        )
        self.generation = new_generation

    async def await_recovery_after_race(self, exc: Exception) -> None:
        """Absorb a resubscribe that raced the reconnect.

        The socket the recovery just reported healthy was gone again by the
        time the resubscribe reached it. To a reader following the journal's
        generations that is a second interruption -- the generation the
        ``recovered`` event named died without delivering -- so it is recorded
        as one and waited out like one. It stays under the deadline the first
        interruption anchored: nothing was delivered in between, so the
        consumer's next decision has not moved. With no interruption open this
        is an ordinary connection failure and stays fatal, as today.
        """
        interruption = self.interruption
        if interruption is None:
            raise MarketDataFeedError(str(exc)) from exc
        await self._record_interruption(interruption, "socket_down")
        await self.await_recovery()

    @property
    def request_deadline_ms(self) -> int | None:
        """The deadline a resubscribe's line request must not outwait, if one is open.

        The socket being healthy is not the line being back: the fresh request
        still waits on the shared 60-per-600 s pacer, and that wait counts
        against the same consumer deadline (#2397 review). ``None`` with no
        interruption open -- a first subscribe keeps today's unbounded wait.
        """
        return None if self.interruption is None else self.interruption.deadline_ms

    async def refuse_request_past_deadline(self, exc: IBKRBarRequestDeadlineExceeded) -> None:
        """End the run with ``refused`` when the resubscribe could not be requested in time."""
        interruption = self.interruption
        if interruption is None:
            raise MarketDataFeedError(str(exc)) from exc
        await self._refuse_decision_bar_missed(interruption.deadline_ms)

    def observe_source_bar(self, source_ms: int) -> None:
        """Note where the resubscribed line landed (spec §4.2 rule 4).

        Called for every raw 5-second bar that advances the source watermark.
        The first one after a recovery names the landing minute: every print
        of that minute before ``source_ms`` was lost to the interruption, so
        the minute is touched and must prove itself by count exactly like the
        open minute the interruption found. Bars before any recovery, and
        every bar after the first, carry no continuity fact.
        """
        interruption = self.interruption
        if (
            interruption is None
            or interruption.recovered_ref is None
            or interruption.landing_minute_start_ms is not None
        ):
            return
        interruption.landing_minute_start_ms = source_ms - source_ms % SOURCE_BAR_MS

    # -- evidence -----------------------------------------------------------

    async def _record(self, event: FeedContinuityEvent) -> ContinuityEventRef:
        return await record_continuity_event(self.policy, event)

    async def _record_interruption(
        self, interruption: _OpenInterruption, cause: InterruptionCause
    ) -> None:
        """Record ``interruption`` under the deadline it anchored, then say so."""
        await self._record(
            self._event(
                "interruption",
                cause=cause,
                generation_from=self.generation,
                last_delivered_end_ms=self.last_delivered_end_ms,
                deadline_ms=interruption.deadline_ms,
            )
        )
        logger.warning(
            "Feed interrupted; attempting same-run continuity",
            extra={
                "action": "marketdata_interruption_observed",
                "feed_id": self.feed_id,
                "symbol": self.symbol,
                "cause": cause,
            },
        )

    def _event(self, kind: ContinuityEventKind, **fields: object) -> FeedContinuityEvent:
        return FeedContinuityEvent(
            kind=kind, feed_id=self.feed_id, symbol=self.symbol, observed_at_ms=now_ms_utc(), **fields
        )

    # -- the wait -----------------------------------------------------------

    async def _wait_for_healthy(self, *, deadline_ms: int) -> None:
        """Block until the socket is healthy again or ``deadline_ms`` passes.

        The deadline is the consumer's, computed once when the interruption was
        observed and recorded on that ``interruption`` event, so the wait the
        journal describes is the wait that is actually enforced. Re-deriving it
        here from the live ``last_delivered_end_ms`` would silently extend it by
        a whole decision interval whenever the minute flushed after that event
        crossed a decision trigger -- and the journal would still claim the
        shorter one.

        The deadline is checked before the socket's health, not only while it
        is unhealthy. A line that is healthy again by the time the wait begins
        -- a stall took 60 s to detect, or the process stall behind #1921
        delayed the detection -- has still missed the decision if the deadline
        has passed; the deadline is the consumer's, not the socket's.
        """
        while True:
            if now_ms_utc() >= deadline_ms:
                await self._refuse_decision_bar_missed(deadline_ms)
            if _healthy(self.client):
                return
            await asyncio.sleep(WAIT_POLL_S)

    async def _refuse_decision_bar_missed(self, deadline_ms: int) -> None:
        """Record ADR 0053's ``refused`` for a missed deadline, then end the run."""
        await self._record(
            self._event(
                "refused",
                reason="DECISION_BAR_MISSED",
                last_delivered_end_ms=self.last_delivered_end_ms,
                deadline_ms=deadline_ms,
            )
        )
        logger.error(
            "Feed continuity refused: decision bar missed",
            extra={
                "action": "marketdata_continuity_refused",
                "symbol": self.symbol,
                "reason": "DECISION_BAR_MISSED",
                "deadline_ms": deadline_ms,
            },
        )
        raise MarketDataFeedError(
            f"{self.symbol} was not recovered before {deadline_ms}", reason="DECISION_BAR_MISSED"
        )

    # -- resolving what the interruption cost -------------------------------

    def _inside_decision_session(self, window_start_ms: int) -> bool:
        """Whether the consumer's decision clock treats ``window_start_ms`` as decidable.

        The consumer's own session answers, so this floor covers exactly the
        minutes the run decides on — a regular-hours run's RTH minutes, and an
        extended run's whole declared window. Asking anything narrower would
        journal a swallowed pre-market minute as a survivable gap and let an
        extended run keep deciding on a series with a hole in it.
        """
        return self.policy.session.includes_instant(window_start_ms)

    def _session_uniform_windows(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        """Split ``[start_ms, end_ms)`` into the fewest windows of one session verdict each.

        Contiguous unresolvable minutes are one window, not N (ruling P11): a
        consumer that may one day authorize a substitution needs to be asked
        about the *episode*, and a reader of the journal needs one fact per
        episode. The only reason to split is that ``_inside_decision_session``
        disagrees across the range -- a window straddling the RTH open is half a
        refusal and half a gap, so it is cut at the boundary.
        """
        windows: list[tuple[int, int]] = []
        run_start: int | None = None
        run_inside = False
        for minute_start_ms in range(start_ms, end_ms, SOURCE_BAR_MS):
            inside = self._inside_decision_session(minute_start_ms)
            if run_start is None:
                run_start, run_inside = minute_start_ms, inside
            elif inside != run_inside:
                windows.append((run_start, minute_start_ms))
                run_start, run_inside = minute_start_ms, inside
        if run_start is not None:
            windows.append((run_start, end_ms))
        return windows

    async def _resolve_unresolvable_episode(self, ibkr_bar: IbkrMinuteBar) -> None:
        """Resolve a short emitted minute together with every minute missed behind it.

        The assembler emits the minute an interruption cut short when the
        first post-recovery print lands in a later minute -- possibly several
        minutes on -- or, outside RTH, when the sparse-minute timer emits it
        with no later minute open yet (#2376; the minutes missed after it are
        then a later window of the same interruption). The wholly-missed
        minutes in between are the same episode
        as the short one, and the coalescing rule (P11) wants one fact per
        episode, so the window runs from this bar's start to the minute the
        assembler now holds open, split only at the decision-session boundary.
        The count travels only with a window that is exactly this emitted
        minute; a coalesced window has no single count (P12).
        """
        open_minute_start_ms = self.assembler.open_minute_start_ms
        episode_end_ms = max(ibkr_bar.end_ms, open_minute_start_ms or ibkr_bar.end_ms)
        for window_start_ms, window_end_ms in self._session_uniform_windows(
            ibkr_bar.start_ms, episode_end_ms
        ):
            is_this_minute = (window_start_ms, window_end_ms) == (ibkr_bar.start_ms, ibkr_bar.end_ms)
            await self._resolve_unresolvable_window(
                window_start_ms,
                window_end_ms,
                contribution_count=ibkr_bar.contribution_count if is_this_minute else None,
            )

    async def _resolve_missed_windows(self, until_start_ms: int) -> None:
        """Resolve every minute an interruption swallowed whole, coalesced by session verdict.

        Runs only for the first emitted bar after a recorded interruption. A gap
        with no interruption behind it is an ordinary gap, which the port
        promises is non-fatal (spec §6) -- scanning on every bar would make one
        fatal.
        """
        last_delivered_end_ms = self.last_delivered_end_ms
        if last_delivered_end_ms is None or until_start_ms <= last_delivered_end_ms:
            return
        for window_start_ms, window_end_ms in self._session_uniform_windows(
            last_delivered_end_ms, until_start_ms
        ):
            await self._resolve_unresolvable_window(window_start_ms, window_end_ms)

    async def _record_gap(
        self,
        window_start_ms: int,
        window_end_ms: int,
        *,
        contribution_count: int | None,
        cause: GapCause | None = None,
    ) -> None:
        """Omit a window nothing can prove complete, as a recorded ``gap``, and move past it.

        ``cause`` names a gap that is not an unresolvable window outside the
        decision session -- the only other origin today is the minute the
        stream joined (``"stream_joined"``), which can fall inside it.
        """
        await self._record(
            self._event(
                "gap",
                cause=cause,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                last_delivered_end_ms=self.last_delivered_end_ms,
                contribution_count=contribution_count,
            )
        )
        logger.warning(
            "Feed continuity omitted a minute it cannot prove complete",
            extra={
                "action": "marketdata_gap_omitted",
                "symbol": self.symbol,
                "window_start_ms": window_start_ms,
                "contribution_count": contribution_count,
                "cause": cause,
            },
        )
        self.last_delivered_end_ms = window_end_ms

    async def _resolve_unresolvable_window(
        self,
        window_start_ms: int,
        window_end_ms: int,
        *,
        contribution_count: int | None = None,
        refusal_reason: str | None = None,
    ) -> None:
        """Rule 3 for one window nothing real-time can prove complete: gap outside the session, refusal inside.

        ``contribution_count`` is how many 5-second contributions the emitted
        minute actually held, when the window is one such minute. A window
        nothing was ever assembled for carries ``None`` -- absent is a different
        fact from zero, and a coalesced multi-minute window has no single count.
        ``refusal_reason`` is set when the window's cause already names the
        refusal (``MINUTE_INCOMPLETE``); otherwise the substitution grant is
        asked, because the window belongs to an interruption episode.
        """
        if not self._inside_decision_session(window_start_ms):
            await self._record_gap(window_start_ms, window_end_ms, contribution_count=contribution_count)
            return
        reason = refusal_reason or self._substitution_refusal_reason(window_start_ms, window_end_ms)
        await self._record(
            self._event(
                "refused",
                reason=reason,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
                last_delivered_end_ms=self.last_delivered_end_ms,
                contribution_count=contribution_count,
            )
        )
        logger.error(
            "Feed continuity refused: minute cannot be proven complete",
            extra={"action": "marketdata_continuity_refused", "symbol": self.symbol, "reason": reason,
                   "window_start_ms": window_start_ms},
        )
        raise MarketDataFeedError(
            f"minute {window_start_ms}..{window_end_ms} for {self.symbol} cannot be proven complete",
            reason=reason,
        )

    def _substitution_refusal_reason(self, window_start_ms: int, window_end_ms: int) -> str:
        """Ask the grant author about one interruption window; every answer is a refusal (R3)."""
        verdict = self.policy.substitution_grant(window_start_ms, window_end_ms)
        reason = verdict.reason if isinstance(verdict, SubstitutionRefusal) else "SUBSTITUTION_PATH_UNAVAILABLE"
        if isinstance(verdict, SubstitutionGrant):
            logger.error(
                "A substitution grant was offered but no substitution path exists (fail closed)",
                extra={"action": "marketdata_continuity_refused", "symbol": self.symbol, "reason": reason},
            )
        return reason
