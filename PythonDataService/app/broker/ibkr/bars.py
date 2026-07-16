"""Real-time underlying minute bars from IBKR.

IBKR's ``reqRealTimeBars`` emits 5-second TRADES bars. This module
aggregates those into closed 1-minute bars for the live engine, enforcing
the repo's timestamp policy at the ingestion boundary: every yielded model
uses ``int64`` ms UTC.

All same-process consumers for the same ``(client, contract, whatToShow,
useRTH)`` tuple share one underlying ``reqRealTimeBars`` subscription. The
registry reference-counts consumers and cancels the broker subscription only
when the last consumer leaves. New subscriptions are paced at IBKR's
documented ceiling of 60 requests per 600 seconds. ``ib_async`` separately
throttles ordinary socket messages at the 45-per-second rate pinned by
``IbkrClient``, below the default 50 requests/second connection limit.

Two duplicate policies govern how a repeated source timestamp is treated
(see ``DuplicatePolicy``):

* ``"strict"`` (default) — any duplicate or non-monotonic source timestamp
  fails fast. This is the finite-historical-ingestion contract from
  ``.claude/rules/numerical-rigor.md`` and keeps the parity tests honest.
* ``"live_idempotent"`` — used only by the live 5-second subscription.
  IBKR's docs do not promise duplicate-free delivery for an active
  ``reqRealTimeBars`` subscription, so a redelivery of the most recent
  5-second bar is absorbed idempotently and surfaced (logged + counted)
  rather than crashing the live run. A redelivery that carries *different*
  OHLCV is treated as a correction to the still-open minute. Any timestamp
  belonging to an already-emitted minute is strictly less than the current
  minute's bars and therefore still fails fast as a regression.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from app.broker.ibkr.api_evidence import (
    evidence_request,
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.models import BarProvenance, IbkrMinuteBar
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

DuplicatePolicy = Literal["strict", "live_idempotent"]
NO_BAR_WARNING_INITIAL_INTERVAL_S = 30.0
NO_BAR_WARNING_MAX_INTERVAL_S = 300.0
_HISTORICAL_BARS_TIMEOUT_S = 15.0
_NY_TZ = ZoneInfo("America/New_York")
_REALTIME_BAR_MAX_NEW_REQUESTS = 60
_REALTIME_BAR_REQUEST_WINDOW_S = 600.0
_REALTIME_BAR_DEFAULT_MAX_ACTIVE = 100


class IBKRBarStreamError(Exception):
    """Raised when IBKR real-time bars violate timestamp invariants."""


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


_SubscriptionKey = tuple[int, int, int, str, bool]


@dataclass
class _RealtimeBarSubscription:
    client: IbkrClient
    bars: list[object]
    consumer_count: int = 1


@dataclass
class _RealtimeBarLease:
    """One consumer's reference to a shared broker subscription."""

    registry: _RealtimeBarSubscriptionRegistry
    key: _SubscriptionKey
    bars: list[object]
    start_index: int
    multiplexed: bool
    consumer_count: int
    _released: bool = False

    def release(self) -> bool:
        """Release this consumer; return whether the broker line was cancelled."""
        if self._released:
            return False
        self._released = True
        return self.registry.release(self.key)


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
        key = (id(client), con_id, bar_size, what_to_show, use_rth)

        while True:
            existing = self._subscriptions.get(key)
            if existing is not None:
                start_index = len(existing.bars)
                existing.consumer_count += 1
                return _RealtimeBarLease(
                    registry=self,
                    key=key,
                    bars=existing.bars,
                    start_index=start_index,
                    multiplexed=True,
                    consumer_count=existing.consumer_count,
                )

            pending = self._pending.get(key)
            if pending is None:
                max_active = self._max_active_for_client(client)
                client_key = id(client)
                reserved = sum(
                    existing_key[0] == client_key
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
                break
            await asyncio.shield(pending)

        try:
            await self._pacer.acquire()
            bars = client.ib.reqRealTimeBars(
                contract,
                bar_size,
                what_to_show,
                useRTH=use_rth,
            )
            subscription = _RealtimeBarSubscription(client=client, bars=bars)
            self._subscriptions[key] = subscription
            return _RealtimeBarLease(
                registry=self,
                key=key,
                bars=bars,
                start_index=0,
                multiplexed=False,
                consumer_count=1,
            )
        finally:
            self._pending.pop(key, None)
            if not pending.done():
                pending.set_result(None)

    def _max_active_for_client(self, client: IbkrClient) -> int:
        settings = getattr(client, "settings", None)
        configured = getattr(settings, "realtime_bar_max_active", self._default_max_active)
        return int(configured)

    def release(self, key: _SubscriptionKey) -> bool:
        subscription = self._subscriptions.get(key)
        if subscription is None:
            return False
        subscription.consumer_count -= 1
        if subscription.consumer_count > 0:
            return False

        self._subscriptions.pop(key, None)
        try:
            subscription.client.ib.cancelRealTimeBars(subscription.bars)
        except Exception as exc:
            logger.debug("cancelRealTimeBars raised on shared-subscription shutdown: %s", exc)
        return True


_REALTIME_BAR_SUBSCRIPTIONS = _RealtimeBarSubscriptionRegistry()


@dataclass
class LiveBarCounters:
    """Observable counters for idempotent live redelivery handling.

    Owned by ``stream_minute_bars`` and threaded into
    ``aggregate_realtime_bar`` so a live run can report how often IBKR
    redelivered a 5-second bar without it being a fatal event.
    """

    skipped_duplicate: int = 0
    applied_correction: int = 0


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


def _to_utc_ms(value: datetime | int | float | str) -> int:
    """Convert an IBKR bar timestamp to canonical int64 ms UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise IBKRBarStreamError("IBKR bar timestamp is naive; expected tz-aware UTC datetime.")
        return int(value.astimezone(UTC).timestamp() * 1000)
    if isinstance(value, str):
        text = value.strip()
        for fmt in ("%Y%m%d %H:%M:%S", "%Y%m%d"):
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            return int(parsed.replace(tzinfo=_NY_TZ).astimezone(UTC).timestamp() * 1000)
        raise IBKRBarStreamError(f"IBKR bar timestamp string has unsupported format: {value!r}.")
    numeric = float(value)
    # ib_async/IB API bars commonly expose epoch seconds. Accept ms too for
    # tests/future wrappers by checking magnitude.
    if numeric > 10_000_000_000:
        return int(numeric)
    return int(numeric * 1000)


def _minute_start_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % 60_000)


def _session_phase_for_ms(ts_ms: int) -> Literal["PRE", "RTH", "POST", "OVERNIGHT", "CLOSED", "UNKNOWN"]:
    """Classify a bar timestamp into the exchange session used for provenance."""
    now_ny = datetime.fromtimestamp(ts_ms / 1000, tz=UTC).astimezone(_NY_TZ)
    minutes = now_ny.hour * 60 + now_ny.minute
    if minutes < 4 * 60 or minutes >= 20 * 60:
        return "OVERNIGHT"
    try:
        window = session_window_for_date(now_ny.date())
    except LookupError:
        return "CLOSED"
    if ts_ms < window.open_ms_utc:
        return "PRE"
    if ts_ms < window.close_ms_utc:
        return "RTH"
    return "POST"


def _contract_venue(contract: object) -> str | None:
    exchange = getattr(contract, "exchange", None)
    primary = getattr(contract, "primaryExchange", None)
    venue = str(primary or exchange or "").strip().upper()
    return venue or None


@dataclass(frozen=True)
class _Contribution:
    """One 5-second bar's OHLCV contribution to a minute."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass
class _MinuteAccumulator:
    """Accumulates 5-second contributions, keyed by source timestamp.

    Contributions are stored per source ``ms`` rather than folded into a
    running OHLCV so a same-timestamp correction can replace one
    contribution and have ``high``/``low`` recomputed correctly. A minute
    holds at most twelve 5-second bars, so the storage cost is trivial.
    """

    symbol: str
    start_ms: int
    venue: str | None = None
    use_rth: bool | None = None
    provenance: BarProvenance = "ibkr_realtime"
    contributions: dict[int, _Contribution] = field(default_factory=dict)

    @property
    def open(self) -> Decimal:
        return self.contributions[min(self.contributions)].open

    @property
    def high(self) -> Decimal:
        return max(c.high for c in self.contributions.values())

    @property
    def low(self) -> Decimal:
        return min(c.low for c in self.contributions.values())

    @property
    def close(self) -> Decimal:
        return self.contributions[max(self.contributions)].close

    @property
    def volume(self) -> int:
        return sum(c.volume for c in self.contributions.values())

    def to_model(self) -> IbkrMinuteBar:
        return IbkrMinuteBar(
            symbol=self.symbol,
            start_ms=self.start_ms,
            end_ms=self.start_ms + 60_000,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            fetched_at_ms=now_ms_utc(),
            provenance=self.provenance,
            venue=self.venue,
            session_phase=_session_phase_for_ms(self.start_ms),
            use_rth=self.use_rth,
        )


def _decimal_attr(obj, *names: str) -> Decimal:
    """Read the first present attribute from ``obj`` and coerce to ``Decimal``.

    The bar protocol differs slightly between sources: ``ib_async``'s
    ``RealTimeBar`` exposes the open as ``open_`` (trailing underscore to
    avoid shadowing the ``open()`` builtin in dataclass code), while the
    in-repo test fakes use plain ``open`` because the name is legal as
    an attribute. Try each candidate in order; raise if none are present.
    """
    for name in names:
        if hasattr(obj, name):
            return Decimal(str(getattr(obj, name)))
    raise IBKRBarStreamError(f"5-second bar missing all of: {names!r}")


def _volume_attr(obj) -> int:
    return int(getattr(obj, "volume", getattr(obj, "barCount", 0)) or 0)


def _bar_time_ms(obj) -> int:
    value = getattr(obj, "time", getattr(obj, "date", None))
    if value is None:
        raise IBKRBarStreamError("IBKR 5-second bar is missing a time/date field.")
    return _to_utc_ms(value)


def _contribution(bar) -> _Contribution:
    # ib_async.RealTimeBar uses ``open_`` (trailing underscore to avoid
    # shadowing the ``open()`` builtin); test fakes use plain ``open``.
    # Accept either so this works against both wire types.
    return _Contribution(
        open=_decimal_attr(bar, "open", "open_"),
        high=_decimal_attr(bar, "high"),
        low=_decimal_attr(bar, "low"),
        close=_decimal_attr(bar, "close"),
        volume=_volume_attr(bar),
    )


def _handle_duplicate(
    current: _MinuteAccumulator | None,
    source_ms: int,
    incoming: _Contribution,
    *,
    symbol: str,
    policy: DuplicatePolicy,
    counters: LiveBarCounters | None,
) -> tuple[_MinuteAccumulator, IbkrMinuteBar | None, int]:
    """Resolve a 5-second bar whose timestamp equals the last accepted one.

    ``strict`` raises. ``live_idempotent`` absorbs an exact redelivery
    (skip) or applies a correction in place. The duplicate always belongs
    to the still-open minute: ``last_source_ms`` is, by construction, the
    most recent contribution in ``current``.
    """
    if policy == "strict":
        raise IBKRBarStreamError(f"Duplicate IBKR 5-second bar timestamp: {source_ms}.")
    if policy != "live_idempotent":
        raise IBKRBarStreamError(f"Unknown duplicate policy: {policy!r}.")

    if current is None or source_ms not in current.contributions:
        # Invariant violation: a duplicate of last_source_ms must live in
        # the open minute. Surface rather than silently mis-handle.
        raise IBKRBarStreamError(
            f"Duplicate IBKR 5-second bar timestamp {source_ms} not found in open minute."
        )

    existing = current.contributions[source_ms]
    if existing == incoming:
        if counters is not None:
            counters.skipped_duplicate += 1
        # Logged at INFO, not WARNING — the live-idempotent ADR's
        # "surface, never silence" intent is satisfied by the
        # ``skipped_duplicate`` counter and the aggregate-stall
        # SUBSCRIPTION_STALE WARNING. Per-bar visibility doesn't need
        # to land in the Incidents panel. The "Applied correction"
        # log below stays WARNING because it actually changes the
        # bar's value.
        logger.info(
            "Idempotent skip of redelivered IBKR 5-second bar",
            extra={"symbol": symbol, "source_ms": source_ms, "action": "skipped_duplicate"},
        )
        return current, None, source_ms

    current.contributions[source_ms] = incoming
    if counters is not None:
        counters.applied_correction += 1
    logger.warning(
        "Applied correction to redelivered IBKR 5-second bar in open minute",
        extra={"symbol": symbol, "source_ms": source_ms, "action": "applied_correction"},
    )
    return current, None, source_ms


def aggregate_realtime_bar(
    current: _MinuteAccumulator | None,
    bar,
    *,
    symbol: str,
    last_source_ms: int | None,
    policy: DuplicatePolicy = "strict",
    counters: LiveBarCounters | None = None,
    venue: str | None = None,
    use_rth: bool | None = None,
    provenance: BarProvenance = "ibkr_realtime",
) -> tuple[_MinuteAccumulator, IbkrMinuteBar | None, int]:
    """Fold one IBKR 5-second bar into a minute accumulator.

    Returns ``(accumulator, emitted_minute_or_None, source_ms)``. The
    returned ``source_ms`` becomes the caller's ``last_source_ms`` — for an
    absorbed duplicate it is unchanged so monotonicity stays anchored to the
    last *distinct* timestamp.
    """
    source_ms = _bar_time_ms(bar)
    incoming = _contribution(bar)

    if last_source_ms is not None:
        if source_ms == last_source_ms:
            return _handle_duplicate(
                current,
                source_ms,
                incoming,
                symbol=symbol,
                policy=policy,
                counters=counters,
            )
        if source_ms < last_source_ms:
            raise IBKRBarStreamError(
                f"Non-monotonic IBKR 5-second bar timestamp: {source_ms} after {last_source_ms}."
            )

    start_ms = _minute_start_ms(source_ms)

    if current is None:
        return (
            _MinuteAccumulator(
                symbol=symbol,
                start_ms=start_ms,
                venue=venue,
                use_rth=use_rth,
                provenance=provenance,
                contributions={source_ms: incoming},
            ),
            None,
            source_ms,
        )

    if start_ms == current.start_ms:
        current.contributions[source_ms] = incoming
        return current, None, source_ms

    if start_ms < current.start_ms:
        raise IBKRBarStreamError(f"IBKR bar minute regressed from {current.start_ms} to {start_ms}.")

    emitted = current.to_model()
    return (
        _MinuteAccumulator(
            symbol=symbol,
            start_ms=start_ms,
            venue=venue,
            use_rth=use_rth,
            provenance=provenance,
            contributions={source_ms: incoming},
        ),
        emitted,
        source_ms,
    )


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


async def stream_raw_5s_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool = True,
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
        source="bars.stream_raw_5s_bars.subscribe",
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
    try:
        while True:
            if index >= len(bars):
                connected = client.is_connected()
                connection_lost = client.connection_lost
                if not connected or connection_lost:
                    raise IBKRBarStreamError(
                        f"IBKR connection lost while streaming {symbol} 5-second "
                        "raw bars; halting rather than hanging on a dead feed."
                    )
                delivery_logger.maybe_log_no_bar(
                    bar_count=len(bars),
                    connected=connected,
                    connection_lost=connection_lost,
                    message="IBKR reqRealTimeBars has not delivered raw 5-second bars",
                )
                await asyncio.sleep(0.1)
                continue
            raw_bar = bars[index]
            index += 1
            delivery_logger.log_first_bar(
                bar_count=len(bars),
                message="IBKR reqRealTimeBars delivered first raw 5-second bar",
            )
            recorder.record(
                source="bars.stream_raw_5s_bars.bar",
                symbol=sym,
                request=evidence_request("reqRealTimeBars", barSize=5, whatToShow="TRADES", useRTH=use_rth),
                response=evidence_response("realTimeBar", objects=[raw_bar]),
            )
            source_ms = _bar_time_ms(raw_bar)
            contribution = _contribution(raw_bar)
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
                venue=venue,
                session_phase=_session_phase_for_ms(source_ms),
                use_rth=use_rth,
            )
    finally:
        cancelled = lease.release()
        logger.debug(
            "Released raw 5-second bar consumer for %s (broker_subscription_cancelled=%s)",
            symbol,
            cancelled,
        )


