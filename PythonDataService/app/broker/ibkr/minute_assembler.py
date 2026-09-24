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
from app.services.session_authority import scheduled_exchange_phase_at_ms
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

DuplicatePolicy = Literal["strict", "live_idempotent"]
_NY_TZ = ZoneInfo("America/New_York")

RTH_CONTRIBUTIONS_PER_MINUTE: int = 60_000 // 5_000
"""IBKR pushes one 5-second TRADES bar every 5 s in RTH (measured 12/12 on 2026-09-02)."""

SPARSE_MINUTE_EMIT_GRACE_MS: int = 8_000
"""How long past its close an extended-hours minute short of twelve prints is held (#2376).

A sparse PRE/POST minute has no twelfth print to close it, and waiting for the
next print can take longer than the decision allowance
(``marketdata.feed.DELIVERY_ALLOWANCE_MS``, 20 s), so the minute is refused as
``DECISION_LATE``. IBKR delivers each 5-second bar about 5 s after it starts,
so the minute's last print is due at its close; 8 s covers that delivery with
margin and leaves 12 s of the allowance for the decision. A print that lands
after the emit is ignored and counted
(``LiveBarCounters.ignored_late_print_after_emit``), never folded into the
minute downstream already decided on. Not a ported constant: an owner-approved
latency trade-off, tunable here.
"""


def _is_complete_by_count(bar: IbkrMinuteBar) -> bool:
    """Whether ``bar`` holds every 5-second print a minute can hold.

    Twelve contributions is the whole minute wherever it falls, so this is the
    one proof of completeness a minute *known* to have been cut -- by an
    interruption, or by the stream joining partway through it -- can offer.
    """
    return bar.contribution_count is not None and bar.contribution_count >= RTH_CONTRIBUTIONS_PER_MINUTE


