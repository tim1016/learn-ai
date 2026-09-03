"""Real-time underlying minute bars from IBKR.

IBKR's ``reqRealTimeBars`` emits 5-second TRADES bars. This module owns the
broker side of that feed — the shared-subscription registry, the liveness
gate, the historical fetch, and the streams — and folds 5-second bars into
closed 1-minute bars through
:class:`~app.broker.ibkr.minute_assembler.MinuteAssembler`, enforcing the
repo's timestamp policy at the ingestion boundary: every yielded model uses
``int64`` ms UTC.

All same-process consumers for the same ``(client, connection generation,
contract, whatToShow, useRTH)`` tuple share one underlying
``reqRealTimeBars`` subscription; a reconnect fences the old line off. The
registry reference-counts consumers and cancels the broker subscription only
when the last consumer leaves. New subscriptions are paced at IBKR's
documented ceiling of 60 requests per 600 seconds. ``ib_async`` separately
throttles ordinary socket messages at the 45-per-second rate pinned by
``IbkrClient``, below the default 50 requests/second connection limit.

The aggregation primitives — ``MinuteAssembler``, ``aggregate_realtime_bar``,
``LiveBarCounters``, ``IBKRBarStreamError``, and the ``DuplicatePolicy``
contract they implement — live in ``app.broker.ibkr.minute_assembler`` (#1921)
so an interruption that ends a stream call does not discard the open minute.
Import them from there; this module imports only the ones it uses itself.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Literal, NamedTuple

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.minute_assembler import (
    IBKRBarStreamError,
    MinuteAssembler,
    _bar_time_ms,
    _contribution,
    _session_phase_for_ms,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

NO_BAR_WARNING_INITIAL_INTERVAL_S = 30.0
NO_BAR_WARNING_MAX_INTERVAL_S = 300.0
REALTIME_BAR_STALL_TIMEOUT_S = 60.0
_HISTORICAL_BARS_TIMEOUT_S = 15.0
_REALTIME_BAR_MAX_NEW_REQUESTS = 60
_REALTIME_BAR_REQUEST_WINDOW_S = 600.0
_REALTIME_BAR_DEFAULT_MAX_ACTIVE = 100


class IBKRBarSubscriptionStalled(IBKRBarStreamError):
    """Raised when a connected real-time-bar request stops advancing."""


class IBKRBarInterrupted(IBKRBarStreamError):
    """A broker line stopped for a survivable reason: socket down, 1100 soft loss, or reconnect.

    Consumers with a continuity policy recover from this; everyone else treats it
    as the fatal ``IBKRBarStreamError`` it subclasses.
    """

    def __init__(
        self, message: str, *, cause: Literal["socket_down", "soft_loss_1100", "generation_changed"]
    ) -> None:
        super().__init__(message)
        self.cause = cause


def _client_generation(client: IbkrClient) -> int:
    """Read the connection generation every lease and registry key is fenced by."""
    return client.connection_generation


class _RealtimeBarRequestPacer:
    """Sliding-window guard for *new* ``reqRealTimeBars`` requests.

    IBKR permits at most 60 new real-time-bar subscriptions in 600 seconds.
    Receiving bars on an already-open subscription does not consume this
    request budget. The pacer intentionally waits instead of surfacing a
    broker pacing violation; callers remain cancellable while waiting.

    Reference: https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/
      ("Request Real Time Bars").
    """

    def __init__(
        self,
        *,
        max_requests: int = _REALTIME_BAR_MAX_NEW_REQUESTS,
        window_s: float = _REALTIME_BAR_REQUEST_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be positive")
        if window_s <= 0:
            raise ValueError("window_s must be positive")
        self._max_requests = max_requests
        self._window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._request_times: deque[float] = deque()

    async def acquire(self) -> None:
        delayed = False
        while True:
            now = self._clock()
            cutoff = now - self._window_s
            while self._request_times and self._request_times[0] <= cutoff:
                self._request_times.popleft()
            if len(self._request_times) < self._max_requests:
                self._request_times.append(now)
                return

            wait_s = max(0.0, self._request_times[0] + self._window_s - now)
            if not delayed:
                logger.warning(
                    "Pacing new IBKR real-time-bar subscription",
                    extra={
                        "action": "ibkr_realtime_bar_paced",
                        "max_new_requests": self._max_requests,
                        "window_s": self._window_s,
                        "wait_s": wait_s,
                    },
                )
                delayed = True
            await self._sleep(wait_s)


class _SubscriptionKey(NamedTuple):
    """Identity of one shared ``reqRealTimeBars`` line, generation included."""

    client_id: int
    generation: int
    con_id: int
    bar_size: int
    what_to_show: str
    use_rth: bool


@dataclass
class _RealtimeBarSubscription:
    client: IbkrClient
    bars: list[object]
    generation: int
    consumer_count: int = 1
    invalidated: bool = False


@dataclass
class _RealtimeBarLease:
    """One consumer's reference to a shared broker subscription."""

    registry: _RealtimeBarSubscriptionRegistry
    key: _SubscriptionKey
    subscription: _RealtimeBarSubscription
    bars: list[object]
    start_index: int
    multiplexed: bool
    consumer_count: int
    generation: int
    _released: bool = False

    @property
    def invalidated(self) -> bool:
        """Whether another consumer invalidated this shared broker line."""
        return self.subscription.invalidated

    def invalidate(self) -> bool:
        """Invalidate the shared line; return whether this call cancelled it."""
        if self._released:
            return False
        self._released = True
        return self.registry.invalidate(self.key, self.subscription)

    def release(self) -> bool:
        """Release this consumer; return whether the broker line was cancelled."""
        if self._released:
            return False
        self._released = True
        return self.registry.release(self.key, self.subscription)