async def stream_minute_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool = True,
) -> AsyncIterator[IbkrMinuteBar]:
    """Yield closed 1-minute bars built from IBKR 5-second TRADES bars.

    Uses the ``live_idempotent`` duplicate policy: IBKR may redeliver a
    5-second bar on an active subscription, and that redelivery must not
    crash a live trading run. Exact redeliveries are skipped and
    different-valued redeliveries correct the still-open minute; both are
    counted on ``LiveBarCounters`` and logged.
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
        source="bars.stream_minute_bars.subscribe",
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
    current: _MinuteAccumulator | None = None
    last_source_ms: int | None = None
    counters = LiveBarCounters()
    try:
        while True:
            if index >= len(bars):
                # No new 5-second bar yet. Before sleeping, confirm the feed is
                # still live: ib_async stops appending to ``bars`` on a Gateway
                # disconnect and raises nothing, so without this check the loop
                # would spin forever yielding no bars and the live engine would
                # go silently blind. Surface a fatal error instead.
                connected = client.is_connected()
                connection_lost = client.connection_lost
                if not connected or connection_lost:
                    raise IBKRBarStreamError(
                        f"IBKR connection lost while streaming {symbol} 5-second "
                        "bars; halting rather than hanging on a dead feed."
                    )
                delivery_logger.maybe_log_no_bar(
                    bar_count=len(bars),
                    connected=connected,
                    connection_lost=connection_lost,
                    message="IBKR reqRealTimeBars has not delivered 5-second bars",
                )
                await asyncio.sleep(0.1)
                continue
            raw_bar = bars[index]
            index += 1
            delivery_logger.log_first_bar(
                bar_count=len(bars),
                message="IBKR reqRealTimeBars delivered first 5-second bar",
            )
            recorder.record(
                source="bars.stream_minute_bars.bar",
                symbol=sym,
                request=evidence_request("reqRealTimeBars", barSize=5, whatToShow="TRADES", useRTH=use_rth),
                response=evidence_response("realTimeBar", objects=[raw_bar]),
            )
            current, emitted, last_source_ms = aggregate_realtime_bar(
                current,
                raw_bar,
                symbol=sym,
                last_source_ms=last_source_ms,
                policy="live_idempotent",
                counters=counters,
                venue=venue,
                use_rth=use_rth,
                provenance="ibkr_realtime",
            )
            if emitted is not None:
                yield emitted
    finally:
        cancelled = lease.release()
        logger.debug(
            "Released minute-bar consumer for %s (broker_subscription_cancelled=%s, "
            "skipped_duplicate=%d, applied_correction=%d)",
            symbol,
            cancelled,
            counters.skipped_duplicate,
            counters.applied_correction,
        )