MinuteCompleteness = Literal["complete", "short_join", "unprovable"]
"""What an emitted minute can prove about itself (#2364).

``complete``   -- deliverable as a whole minute: it holds all twelve prints,
                  or it is an untouched minute outside the regular session,
                  where sparse bars are normal and no count is owed.
``short_join`` -- the minute the stream joined partway through, short of
                  twelve and touched by no interruption. Nothing was
                  delivered before it, so it is omitted, never refused.
``unprovable`` -- short of twelve where twelve is owed: any minute an
                  interruption touched, or an untouched regular-session one.
"""


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
    ignored_post_emit_correction: int = 0
    #: A new print for a minute already emitted by the sparse-minute timer (#2376).
    ignored_late_print_after_emit: int = 0


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
    """Classify one instant by the calendar's scheduled PRE/RTH/POST session.

    Not ``session_state_at_ms``: with no declared window that proves only
    RTH/CLOSED, which blinded the ``useRTH=0`` stall watchdog in PRE and POST
    and stamped extended minutes ``CLOSED`` (#2299, #2313).
    """
    return scheduled_exchange_phase_at_ms(ts_ms)


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

    A minute is emitted as soon as it holds every contribution (by count), or
    otherwise when the first print of a later minute arrives -- or, for a
    sparse extended-hours minute, when the wall clock passes its close by
    ``SPARSE_MINUTE_EMIT_GRACE_MS`` (``emit_if_elapsed``, #2376).

    ``_flushed`` remembers the minute emitted early by count, until a later
    minute arrives. Without it, the resubscribed socket's first
    5-second bars — which may still belong to that minute — would either crash
    the run ("not found in open minute", because ``current`` is now ``None``)
    or rebuild an accumulator for a minute the consumer has already decided on.
    """

    current: _MinuteAccumulator | None = None
    last_source_ms: int | None = None
    counters: LiveBarCounters = field(default_factory=LiveBarCounters)
    #: The minute holding the first contribution this assembler ever accepted:
    #: the minute its stream joined, almost always partway through. Every print
    #: of it before the join was never seen, and nothing on the emitted bar
    #: says so -- only its count can prove it whole (#2364).
    join_minute_start_ms: int | None = field(default=None, init=False)
    _flushed: _MinuteAccumulator | None = field(default=None, init=False, repr=False)
    #: Whether ``_flushed`` was emitted short by the sparse-minute timer (#2376).
    _flushed_sparse: bool = field(default=False, init=False, repr=False)

    @property
    def open_minute_start_ms(self) -> int | None:
        return None if self.current is None else self.current.start_ms

    def completeness(self, bar: IbkrMinuteBar, *, touched: bool) -> MinuteCompleteness:
        """Classify one emitted minute; the one ordering both feed paths dispatch on (#2364).

        ``touched`` is the caller's fact that an interruption cut this minute
        open or landed in it (ruling P9, spec §4.2 rule 4); ``spans_interruption``
        counts as touched too. Order matters and lives only here:

        1. Twelve prints is every print a minute can hold, so a minute holding
           them is complete wherever it falls.
        2. A touched minute short of twelve is unprovable in every session
           phase -- short in RTH, undecidable outside it.
        3. The untouched minute the stream joined is short by construction
           (ruling R2): its prints before the join were never seen.
        4. Any other untouched minute owes the calendar its prints: a
           regular-session one (IBKR prints every 5 s in RTH) short of twelve
           is unprovable; outside RTH a sparse minute is normal. The phase is
           the one this assembler stamped from the canonical session
           authority, so a half-day's early close ends the floor exactly where
           the session ends.
        """
        if _is_complete_by_count(bar):
            return "complete"
        if touched or bar.spans_interruption:
            return "unprovable"
        if bar.start_ms == self.join_minute_start_ms:
            return "short_join"
        return "unprovable" if bar.session_phase == "RTH" else "complete"

    def _absorb_after_flush(self, raw_bar: object, *, symbol: str) -> bool:
        """Resolve a 5-second bar arriving after its minute was flushed early.

        Returns ``True`` when the bar was absorbed and must not reach the
        accumulator. A redelivery of the flushed minute's *most recent*
        contribution is absorbed: an exact one carries no new data and is
        skipped, and one with a different payload is a correction to a minute
        downstream has already consumed, so it is ignored -- never applied,
        never a rebuild -- and surfaced on
        ``LiveBarCounters.ignored_post_emit_correction`` and a WARNING. IBKR
        does redeliver the latest 5-second bar on a live subscription (the
        live relaxation in ``.claude/rules/temporal-rigor.md``), and a minute
        emitted on its twelfth print would otherwise die on the correction the
        open minute used to absorb. Any *earlier* timestamp inside the flushed
        minute belongs to an already-emitted aggregate and stays fatal,
        identical payload or not. A bar belonging to a later minute clears the
        memory and proceeds normally; one belonging to an earlier minute
        proceeds too, and the ordinary non-monotonic guard fails it.
        """
        flushed = self._flushed
        if flushed is None:
            return False
        source_ms = _bar_time_ms(raw_bar)
        if _minute_start_ms(source_ms) != flushed.start_ms:
            self._flushed = None
            return False
        if source_ms == self.last_source_ms:
            if flushed.contributions.get(source_ms) == _contribution(raw_bar):
                self.counters.skipped_duplicate += 1
                logger.info(
                    "Idempotent skip of a 5-second bar redelivered after its minute was flushed",
                    extra={"symbol": symbol, "source_ms": source_ms, "action": "skipped_duplicate"},
                )
                return True
            self.counters.ignored_post_emit_correction += 1
            logger.warning(
                "Ignored a correction to a 5-second bar whose minute was already emitted",
                extra={"symbol": symbol, "source_ms": source_ms, "action": "post_emit_correction_ignored"},
            )
            return True
        if (
            self._flushed_sparse
            and self.last_source_ms is not None
            and source_ms > self.last_source_ms
        ):
            # A minute the sparse-minute timer emitted short still has later
            # slots open. The decision on it is made, so a print that lands in
            # one is dropped and counted, never folded in (#2376). A minute
            # emitted by count holds all twelve, so there a later timestamp
            # stays the anomaly it always was.
            self.counters.ignored_late_print_after_emit += 1
            logger.warning(
                "Ignored a 5-second print that arrived after its sparse minute was emitted",
                extra={
                    "symbol": symbol,
                    "source_ms": source_ms,
                    "minute_start_ms": flushed.start_ms,
                    "action": "late_print_after_emit_ignored",
                },
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
        if self.join_minute_start_ms is None:
            self.join_minute_start_ms = self.current.start_ms
        # A minute proven complete by count is closed: emit it now rather than
        # hold it for the next minute's first print. That print can be a night
        # away -- an extended run's 19:59 ET minute otherwise waited for 04:00
        # the next trading day and was decided eight hours late (#2345).
        return emitted if emitted is not None else self._emit_if_complete()

    def emit_if_elapsed(self, now_ms: int) -> IbkrMinuteBar | None:
        """Emit a sparse extended-hours minute once its close is past by the grace (#2376).

        Driven by the healthy line's idle poll, so a PRE/POST minute short of
        twelve prints is decided on time instead of waiting for the next
        print. An RTH minute is never emitted this way: IBKR prints every 5 s
        there, so a short one is already unprovable, and holding it for its
        late twelfth print is what lets it be proven complete.
        """
        current = self.current
        if current is None or _session_phase_for_ms(current.start_ms) == "RTH":
            return None
        if now_ms < current.start_ms + 60_000 + SPARSE_MINUTE_EMIT_GRACE_MS:
            return None
        return self._flush_current(sparse=True)

    def _emit_if_complete(self) -> IbkrMinuteBar | None:
        """Emit the open minute now iff it already holds every RTH contribution."""
        if self.current is None or len(self.current.contributions) < RTH_CONTRIBUTIONS_PER_MINUTE:
            return None
        return self._flush_current(sparse=False)

    def _flush_current(self, *, sparse: bool) -> IbkrMinuteBar:
        """Close the open minute early and remember it for post-emit arrivals."""
        assert self.current is not None
        emitted = self.current.to_model()
        self._flushed = self.current
        self._flushed_sparse = sparse
        self.current = None
        return emitted
