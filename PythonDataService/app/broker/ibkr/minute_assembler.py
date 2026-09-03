"""Fold IBKR 5-second TRADES bars into closed 1-minute bars.

Split out of ``app/broker/ibkr/bars.py`` (#1921) so the aggregation
primitives are independent of the broker subscription that feeds them:
:class:`MinuteAssembler` is owned by the *consumer* of
``stream_minute_bars``, so an interruption (socket drop, 1100 soft loss,
stall replacement) that ends one stream call never discards the open
minute. Contributions are keyed by source timestamp, so bars delivered
over the old socket and the new one merge deterministically, and every
emitted minute carries the receipts for that merge: ``contribution_count``
proves completeness by count and ``spans_interruption`` records that the
minute's data arrived over more than one connection generation.

This module owns no client, no request, and no event loop; ``bars.py``
keeps the subscription registry, the liveness gate, and the streams, and
re-exports what moved here so existing importers keep working.

Every boundary timestamp is ``int64`` ms UTC, per the repo's temporal
rules.

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

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from app.broker.ibkr.bar_models import BarProvenance, IbkrMinuteBar
from app.marketdata.feed import BarSessionPhase
from app.services.session_authority import session_state_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

DuplicatePolicy = Literal["strict", "live_idempotent"]
_NY_TZ = ZoneInfo("America/New_York")

RTH_CONTRIBUTIONS_PER_MINUTE: int = 60_000 // 5_000
"""IBKR pushes one 5-second TRADES bar every 5 s in RTH (measured 12/12 on 2026-09-02)."""


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


def _session_phase_for_ms(ts_ms: int) -> BarSessionPhase:
    """Classify one instant through the canonical session authority."""
    return session_state_at_ms(now_ms=ts_ms).phase


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

    ``generations`` records which connection generation each *stored*
    contribution came from. A skipped exact redelivery is not recorded:
    it contributes no data, so counting it would make ``spans_interruption``
    claim something the minute's contents do not support.
    """

    symbol: str
    start_ms: int
    venue: str | None = None
    use_rth: bool | None = None
    provenance: BarProvenance = "ibkr_realtime"
    contributions: dict[int, _Contribution] = field(default_factory=dict)
    generations: set[int] = field(default_factory=set)

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
            contribution_count=len(self.contributions),
            spans_interruption=len(self.generations) > 1,
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
    generation: int,
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
    current.generations.add(generation)
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
    generation: int = 0,
) -> tuple[_MinuteAccumulator, IbkrMinuteBar | None, int]:
    """Fold one IBKR 5-second bar into a minute accumulator.

    Returns ``(accumulator, emitted_minute_or_None, source_ms)``. The
    returned ``source_ms`` becomes the caller's ``last_source_ms`` — for an
    absorbed duplicate it is unchanged so monotonicity stays anchored to the
    last *distinct* timestamp.

    ``generation`` is the connection generation this bar was delivered on;
    it is recorded on whichever accumulator stores the contribution, so a
    minute stitched across a reconnect emits with ``spans_interruption``.
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
                generation=generation,
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
                generations={generation},
            ),
            None,
            source_ms,
        )

    if start_ms == current.start_ms:
        current.contributions[source_ms] = incoming
        current.generations.add(generation)
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
            generations={generation},
        ),
        emitted,
        source_ms,
    )


@dataclass
class MinuteAssembler:
    """Fold 5-second bars into closed minutes across subscription generations.

    Owned by the consumer of ``stream_minute_bars`` so an interruption (socket
    drop, 1100 soft loss, stall replacement) never discards the open minute;
    contributions are keyed by source timestamp, so bars from the old and the
    new socket merge deterministically and a redelivery is absorbed by the
    ``live_idempotent`` policy.

    ``_flushed`` remembers the minute :meth:`flush_if_complete` emitted early,
    until a later minute arrives. Without it, the resubscribed socket's first
    5-second bars — which may still belong to that minute — would either crash
    the run ("not found in open minute", because ``current`` is now ``None``)
    or rebuild an accumulator for a minute the consumer has already decided on.
    """

    current: _MinuteAccumulator | None = None
    last_source_ms: int | None = None
    counters: LiveBarCounters = field(default_factory=LiveBarCounters)
    _flushed: _MinuteAccumulator | None = field(default=None, init=False, repr=False)

    @property
    def open_minute_start_ms(self) -> int | None:
        return None if self.current is None else self.current.start_ms

    def _absorb_after_flush(self, raw_bar: object, *, symbol: str) -> bool:
        """Resolve a 5-second bar arriving after its minute was flushed early.

        Returns ``True`` when the bar was absorbed and must not reach the
        accumulator: an exact redelivery of the flushed minute's *most recent*
        contribution carries no new data. That is the whole of the live
        relaxation ``.claude/rules/temporal-rigor.md`` grants -- the same one
        ``aggregate_realtime_bar`` applies to the open minute -- and every
        other bar inside the flushed minute is refused, identical payload or
        not: an earlier timestamp belongs to an already-emitted aggregate, and
        a changed payload would correct a minute downstream has consumed. A bar
        belonging to a later minute clears the memory and proceeds normally;
        one belonging to an earlier minute proceeds too, and the ordinary
        non-monotonic guard fails it.
        """
        flushed = self._flushed
        if flushed is None:
            return False
        source_ms = _bar_time_ms(raw_bar)
        if _minute_start_ms(source_ms) != flushed.start_ms:
            self._flushed = None
            return False
        if source_ms == self.last_source_ms and flushed.contributions.get(source_ms) == _contribution(raw_bar):
            self.counters.skipped_duplicate += 1
            logger.info(
                "Idempotent skip of a 5-second bar redelivered after its minute was flushed",
                extra={"symbol": symbol, "source_ms": source_ms, "action": "skipped_duplicate"},
            )
            return True
        raise IBKRBarStreamError(
            f"IBKR 5-second bar {source_ms} belongs to minute {flushed.start_ms}, "
            "which was already emitted; refusing to rebuild an emitted minute."
        )

    def feed(
        self, raw_bar: object, *, symbol: str, generation: int, venue: str | None, use_rth: bool
    ) -> IbkrMinuteBar | None:
        if self._absorb_after_flush(raw_bar, symbol=symbol):
            return None
        self.current, emitted, self.last_source_ms = aggregate_realtime_bar(
            self.current,
            raw_bar,
            symbol=symbol,
            last_source_ms=self.last_source_ms,
            policy="live_idempotent",
            counters=self.counters,
            venue=venue,
            use_rth=use_rth,
            provenance="ibkr_realtime",
            generation=generation,
        )
        return emitted

    def flush_if_complete(self) -> IbkrMinuteBar | None:
        """Emit the open minute now iff it already holds every RTH contribution."""
        if self.current is None or len(self.current.contributions) < RTH_CONTRIBUTIONS_PER_MINUTE:
            return None
        emitted = self.current.to_model()
        self._flushed = self.current
        self.current = None
        return emitted