class _RealtimeBarSubscriptionRegistry:
    """Multiplex real-time bars over one request per qualified contract.

    ``IbkrClient`` is single-event-loop owned, so registry state mutations are
    deliberately synchronous between await points. A per-key pending future
    prevents concurrent first consumers from opening duplicate subscriptions.
    The scope is one Python process; separate host-runner processes still own
    separate IBKR clients for order-identity isolation.
    """

    def __init__(
        self,
        pacer: _RealtimeBarRequestPacer | None = None,
        *,
        default_max_active: int = _REALTIME_BAR_DEFAULT_MAX_ACTIVE,
    ) -> None:
        if default_max_active < 1:
            raise ValueError("default_max_active must be positive")
        self._pacer = pacer or _RealtimeBarRequestPacer()
        self._default_max_active = default_max_active
        self._subscriptions: dict[_SubscriptionKey, _RealtimeBarSubscription] = {}
        self._pending: dict[_SubscriptionKey, asyncio.Future[None]] = {}

    async def acquire(
        self,
        client: IbkrClient,
        contract: object,
        *,
        bar_size: int,
        what_to_show: str,
        use_rth: bool,
    ) -> _RealtimeBarLease:
        con_id = int(getattr(contract, "conId", 0))
        if con_id <= 0:
            raise IBKRBarStreamError(
                "reqRealTimeBars requires a qualified contract with a positive conId."
            )

        # One pass per connection generation. Every ``continue`` below re-reads
        # the generation and rebuilds the key: a pass that survives an await
        # may be looking at a socket that no longer exists, and acting on the
        # stale key would file a request under — or spend pacing budget on —
        # a line that can never be used.
        while True:
            generation = _client_generation(client)
            self._evict_older_generations(client, generation)
            key = _SubscriptionKey(
                id(client), generation, con_id, bar_size, what_to_show, use_rth
            )

            existing = self._subscriptions.get(key)
            if existing is not None:
                start_index = len(existing.bars)
                existing.consumer_count += 1
                return _RealtimeBarLease(
                    registry=self,
                    key=key,
                    subscription=existing,
                    bars=existing.bars,
                    start_index=start_index,
                    multiplexed=True,
                    consumer_count=existing.consumer_count,
                    generation=existing.generation,
                )

            pending = self._pending.get(key)
            if pending is not None:
                # Another consumer is opening this exact line. Restart rather
                # than resuming with this pass's key: the socket may have been
                # replaced while we waited, and a woken waiter that trusts the
                # old key becomes the leader for a dead one.
                await asyncio.shield(pending)
                continue

            max_active = self._max_active_for_client(client)
            client_key = id(client)
            reserved = sum(
                existing_key.client_id == client_key and existing_key.generation == generation
                for existing_key in (*self._subscriptions, *self._pending)
            )
            if reserved >= max_active:
                raise IBKRBarStreamError(
                    "IBKR real-time-bar local active-line cap reached: "
                    f"{reserved}/{max_active}. Reuse or release a subscription, "
                    "raise IBKR_REALTIME_BAR_MAX_ACTIVE only when the username's "
                    "market-data allocation supports it, or use an external data provider."
                )
            pending = asyncio.get_running_loop().create_future()
            self._pending[key] = pending

            try:
                # No await separates the generation read above from this call,
                # so the pacer is only ever entered under a live generation and
                # never spends budget on a dead key. It can still move during a
                # real pacing sleep, which is what the check below catches.
                await self._pacer.acquire()
                if _client_generation(client) != generation:
                    # The socket was replaced while we waited on the pacer:
                    # requesting bars now would file them under a dead key.
                    logger.info(
                        "Restarting real-time-bar acquisition on a newer connection generation",
                        extra={
                            "action": "ibkr_realtime_bar_generation_restart",
                            "generation": generation,
                            "con_id": con_id,
                        },
                    )
                    continue
                bars = client.ib.reqRealTimeBars(
                    contract,
                    bar_size,
                    what_to_show,
                    useRTH=use_rth,
                )
                subscription = _RealtimeBarSubscription(
                    client=client, bars=bars, generation=generation
                )
                self._subscriptions[key] = subscription
                return _RealtimeBarLease(
                    registry=self,
                    key=key,
                    subscription=subscription,
                    bars=bars,
                    start_index=0,
                    multiplexed=False,
                    consumer_count=1,
                    generation=generation,
                )
            finally:
                self._pending.pop(key, None)
                if not pending.done():
                    pending.set_result(None)

    def _evict_older_generations(self, client: IbkrClient, generation: int) -> None:
        """Drop registry entries whose socket is gone; never send a cancel for them."""
        stale = [
            key
            for key in self._subscriptions
            if key.client_id == id(client) and key.generation < generation
        ]
        for key in stale:
            subscription = self._subscriptions.pop(key)
            subscription.invalidated = True
            logger.info(
                "Evicted real-time-bar subscription from a previous connection generation",
                extra={
                    "action": "ibkr_realtime_bar_generation_evicted",
                    "generation": key.generation,
                    "con_id": key.con_id,
                },
            )

    def _dropped_as_stale(
        self,
        key: _SubscriptionKey,
        subscription: _RealtimeBarSubscription,
    ) -> bool:
        """Drop a subscription whose socket is gone; return whether it was dropped.

        ``ib_async`` restarts request ids on reconnect, so the reqId this
        subscription holds may already belong to a line on the new socket:
        never send ``cancelRealTimeBars`` across generations.
        """
        if subscription.generation == _client_generation(subscription.client):
            return False
        self._subscriptions.pop(key, None)
        return True

    def _max_active_for_client(self, client: IbkrClient) -> int:
        settings = getattr(client, "settings", None)
        configured = getattr(settings, "realtime_bar_max_active", self._default_max_active)
        return int(configured)

    def release(
        self,
        key: _SubscriptionKey,
        expected: _RealtimeBarSubscription,
    ) -> bool:
        subscription = self._subscriptions.get(key)
        if subscription is not expected:
            return False
        subscription.consumer_count -= 1
        if subscription.consumer_count > 0:
            return False
        if self._dropped_as_stale(key, subscription):
            return False

        self._subscriptions.pop(key, None)
        try:
            subscription.client.ib.cancelRealTimeBars(subscription.bars)
        except Exception as exc:
            logger.debug("cancelRealTimeBars raised on shared-subscription shutdown: %s", exc)
        return True

    def invalidate(
        self,
        key: _SubscriptionKey,
        expected: _RealtimeBarSubscription,
    ) -> bool:
        """Cancel and evict exactly the stalled generation for ``key``."""
        subscription = self._subscriptions.get(key)
        if subscription is not expected:
            return False
        if self._dropped_as_stale(key, subscription):
            return False

        self._subscriptions.pop(key, None)
        subscription.invalidated = True
        try:
            subscription.client.ib.cancelRealTimeBars(subscription.bars)
        except Exception as exc:
            logger.debug("cancelRealTimeBars raised on stalled-subscription invalidation: %s", exc)
        return True


