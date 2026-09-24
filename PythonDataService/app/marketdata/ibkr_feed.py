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
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from datetime import timedelta

from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.broker.ibkr.bars import (
    IBKRBarInterrupted,
    IBKRBarStreamError,
    IBKRBarSubscriptionStalled,
    MinuteAssembler,
    fetch_historical_minute_bars,
    stream_minute_bars,
)
from app.broker.ibkr.client import BrokerError, IbkrClient, NotConnectedError
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.marketdata.feed import (
    WARMUP_HISTORY_UNAVAILABLE,
    BarProvenanceTag,
    ContinuityPolicy,
    FeedHealth,
    MarketDataBar,
    MarketDataFeedError,
)
from app.marketdata.ibkr_continuity import ContinuityLoop, ResolvedBar
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


def _earliest_owed_warmup_session_close_ms(lookback_days: int, *, now_ms: int) -> int | None:
    """Close of the oldest session a ``lookback_days`` warmup must reach, or ``None``.

    The window is the ``lookback_days`` calendar days ending at ``now_ms``.
    The ET date the window starts on is excluded, because a calendar-day
    duration can begin partway through it (after that day's close, even).
    The owed sessions are the NYSE sessions strictly after that date that
    have already opened by ``now_ms``, from the canonical calendar. ``None``
    means the window owes no session -- e.g. a one-day lookback before the
    open -- so an empty history is then a true "nothing to warm on".
    """
    now_date = et_date_at_ms(now_ms)
    first_day = et_date_at_ms(now_ms - lookback_days * 86_400_000) + timedelta(days=1)
    if first_day > now_date:
        return None
    owed = [
        session
        for session in expected_sessions(first_day, now_date)
        if session_open_ms_utc(session) <= now_ms
    ]
    return session_close_ms_utc(owed[0]) if owed else None


def require_warmup_coverage(
    bars: Sequence[MarketDataBar], *, lookback_days: int, now_ms: int
) -> None:
    """Refuse fetched warmup history that does not reach its lookback window (#2365).

    ``ib_async`` does not raise request errors by default
    (``RaiseRequestErrors`` is False): an error such as 162 (pacing, data
    farm) ends the request with whatever rows arrived -- often none -- and
    ``fetch_historical_minute_bars`` returns them as if complete. Without
    this check that short history would start the run on the bare
    indicator minimum instead of its sealed lookback.

    Coverage rule: when the window owes at least one session, some bar must
    start before the close of the *earliest* owed session -- the history
    reaches into (or past) it. The exact first minute is not required, so a
    vendor window that opens a few minutes late is not refused. Empty
    history, or history covering only later sessions, is refused.

    Applied to a fresh IBKR fetch only. A resumed run warms from its
    retained source-bar ledger (``_RetainedSourceBarFeed``), which replays
    exactly what its first start consumed -- that start passed this check --
    and the offline replay/qualification harnesses serve their pinned warmup
    by design; neither reaches this method.
    """
    owed_close_ms = _earliest_owed_warmup_session_close_ms(lookback_days, now_ms=now_ms)
    if owed_close_ms is None:
        return
    oldest_start_ms = min((bar.start_ms for bar in bars), default=None)
    if oldest_start_ms is not None and oldest_start_ms < owed_close_ms:
        return
    raise MarketDataFeedError(
        f"the {lookback_days}-day warmup history does not reach the session closing at "
        f"{owed_close_ms} (oldest bar start: {oldest_start_ms}, bars: {len(bars)})",
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
        """Pre-#1921 delivery: replace a stalled line, fail fast on everything else."""
        try:
            replacements = 0
            while True:
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
                            # Per attempt: this path does not carry a minute across a
                            # replaced subscription, and never did.
                            assembler=MinuteAssembler(),
                        )
                    ) as minute_bars:
                        async for ibkr_bar in minute_bars:
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
        except (IBKRBarStreamError, NotConnectedError) as exc:
            raise MarketDataFeedError(str(exc)) from exc

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

        A fetch failure -- or a fetch that ends without reaching the
        ``lookback_days`` window (see :func:`require_warmup_coverage`) --
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
                "Historical warmup bars do not reach the sealed lookback; refusing to start the run cold",
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
