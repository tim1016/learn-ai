"""IBKR-backed implementation of the broker-neutral MarketDataFeed port.

This module backs ``MarketDataFeed`` with the existing, proven IBKR bar path
(``app/broker/ibkr/bars.stream_minute_bars``).  It and its continuity helper
``ibkr_continuity.py`` are the **only** files in ``app/marketdata/`` that
import IBKR types; all other consumers depend only on the neutral port in
``feed.py``.

Architecture (phase-3 design §4 + #1258 L2 "one shared feed, in-process fan-out"):

* One ``IbkrMarketDataFeed`` instance lives in the data plane for the lifetime
  of the process.  All bots and consumers in the same container call
  ``stream_bars()`` on this shared instance.
* Fan-out is reference-counted per symbol via the existing
  ``_RealtimeBarSubscriptionRegistry`` in ``bars.py``.  N concurrent callers of
  ``stream_bars("SPY")`` share one ``reqRealTimeBars`` subscription; each gets
  every closed minute bar; the last caller's ``async for`` exit releases the
  underlying IBKR subscription.
* A connected but stalled ``reqRealTimeBars`` line is invalidated and
  transparently replaced. Other ``IBKRBarStreamError`` failures are re-raised
  as ``MarketDataFeedError`` so no IBKR type escapes the port.
* ``health()`` reads the IBKR client's connection signals synchronously.  A
  newly active feed fails closed until its first closed minute. Thereafter,
  raw 5-second source activity must advance within a configurable threshold
  (default: 30 seconds). Ordinary bar gaps remain non-fatal.
* ``IbkrMinuteBar → MarketDataBar`` translation happens here, at the boundary.
  The translated bar carries ``feed_id="ibkr"`` as provenance.

Temporal contract: all ``int64 ms UTC``; no ISO strings, no naive datetimes.
Temporal ban-list clean: no utcnow, no tz-naive now(), no pd.to_datetime
without utc.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass

from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarRequestDeadlineExceeded,
    IBKRBarStreamError,
    IBKRBarSubscriptionStalled,
    MinuteAssembler,
    fetch_historical_minute_bars,
    stream_minute_bars,
)
from app.broker.ibkr.client import BrokerError, IbkrClient, NotConnectedError
from app.broker.ibkr.minute_assembler import (
    RTH_CONTRIBUTIONS_PER_MINUTE,
    IBKRImpossibleBarError,
)
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.marketdata.feed import (
    IMPOSSIBLE_SOURCE_BAR,
    WARMUP_HISTORY_UNAVAILABLE,
    BarProvenanceTag,
    ContinuityPolicy,
    FeedHealth,
    MarketDataBar,
    MarketDataFeedError,
    warmup_window_start_ms,
)
from app.marketdata.ibkr_continuity import MINUTE_INCOMPLETE_REASON_CODE, ContinuityLoop, ResolvedBar
from app.utils.session_anchors import et_date_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_STALE_THRESHOLD_MS: int = 30_000


@dataclass
class _SymbolLiveness:
    """Mutable ingestion watermarks owned by one normalized symbol."""

    last_bar_ms: int | None = None
    last_bar_wall_ms: int | None = None
    last_source_ms: int | None = None
    last_source_wall_ms: int | None = None
    first_bar_seen: bool = False
    active_count: int = 0




def _owed_warmup_sessions(lookback_days: int, *, now_ms: int) -> list[tuple[int, int]]:
    """The regular sessions a ``lookback_days`` warmup must cover, as ``(open_ms, close_ms)``.

    The window is the ``lookback_days`` calendar days ending at ``now_ms``.
    Every NYSE session it touches is a candidate, from the canonical
    calendar (half-days keep their early close). A session is **owed** when
    the window holds at least half of it -- measured on the part that has
    already happened, so today's session is owed only once half of it has
    elapsed:

    * a session wholly inside the window is always owed, including one on
      the window's first ET date (a one-day warmup at 08:30 owes all of the
      previous session);
    * at the window's two edges, a session the window holds most of is
      owed, and a thin sliver is not. A sliver can legitimately come back
      bar-free -- the few minutes before a close the window starts in, or
      the first minutes of a session that just opened -- while a response
      holding no bar of a session the window mostly spans is truncated, not
      sparse. For a one-day warmup the two edge sessions together span one
      session, so one of them is always owed while the market is open.
    """
    window_start_ms = warmup_window_start_ms(lookback_days, now_ms=now_ms)
    owed: list[tuple[int, int]] = []
    for session in expected_sessions(et_date_at_ms(window_start_ms), et_date_at_ms(now_ms)):
        open_ms = session_open_ms_utc(session)
        close_ms = session_close_ms_utc(session)
        held_ms = min(close_ms, now_ms) - max(open_ms, window_start_ms)
        if 2 * held_ms >= close_ms - open_ms:
            owed.append((open_ms, close_ms))
    return owed


def require_warmup_coverage(
    bars: Sequence[MarketDataBar], *, lookback_days: int, now_ms: int
) -> None:
    """Refuse fetched warmup history that does not cover its lookback window (#2365).

    ``ib_async`` does not raise request errors by default
    (``RaiseRequestErrors`` is False): an error such as 162 (pacing, data
    farm) ends the request with whatever rows arrived -- often none -- and
    ``fetch_historical_minute_bars`` returns them as if complete. Without
    this check that short history would start the run on the bare
    indicator minimum instead of its sealed lookback.

    Coverage rule, per session: every owed session (see
    :func:`_owed_warmup_sessions`) must hold at least one regular-hours bar.
    Checking each session, not just the oldest bar, refuses a response that
    reaches the window's start but is missing sessions after it. The rule is
    session-level on purpose: a traded symbol prints somewhere in a regular
    session, but it may skip individual minutes, so demanding every minute
    would refuse ordinary history.

    Only ``RTH``-labelled bars count. The run fetches with ``use_rth=False``
    and filters by its own decision session afterwards; a regular-hours bar
    survives every run's filter (a declared extended window must enclose the
    regular session), while a pre- or post-market bar is dropped by a
    regular-hours run. Counting those would let the check pass on bars the
    strategy never replays.

    Applied to a fresh IBKR fetch only. A resumed run warms from its
    retained source-bar ledger (``_RetainedSourceBarFeed``), which replays
    exactly what its first start consumed -- that start passed this check --
    and the offline replay/qualification harnesses serve their pinned warmup
    by design; neither reaches this method.
    """
    rth_starts = sorted(bar.start_ms for bar in bars if bar.session_phase == "RTH")
    uncovered = [
        (open_ms, close_ms)
        for open_ms, close_ms in _owed_warmup_sessions(lookback_days, now_ms=now_ms)
        if bisect_left(rth_starts, open_ms) == bisect_left(rth_starts, close_ms)
    ]
    if not uncovered:
        return
    raise MarketDataFeedError(
        f"the {lookback_days}-day warmup history holds no regular-hours bar for "
        f"{len(uncovered)} owed session(s), the earliest opening at {uncovered[0][0]} "
        f"(regular-hours bars: {len(rth_starts)}, bars: {len(bars)})",
        reason=WARMUP_HISTORY_UNAVAILABLE,
    )


class IbkrMarketDataFeed:
    """IBKR-backed MarketDataFeed.

    Satisfies the ``MarketDataFeed`` Protocol structurally.

    Parameters
    ----------
    client:
        The data-plane's existing in-container ``IbkrClient``.  This feed
        uses it *read-only* — no order submission, no account queries.
    stale_threshold_ms:
        How long without raw source activity before ``health()`` reports
        ``stale=True``. Default is 30 seconds. A newly active subscription is
        stale until it emits its first closed minute.
    """

    feed_id: str = "ibkr"

    def __init__(
        self,
        client: IbkrClient,
        *,
        stale_threshold_ms: int = _STALE_THRESHOLD_MS,
    ) -> None:
        self._client = client
        self._stale_threshold_ms = stale_threshold_ms
        self._symbol_liveness: dict[str, _SymbolLiveness] = {}

    @property
    def capability_account_id(self) -> str | None:
        """Expose the IBKR account that owns this feed's session evidence."""
        return self._client.connected_account

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncGenerator[MarketDataBar, None]:
        """Yield closed 1-minute bars for ``symbol``.

        Reference-counted: N concurrent callers of the same symbol share one
        underlying ``reqRealTimeBars`` subscription.  The last caller's exit
        releases it.

        Raises ``MarketDataFeedError`` when the IBKR connection dies or the
        source violates a data invariant. A stalled request is replaced
        transparently. Closed-minute output gaps are non-fatal; source-heartbeat
        silence is evaluated against IBKR's documented one-bar-per-five-seconds
        ``reqRealTimeBars`` contract.

        ``continuity`` is how a caller that cannot miss a decision bar states
        its decision clock, its substitution authority and its evidence sink.
        With a policy, a survivable interruption (socket down, 1100 soft loss,
        stall, reconnect) is waited out under the caller's deadline and the
        open minute is stitched across the gap; every minute that cannot be
        proven complete fails the run closed rather than being delivered
        short. ``None`` — the default — keeps the pre-#1921 behavior, as does
        ``IBKR_FEED_CONTINUITY_ENABLED=false``.
        """
        normalized_symbol = symbol.upper()
        liveness = self._state_for(normalized_symbol)
        if liveness.active_count == 0:
            liveness.first_bar_seen = False
        liveness.active_count += 1
        logger.info(
            "MarketDataFeed consumer attached",
            extra={
                "action": "marketdata_consumer_attached",
                "feed_id": self.feed_id,
                "symbol": normalized_symbol,
                "use_rth": use_rth,
                "active_count": liveness.active_count,
            },
        )
        try:
            stream = (
                self._stream_bars_legacy(normalized_symbol, liveness, use_rth=use_rth)
                if continuity is None or self._continuity_disabled(normalized_symbol)
                else self._stream_bars_with_continuity(
                    normalized_symbol, liveness, use_rth=use_rth, policy=continuity
                )
            )
            # Closed explicitly, so a consumer that stops early releases the
            # broker line now rather than when the generator finalizer runs.
            async with aclosing(stream) as bars:
                async for bar in bars:
                    yield bar
        finally:
            liveness.active_count = max(0, liveness.active_count - 1)
            if liveness.active_count == 0:
                liveness.first_bar_seen = False
            logger.info(
                "MarketDataFeed consumer detached",
                extra={
                    "action": "marketdata_consumer_detached",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "active_count": liveness.active_count,
                },
            )

    def _continuity_disabled(self, symbol: str) -> bool:
        """Whether the kill switch refuses the policy this caller authored."""
        if self._client.settings.feed_continuity_enabled:
            return False
        logger.warning(
            "Feed continuity disabled by IBKR_FEED_CONTINUITY_ENABLED; "
            "failing fast on interruptions",
            extra={
                "action": "marketdata_continuity_disabled",
                "feed_id": self.feed_id,
                "symbol": symbol,
            },
        )
        return True

    async def _stream_bars_legacy(
        self,
        symbol: str,
        liveness: _SymbolLiveness,
        *,
        use_rth: bool,
    ) -> AsyncGenerator[MarketDataBar, None]:
        """Pre-#1921 delivery: replace a stalled line, fail fast on everything else.

        A short minute is never delivered as a complete one (#2364). The minute
        each attempt joined partway through -- including the first minute after
        a stall replacement -- is omitted as an ordinary gap; any other
        regular-session minute short of the calendar's count fails fast.
        """
        try:
            replacements = 0
            while True:
                # Per attempt: this path does not carry a minute across a
                # replaced subscription, and never did.
                assembler = MinuteAssembler()
                try:
                    async with aclosing(
                        stream_minute_bars(
                            self._client,
                            symbol,
                            use_rth=use_rth,
                            on_source_bar=lambda source_ms: self._observe_source_bar(
                                symbol,
                                source_ms,
                            ),
                            assembler=assembler,
                        )
                    ) as minute_bars:
                        async for ibkr_bar in minute_bars:
                            if not self._legacy_minute_is_deliverable(ibkr_bar, assembler):
                                continue
                            bar = self._translate(ibkr_bar)
                            liveness.last_bar_ms = bar.start_ms
                            liveness.last_bar_wall_ms = now_ms_utc()
                            liveness.first_bar_seen = True
                            yield bar
                    break
                except IBKRBarSubscriptionStalled as exc:
                    liveness.first_bar_seen = False
                    replacements += 1
                    logger.warning(
                        "Replacing stalled IBKR real-time-bar subscription",
                        extra={
                            "action": "marketdata_stalled_subscription_replaced",
                            "feed_id": self.feed_id,
                            "symbol": symbol,
                            "use_rth": use_rth,
                            "replacement_count": replacements,
                            "reason": str(exc),
                        },
                    )
        except (IBKRImpossibleBarError, IBKRBarStreamError, NotConnectedError) as exc:
            raise MarketDataFeedError(
                str(exc),
                reason=IMPOSSIBLE_SOURCE_BAR if isinstance(exc, IBKRImpossibleBarError) else None,
            ) from exc

    def _legacy_minute_is_deliverable(self, ibkr_bar: IbkrMinuteBar, assembler: MinuteAssembler) -> bool:
        """Omit a short join minute; fail fast on any other minute short of the calendar.

        Dispatches on ``MinuteAssembler.completeness`` exactly as the
        continuity path does. Nothing on this path observes an interruption,
        so ``touched`` is False; a minute whose prints span connection
        generations still classifies as touched through ``spans_interruption``.
        """
        completeness = assembler.completeness(ibkr_bar, touched=False)
        if completeness == "short_join":
            logger.warning(
                "Omitted the minute the IBKR stream joined partway through",
                extra={
                    "action": "marketdata_join_minute_omitted",
                    "feed_id": self.feed_id,
                    "symbol": ibkr_bar.symbol,
                    "window_start_ms": ibkr_bar.start_ms,
                    "contribution_count": ibkr_bar.contribution_count,
                },
            )
            return False
        if completeness == "unprovable":
            raise MarketDataFeedError(
                f"minute {ibkr_bar.start_ms}..{ibkr_bar.end_ms} for {ibkr_bar.symbol} holds "
                f"{ibkr_bar.contribution_count} of the {RTH_CONTRIBUTIONS_PER_MINUTE} 5-second "
                "prints a regular-session minute owes",
                reason=MINUTE_INCOMPLETE_REASON_CODE,
            )
        return True

    async def _stream_bars_with_continuity(
        self,
        symbol: str,
        liveness: _SymbolLiveness,
        *,
        use_rth: bool,
        policy: ContinuityPolicy,
    ) -> AsyncGenerator[MarketDataBar, None]:
        """Deliver under a ``ContinuityPolicy``: survive an interruption, or fail closed.

        The retry loop is all this method is. ``ContinuityLoop`` owns the rest:
        the one ``MinuteAssembler`` that outlives every resubscribe, so the
        minute open when the socket died is finished by the new one; the
        deadline the wait is held to; and the resolution of every minute the
        merge cannot prove complete — omitted as a ``gap`` outside the decision
        session, refused (fatally) inside it. "Inside" is the consumer's own
        session (``policy.session``): an RTH minute for a regular-hours run,
        any minute of the broker's declared window for an extended one.
        """
        loop = ContinuityLoop(
            client=self._client, feed_id=self.feed_id, symbol=symbol, policy=policy
        )

        def _on_source_bar(source_ms: int) -> None:
            self._observe_source_bar(symbol, source_ms)
            loop.observe_source_bar(source_ms)

        while True:
            try:
                async with aclosing(
                    stream_minute_bars(
                        self._client,
                        symbol,
                        use_rth=use_rth,
                        on_source_bar=_on_source_bar,
                        assembler=loop.assembler,
                        request_deadline_ms=loop.request_deadline_ms,
                    )
                ) as minute_bars:
                    async for ibkr_bar in minute_bars:
                        resolved = await loop.resolve_emitted(ibkr_bar)
                        if resolved is not None:
                            yield self._deliver(resolved, liveness)
                return
            except (IBKRBarInterrupted, IBKRBarSubscriptionStalled) as exc:
                await loop.open_interruption(exc)
                liveness.first_bar_seen = False
                await loop.await_recovery()
            except NotConnectedError as exc:
                await loop.await_recovery_after_race(exc)
            except IBKRBarRequestDeadlineExceeded as exc:
                await loop.refuse_request_past_deadline(exc)
            except IBKRImpossibleBarError as exc:
                raise MarketDataFeedError(str(exc), reason=IMPOSSIBLE_SOURCE_BAR) from exc
            except IBKRBarStreamError as exc:
                raise MarketDataFeedError(str(exc)) from exc

    def _deliver(self, resolved: ResolvedBar, liveness: _SymbolLiveness) -> MarketDataBar:
        """Translate one resolved minute at the port boundary and mark the feed live."""
        bar = self._translate(resolved.bar)
        if resolved.continuity_event_ref is not None:
            # The loop decided this minute was assembled across the
            # interruption; the generation set alone cannot see a
            # same-generation restore, so the port takes the loop's word.
            bar = bar.model_copy(
                update={
                    "provenance": "realtime_across_reconnect",
                    "continuity_event_ref": resolved.continuity_event_ref,
                }
            )
        liveness.last_bar_ms = bar.start_ms
        liveness.last_bar_wall_ms = now_ms_utc()
        liveness.first_bar_seen = True
        return bar

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        """Return closed 1-minute bars from the trailing ``lookback_days``
        calendar days, oldest first, via IBKR's read-only historical-data
        endpoint.

        Used only to warm up a strategy's indicator state before live
        decisions begin (a fresh RTH session alone can't warm
        ADX/EMA-class indicators with multi-day lookback periods) -- never
        itself treated as a decision.

        A fetch failure -- or a fetch that leaves a session the
        ``lookback_days`` window owes without a regular-hours bar (see
        :func:`require_warmup_coverage`) --
        raises ``MarketDataFeedError`` with reason
        ``WARMUP_HISTORY_UNAVAILABLE`` (#2365). Both used to let the run
        start cold, which silently replaced the sealed warmup lookback with
        the bare indicator minimum and changed which real orders the program
        placed. A run that cannot warm up as sealed does not start; the
        runner records the refusal under that reason.
        """
        normalized_symbol = symbol.upper()
        # Anchor the closed-bar cutoff before broker I/O. A request that starts
        # just before a minute boundary can finish just after it; sampling the
        # clock after the await would then misclassify IBKR's partial snapshot
        # of that minute as closed.
        requested_at_ms = now_ms_utc()
        try:
            historical = await fetch_historical_minute_bars(
                self._client,
                normalized_symbol,
                duration=f"{lookback_days} D",
                use_rth=use_rth,
            )
        except IBKRImpossibleBarError as exc:
            # A bar that cannot be real is corruption the retry cannot cure, so
            # it refuses under its own reason rather than the retryable
            # WARMUP_HISTORY_UNAVAILABLE a failed fetch carries (#2444).
            logger.error(
                "Historical warmup bars hold a bar that cannot be real; refusing to start",
                extra={
                    "action": "warmup_impossible_bar",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "lookback_days": lookback_days,
                    "error": str(exc),
                },
            )
            raise MarketDataFeedError(
                f"the {lookback_days}-day warmup history for {normalized_symbol} holds "
                f"a bar that cannot be real: {exc}",
                reason=IMPOSSIBLE_SOURCE_BAR,
            ) from exc
        except (IBKRBarStreamError, BrokerError, ValueError) as exc:
            # BrokerError covers NotConnectedError and a contract that cannot
            # be qualified; the same set the go-live IBKR bar check refuses on.
            logger.error(
                "Historical warmup bars unavailable; refusing to start the run cold",
                extra={
                    "action": "warmup_bars_unavailable",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "lookback_days": lookback_days,
                    "error": str(exc),
                },
            )
            raise MarketDataFeedError(
                f"the {lookback_days}-day warmup history for {normalized_symbol} "
                f"could not be fetched: {exc}",
                reason=WARMUP_HISTORY_UNAVAILABLE,
            ) from exc
        bars = [self._translate(bar) for bar in historical]
        # IBKR's historical endpoint includes the still-forming minute as its
        # last row. A forming bar is not a closed observation: sealing it into
        # the source-bar ledger makes the later live close of the same window
        # a SOURCE_BAR_IDENTITY_CONFLICT (fleet run 2026-08-25 — every bot
        # deployed mid-minute crashed on its first live bar).
        closed = [bar for bar in bars if bar.end_ms <= requested_at_ms]
        if len(closed) < len(bars):
            logger.info(
                "Dropped forming bar(s) from historical warmup",
                extra={
                    "action": "warmup_forming_bars_dropped",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "dropped": len(bars) - len(closed),
                },
            )
        try:
            require_warmup_coverage(closed, lookback_days=lookback_days, now_ms=requested_at_ms)
        except MarketDataFeedError as exc:
            logger.error(
                "Historical warmup bars do not cover the sealed lookback; refusing to start the run cold",
                extra={
                    "action": "warmup_bars_short",
                    "feed_id": self.feed_id,
                    "symbol": normalized_symbol,
                    "lookback_days": lookback_days,
                    "bars": len(closed),
                    "error": str(exc),
                },
            )
            raise
        return closed

    def active_symbols(self) -> tuple[str, ...]:
        """Symbols with bar consumers, including runners between decisions."""
        return tuple(symbol for symbol, state in self._symbol_liveness.items() if state.active_count > 0)

    def health(self, symbol: str | None = None) -> FeedHealth:
        """Return aggregate or symbol-scoped point-in-time feed health."""
        connected = self._client.is_connected() and not self._client.connection_lost
        now = now_ms_utc()
        stale = False
        reason = ""
        normalized_symbol = symbol.upper() if symbol is not None else None
        states = (
            [(normalized_symbol, self._symbol_liveness.get(normalized_symbol))]
            if normalized_symbol is not None
            else list(self._symbol_liveness.items())
        )
        present_states = [(name, state) for name, state in states if state is not None]
        active_states = [
            (name, state)
            for name, state in present_states
            if state.active_count > 0
        ]
        active_count = sum(state.active_count for _, state in present_states)
        last_bar_ms = max(
            (state.last_bar_ms for _, state in present_states if state.last_bar_ms is not None),
            default=None,
        )

        if not connected:
            reason = "IBKR connection lost"
        elif missing_first := [name for name, state in active_states if not state.first_bar_seen]:
            stale = True
            reason = (
                f"Active IBKR feed for {', '.join(missing_first)} has not produced "
                "its first closed bar"
            )
        else:
            stale_ages = [
                (name, now - freshness_wall_ms)
                for name, state in active_states
                if (freshness_wall_ms := state.last_source_wall_ms or state.last_bar_wall_ms)
                is not None
                and now - freshness_wall_ms >= self._stale_threshold_ms
            ]
            if stale_ages:
                stale = True
                stale_names = ", ".join(name for name, _ in stale_ages)
                oldest_age_ms = max(age_ms for _, age_ms in stale_ages)
                reason = (
                    f"No source bar for {stale_names} in {oldest_age_ms // 1000}s "
                    f"(threshold {self._stale_threshold_ms // 1000}s)"
                )

        return FeedHealth(
            connected=connected,
            stale=stale,
            last_bar_ms=last_bar_ms,
            reason=reason,
            active_subscription_count=active_count,
            observed_at_ms=now,
        )

    def _state_for(self, symbol: str) -> _SymbolLiveness:
        """Return the one mutable liveness record for ``symbol``."""
        return self._symbol_liveness.setdefault(symbol.upper(), _SymbolLiveness())

    def _observe_source_bar(self, symbol: str, source_ms: int) -> None:
        """Advance the raw-source liveness watermark at the ingestion edge."""
        state = self._state_for(symbol)
        state.last_source_ms = source_ms
        state.last_source_wall_ms = now_ms_utc()

    @staticmethod
    def _translate(ibkr_bar: IbkrMinuteBar) -> MarketDataBar:
        """Map an IbkrMinuteBar to the neutral MarketDataBar at the boundary."""
        if ibkr_bar.provenance == "ibkr_historical":
            provenance: BarProvenanceTag = "history"
        elif ibkr_bar.spans_interruption:
            provenance = "realtime_across_reconnect"
        else:
            provenance = "realtime"
        return MarketDataBar(
            symbol=ibkr_bar.symbol,
            start_ms=ibkr_bar.start_ms,
            end_ms=ibkr_bar.end_ms,
            open=ibkr_bar.open,
            high=ibkr_bar.high,
            low=ibkr_bar.low,
            close=ibkr_bar.close,
            volume=ibkr_bar.volume,
            fetched_at_ms=ibkr_bar.fetched_at_ms,
            feed_id="ibkr",
            session_phase=ibkr_bar.session_phase,
            provenance=provenance,
        )


# ---------------------------------------------------------------------------
# Process-level singleton — installed at startup in main.py.
# ---------------------------------------------------------------------------

_FEED: IbkrMarketDataFeed | None = None


def get_market_data_feed() -> IbkrMarketDataFeed | None:
    """Return the process-level shared feed, or ``None`` when not installed."""
    return _FEED


def set_market_data_feed(feed: IbkrMarketDataFeed | None) -> None:
    """Install (or clear) the process-level shared feed.

    Called once at startup in ``main.py`` after the IBKR client is
    connected, and once at shutdown.
    """
    global _FEED
    _FEED = feed