_REALTIME_BAR_SUBSCRIPTIONS = _RealtimeBarSubscriptionRegistry()


@dataclass
class _BarDeliveryLogger:
    """Shared subscription timing logs for IBKR real-time bar streams."""

    symbol: str
    con_id: int
    use_rth: bool
    subscribed_at: float = field(default_factory=time.monotonic)
    next_no_bar_log_at: float = field(init=False)
    warning_interval_s: float = field(default=NO_BAR_WARNING_INITIAL_INTERVAL_S, init=False)
    first_bar_logged: bool = False

    def __post_init__(self) -> None:
        self.next_no_bar_log_at = self.subscribed_at + self.warning_interval_s

    def log_subscribed(
        self,
        *,
        initial_bar_count: int,
        multiplexed: bool,
        consumer_count: int,
    ) -> None:
        logger.info(
            "IBKR reqRealTimeBars consumer attached",
            extra={
                "symbol": self.symbol,
                "con_id": self.con_id,
                "bar_size": 5,
                "what_to_show": "TRADES",
                "use_rth": self.use_rth,
                "initial_bar_count": initial_bar_count,
                "multiplexed": multiplexed,
                "consumer_count": consumer_count,
            },
        )

    def maybe_log_no_bar(
        self,
        *,
        bar_count: int,
        connected: bool,
        connection_lost: bool,
        message: str,
    ) -> None:
        now = time.monotonic()
        if now < self.next_no_bar_log_at:
            return
        logger.warning(
            message,
            extra={
                "symbol": self.symbol,
                "con_id": self.con_id,
                "elapsed_s": round(now - self.subscribed_at, 3),
                "bar_count": bar_count,
                "connected": connected,
                "connection_lost": connection_lost,
                "use_rth": self.use_rth,
                "next_warning_interval_s": self.warning_interval_s,
            },
        )
        self.warning_interval_s = min(
            self.warning_interval_s * 2,
            NO_BAR_WARNING_MAX_INTERVAL_S,
        )
        self.next_no_bar_log_at = now + self.warning_interval_s

    def log_first_bar(self, *, bar_count: int, message: str) -> None:
        if self.first_bar_logged:
            return
        logger.info(
            message,
            extra={
                "symbol": self.symbol,
                "con_id": self.con_id,
                "elapsed_s": round(time.monotonic() - self.subscribed_at, 3),
                "bar_count": bar_count,
                "use_rth": self.use_rth,
            },
        )
        self.first_bar_logged = True


