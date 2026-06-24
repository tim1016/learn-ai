"""Real-time underlying minute bars from IBKR.

IBKR's ``reqRealTimeBars`` emits 5-second TRADES bars. This module
aggregates those into closed 1-minute bars for the live engine, enforcing
the repo's timestamp policy at the ingestion boundary: every yielded model
uses ``int64`` ms UTC.

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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.models import IbkrMinuteBar
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

DuplicatePolicy = Literal["strict", "live_idempotent"]


class IBKRBarStreamError(Exception):
    """Raised when IBKR real-time bars violate timestamp invariants."""


@dataclass
class LiveBarCounters:
    """Observable counters for idempotent live redelivery handling.

    Owned by ``stream_minute_bars`` and threaded into
    ``aggregate_realtime_bar`` so a live run can report how often IBKR
    redelivered a 5-second bar without it being a fatal event.
    """

    skipped_duplicate: int = 0
    applied_correction: int = 0


def _to_utc_ms(value: datetime | int | float) -> int:
    """Convert an IBKR bar timestamp to canonical int64 ms UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise IBKRBarStreamError("IBKR bar timestamp is naive; expected tz-aware UTC datetime.")
        return int(value.astimezone(UTC).timestamp() * 1000)
    numeric = float(value)
    # ib_async/IB API bars commonly expose epoch seconds. Accept ms too for
    # tests/future wrappers by checking magnitude.
    if numeric > 10_000_000_000:
        return int(numeric)
    return int(numeric * 1000)


def _minute_start_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % 60_000)


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
        return _MinuteAccumulator(symbol, start_ms, {source_ms: incoming}), None, source_ms

    if start_ms == current.start_ms:
        current.contributions[source_ms] = incoming
        return current, None, source_ms

    if start_ms < current.start_ms:
        raise IBKRBarStreamError(f"IBKR bar minute regressed from {current.start_ms} to {start_ms}.")

    emitted = current.to_model()
    return _MinuteAccumulator(symbol, start_ms, {source_ms: incoming}), emitted, source_ms


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

    Note on concurrent subscriptions: this opens a SECOND ``reqRealTimeBars``
    subscription on the same contract when ``stream_minute_bars`` is also
    running for that symbol. IBKR accepts that within the per-session
    subscription cap (50 simultaneous real-time-bar subs); each one gets
    its own reqId. If the cap becomes a real concern at scale, the two
    paths can be unified onto a single source iterator that bifurcates.
    """
    client.require_connected()
    contract = await qualify_underlying(client, symbol)
    bars = client.ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=use_rth)
    sym = symbol.upper()
    index = 0
    try:
        while True:
            if index >= len(bars):
                if not client.is_connected() or client.connection_lost:
                    raise IBKRBarStreamError(
                        f"IBKR connection lost while streaming {symbol} 5-second "
                        "raw bars; halting rather than hanging on a dead feed."
                    )
                await asyncio.sleep(0.1)
                continue
            raw_bar = bars[index]
            index += 1
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
            )
    finally:
        try:
            client.ib.cancelRealTimeBars(bars)
        except Exception as exc:
            logger.debug("cancelRealTimeBars(%s, raw5s) raised on shutdown: %s", symbol, exc)


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
    bars = client.ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=use_rth)
    index = 0
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
                if not client.is_connected() or client.connection_lost:
                    raise IBKRBarStreamError(
                        f"IBKR connection lost while streaming {symbol} 5-second "
                        "bars; halting rather than hanging on a dead feed."
                    )
                await asyncio.sleep(0.1)
                continue
            raw_bar = bars[index]
            index += 1
            current, emitted, last_source_ms = aggregate_realtime_bar(
                current,
                raw_bar,
                symbol=symbol.upper(),
                last_source_ms=last_source_ms,
                policy="live_idempotent",
                counters=counters,
            )
            if emitted is not None:
                yield emitted
    finally:
        # Guard the cancel like every other subscription-cancel path in this
        # package (market_data, pnl): if the stream is unwinding because the
        # connection dropped, cancelRealTimeBars itself can raise — and as the
        # first statement of ``finally`` that would mask the original exception
        # the operator needs to see, and skip the counters log below.
        try:
            client.ib.cancelRealTimeBars(bars)
        except Exception as exc:
            logger.debug("cancelRealTimeBars(%s) raised on shutdown: %s", symbol, exc)
        logger.debug(
            "Cancelled reqRealTimeBars for %s (skipped_duplicate=%d, applied_correction=%d)",
            symbol,
            counters.skipped_duplicate,
            counters.applied_correction,
        )