def _bars_expected_now(use_rth: bool) -> bool:
    """Return whether a real-time stock bar should be arriving now."""
    phase = _session_phase_for_ms(now_ms_utc())
    if use_rth:
        return phase == "RTH"
    return phase in {"PRE", "RTH", "POST", "OVERNIGHT"}


def _check_realtime_subscription_liveness(
    *,
    client: IbkrClient,
    lease: _RealtimeBarLease,
    symbol: str,
    use_rth: bool,
    stall_timeout_s: float,
    last_progress_at: float,
) -> tuple[float, bool, bool]:
    """Fail closed on a stale-generation, disconnected, invalidated, or stalled line."""
    if lease.generation != _client_generation(client):
        raise IBKRBarInterrupted(
            f"IBKR connection was re-established while streaming {symbol} 5-second bars; "
            "this lease belongs to the previous socket.",
            cause="generation_changed",
        )
    connected = client.is_connected()
    connection_lost = client.connection_lost
    if not connected:
        raise IBKRBarInterrupted(
            f"IBKR connection lost while streaming {symbol} 5-second bars; "
            "halting rather than hanging on a dead feed.",
            cause="socket_down",
        )
    if connection_lost:
        raise IBKRBarInterrupted(
            f"IBKR connectivity lost (code 1100) while streaming {symbol} 5-second bars; "
            "halting rather than streaming a dead feed.",
            cause="soft_loss_1100",
        )
    if lease.invalidated:
        raise IBKRBarSubscriptionStalled(
            f"IBKR real-time-bar subscription for {symbol} was invalidated "
            "after another consumer observed it stalled."
        )
    now_monotonic = time.monotonic()
    if not _bars_expected_now(use_rth):
        last_progress_at = now_monotonic
    elif now_monotonic - last_progress_at >= stall_timeout_s:
        lease.invalidate()
        raise IBKRBarSubscriptionStalled(
            f"IBKR real-time-bar subscription for {symbol} stalled for "
            f"{stall_timeout_s:g}s while bars were expected."
        )
    return last_progress_at, connected, connection_lost


def _contract_venue(contract: object) -> str | None:
    exchange = getattr(contract, "exchange", None)
    primary = getattr(contract, "primaryExchange", None)
    venue = str(primary or exchange or "").strip().upper()
    return venue or None


async def fetch_historical_minute_bars(
    client: IbkrClient,
    symbol: str,
    *,
    duration: str = "1 D",
    end_datetime: str = "",
    use_rth: bool = True,
) -> list[IbkrMinuteBar]:
    """Fetch read-only IBKR historical 1-minute TRADES bars with provenance."""
    client.require_connected()
    contract = await qualify_underlying(client, symbol)
    sym = symbol.upper()
    venue = _contract_venue(contract)
    recorder = get_ibkr_api_evidence_recorder()
    request = evidence_request(
        "reqHistoricalDataAsync",
        contract={"conId": int(contract.conId), "symbol": contract.symbol, "secType": contract.secType},
        endDateTime=end_datetime,
        durationStr=duration,
        barSizeSetting="1 min",
        whatToShow="TRADES",
        useRTH=use_rth,
        formatDate=2,
        keepUpToDate=False,
    )
    try:
        raw_bars = await asyncio.wait_for(
            client.ib.reqHistoricalDataAsync(
                contract,
                endDateTime=end_datetime,
                durationStr=duration,
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=use_rth,
                formatDate=2,
                keepUpToDate=False,
            ),
            timeout=_HISTORICAL_BARS_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise IBKRBarStreamError(f"IBKR historical bars timed out for {symbol}.") from exc
    recorder.record(
        source="bars.fetch_historical_minute_bars",
        symbol=sym,
        request=request,
        response=evidence_response("historicalData", fields={"bar_count": len(raw_bars)}),
    )

    out: list[IbkrMinuteBar] = []
    last_start_ms: int | None = None
    fetched_at_ms = now_ms_utc()
    for raw_bar in raw_bars:
        start_ms = _bar_time_ms(raw_bar)
        if last_start_ms is not None and start_ms <= last_start_ms:
            raise IBKRBarStreamError(
                f"Non-monotonic IBKR historical minute bar timestamp: {start_ms} after {last_start_ms}."
            )
        last_start_ms = start_ms
        contribution = _contribution(raw_bar)
        out.append(
            IbkrMinuteBar(
                symbol=sym,
                start_ms=start_ms,
                end_ms=start_ms + 60_000,
                open=contribution.open,
                high=contribution.high,
                low=contribution.low,
                close=contribution.close,
                volume=contribution.volume,
                fetched_at_ms=fetched_at_ms,
                provenance="ibkr_historical",
                venue=venue,
                session_phase=_session_phase_for_ms(start_ms),
                use_rth=use_rth,
            )
        )
    return out


class _LeasedBar(NamedTuple):
    """One raw 5-second bar, with the facts about the line that delivered it."""

    raw: object
    venue: str | None
    generation: int


async def _iter_leased_raw_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool,
    stall_timeout_s: float,
    evidence_source: str,
    consumer_label: str,
    no_bar_message: str,
    first_bar_message: str,
    last_source_ms: int | None = None,
    on_source_bar: Callable[[int], None] | None = None,
) -> AsyncIterator[_LeasedBar]:
    """Yield raw 5-second bars off one leased ``reqRealTimeBars`` line.

    Everything both public streams do around the bar itself lives here: the
    shared-subscription lease and its evidence, the per-iteration liveness
    gate, the ruling-P10 drain, the no-delivery log, and the release. The
    callers differ only in how they map a raw bar.

    Progress -- what the stall timer measures and what ``on_source_bar``
    reports -- is "the raw source timestamp strictly advanced", because that
    is exactly when a bar carries an observation this line has not delivered
    before. A redelivery absorbed as a duplicate, or skipped because its
    minute was already flushed, carries none and leaves ``last_source_ms``
    where it was. Pass ``last_source_ms`` when an earlier generation of this
    stream already advanced it.
    """
    client.require_connected()
    contract = await qualify_underlying(client, symbol)
    lease = await _REALTIME_BAR_SUBSCRIPTIONS.acquire(
        client,
        contract,
        bar_size=5,
        what_to_show="TRADES",
        use_rth=use_rth,
    )
    bars = lease.bars
    sym = symbol.upper()
    venue = _contract_venue(contract)
    delivery_logger = _BarDeliveryLogger(
        symbol=sym,
        con_id=int(contract.conId),
        use_rth=use_rth,
    )
    delivery_logger.log_subscribed(
        initial_bar_count=len(bars),
        multiplexed=lease.multiplexed,
        consumer_count=lease.consumer_count,
    )
    recorder = get_ibkr_api_evidence_recorder()
    recorder.record(
        source=f"{evidence_source}.subscribe",
        symbol=sym,
        request=evidence_request(
            "reqRealTimeBars",
            contract={"conId": int(contract.conId), "symbol": contract.symbol, "secType": contract.secType},
            barSize=5,
            whatToShow="TRADES",
            useRTH=use_rth,
            realTimeBarsOptions=[],
            requestIssued=not lease.multiplexed,
            multiplexed=lease.multiplexed,
            consumerCount=lease.consumer_count,
        ),
        response=evidence_response(
            "realTimeBarList",
            fields={"bar_count": len(bars), "start_index": lease.start_index},
        ),
    )
    index = lease.start_index
    last_progress_at = time.monotonic()

    def _observe(raw_bar) -> _LeasedBar:
        nonlocal last_progress_at, last_source_ms
        delivery_logger.log_first_bar(bar_count=len(bars), message=first_bar_message)
        recorder.record(
            source=f"{evidence_source}.bar",
            symbol=sym,
            request=evidence_request("reqRealTimeBars", barSize=5, whatToShow="TRADES", useRTH=use_rth),
            response=evidence_response("realTimeBar", objects=[raw_bar]),
        )
        source_ms = _bar_time_ms(raw_bar)
        if last_source_ms is None or source_ms > last_source_ms:
            last_source_ms = source_ms
            last_progress_at = time.monotonic()
            if on_source_bar is not None:
                on_source_bar(source_ms)
        return _LeasedBar(raw_bar, venue, lease.generation)

    try:
        while True:
            # --- liveness gate: every iteration, before touching ``bars`` ---
            # ib_async stops appending to ``bars`` on a Gateway disconnect and
            # raises nothing, so without this check the loop would spin forever
            # yielding no bars and the live engine would go silently blind. It
            # runs on every iteration, not only when idle: a reconnect can land
            # while undelivered bars are still queued on the orphaned list.
            try:
                last_progress_at, connected, connection_lost = _check_realtime_subscription_liveness(
                    client=client,
                    lease=lease,
                    symbol=symbol,
                    use_rth=use_rth,
                    stall_timeout_s=stall_timeout_s,
                    last_progress_at=last_progress_at,
                )
            except (IBKRBarInterrupted, IBKRBarSubscriptionStalled) as interruption:
                # Ruling P10: the queue holds real pre-disconnect prints, and
                # only pre-disconnect prints -- ``Wrapper.reset()`` orphans this
                # list, so the new socket can never append to it. Delivering
                # them before surfacing the interruption is the difference
                # between a minute the reconnect can complete and a minute the
                # run must refuse.
                queued = list(bars[index:])
                index += len(queued)
                for raw_bar in queued:
                    yield _observe(raw_bar)
                raise interruption
            # --- end liveness gate ---
            if index >= len(bars):
                delivery_logger.maybe_log_no_bar(
                    bar_count=len(bars),
                    connected=connected,
                    connection_lost=connection_lost,
                    message=no_bar_message,
                )
                await asyncio.sleep(0.1)
                continue
            raw_bar = bars[index]
            index += 1
            yield _observe(raw_bar)
    finally:
        cancelled = lease.release()
        logger.debug(
            "Released %s consumer for %s (broker_subscription_cancelled=%s)",
            consumer_label,
            symbol,
            cancelled,
        )


async def stream_raw_5s_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool = True,
    stall_timeout_s: float = REALTIME_BAR_STALL_TIMEOUT_S,
) -> AsyncIterator[IbkrMinuteBar]:
    """Yield raw 5-second TRADES bars from IBKR's ``reqRealTimeBars``.

    No minute aggregation. Each yielded model carries ``start_ms`` =
    source timestamp ms and ``end_ms`` = ``start_ms + 5_000``. The OHLCV
    fields come straight from the raw 5-sec bar (no folding, no
    correction-replacement bookkeeping — every yielded bar is a verbatim
    snapshot of what IBKR delivered).

    The model is reused as :class:`IbkrMinuteBar` (its schema is
    bar-resolution-agnostic; only the name is minute-flavoured). Live
    consumers distinguish 1-min vs 5-sec by the ``end_ms - start_ms``
    window or by which endpoint sourced the data.

    Concurrent same-process consumers multiplex onto the same broker request.
    ``ib_async`` owns the reqId-to-list routing; this module reference-counts
    that list so a 5-second chart and a 1-minute consolidator consume one
    shared market-data line rather than opening duplicate lines.
    """
    sym = symbol.upper()
    async with aclosing(
        _iter_leased_raw_bars(
            client,
            symbol,
            use_rth=use_rth,
            stall_timeout_s=stall_timeout_s,
            evidence_source="bars.stream_raw_5s_bars",
            consumer_label="raw 5-second bar",
            no_bar_message="IBKR reqRealTimeBars has not delivered raw 5-second bars",
            first_bar_message="IBKR reqRealTimeBars delivered first raw 5-second bar",
        )
    ) as leased_bars:
        async for leased in leased_bars:
            source_ms = _bar_time_ms(leased.raw)
            contribution = _contribution(leased.raw)
            yield IbkrMinuteBar(
                symbol=sym,
                start_ms=source_ms,
                end_ms=source_ms + 5_000,
                open=contribution.open,
                high=contribution.high,
                low=contribution.low,
                close=contribution.close,
                volume=contribution.volume,
                fetched_at_ms=now_ms_utc(),
                provenance="ibkr_realtime",
                venue=leased.venue,
                session_phase=_session_phase_for_ms(source_ms),
                use_rth=use_rth,
            )


async def stream_minute_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool = True,
    on_source_bar: Callable[[int], None] | None = None,
    stall_timeout_s: float = REALTIME_BAR_STALL_TIMEOUT_S,
    assembler: MinuteAssembler,
) -> AsyncIterator[IbkrMinuteBar]:
    """Yield closed 1-minute bars built from IBKR 5-second TRADES bars.

    Uses the ``live_idempotent`` duplicate policy: IBKR may redeliver a
    5-second bar on an active subscription, and that redelivery must not
    crash a live trading run. Exact redeliveries are skipped and
    different-valued redeliveries correct the still-open minute; both are
    counted on ``LiveBarCounters`` and logged.

    The ``assembler`` is the caller's, because it outlives this call: a caller
    that resubscribes after ``IBKRBarInterrupted`` hands the same assembler to
    the next call and the contributions from both sockets fold into one minute.
    Each contribution is tagged with the delivering lease's generation, so such
    a minute emits with ``spans_interruption=True`` by construction. A caller
    that does not want to survive an interruption places a fresh
    ``MinuteAssembler()`` per call, which is the pre-#1921 behaviour.
    """
    sym = symbol.upper()
    try:
        async with aclosing(
            _iter_leased_raw_bars(
                client,
                symbol,
                use_rth=use_rth,
                stall_timeout_s=stall_timeout_s,
                evidence_source="bars.stream_minute_bars",
                consumer_label="minute-bar",
                no_bar_message="IBKR reqRealTimeBars has not delivered 5-second bars",
                first_bar_message="IBKR reqRealTimeBars delivered first 5-second bar",
                last_source_ms=assembler.last_source_ms,
                on_source_bar=on_source_bar,
            )
        ) as leased_bars:
            async for leased in leased_bars:
                emitted = assembler.feed(
                    leased.raw,
                    symbol=sym,
                    generation=leased.generation,
                    venue=leased.venue,
                    use_rth=use_rth,
                )
                if emitted is not None:
                    yield emitted
    finally:
        logger.debug(
            "Minute-bar consumer for %s detached (skipped_duplicate=%d, applied_correction=%d)",
            symbol,
            assembler.counters.skipped_duplicate,
            assembler.counters.applied_correction,
        )
