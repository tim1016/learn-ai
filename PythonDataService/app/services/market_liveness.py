"""Compose live Alpaca clock and symbol-status evidence into one safe fact.

Calendar-backed session structure and real-time liveness answer different
questions (ADR 0022). The composer only evaluates live evidence; the shared
entry policy consults session authority separately for extended-hours admission.

IBKR subscription receipts prove current decision data independently of reported
halt state. A missing initial halt tick is permissible only with positive data
readiness; an explicit unavailable status or retained halt blocks entry. The
legacy Alpaca transition source retains its own semantics. The broker clock,
source publication, and decision-data ages have separate bounds (ADR 0067).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.broker.contract.models import BrokerClockEvidence
from app.marketdata.feed import FeedHealth
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    MarketStatusSnapshot,
    MarketStatusSource,
    SymbolMarketDataEvidence,
    SymbolTradingStatusEvidence,
    TopOfBookQuote,
)

# The broker clock is polled on a fixed interval (unlike the transition-only
# status stream), so wall-clock freshness is still the right check for it.
MARKET_CLOCK_MAX_AGE_MS = 5_000
STATUS_PUBLICATION_MAX_AGE_MS = 5_000
QUOTE_MAX_AGE_MS = 5_000
_UNKNOWN_CLOCK_SOURCE = "market_liveness.unavailable"

logger = logging.getLogger(__name__)


def _freshness_violation(
    now_ms: int,
    observed_at_ms: int,
    *,
    invalid_code: str,
    invalid_reason: str,
    stale_code: str,
    stale_reason: str,
) -> tuple[str, str] | None:
    """Return ``(reason_code, reason)`` if ``observed_at_ms`` fails the shared
    freshness bound (future-dated, or older than the clock's liveness
    boundary), else ``None``."""
    if now_ms < observed_at_ms:
        return invalid_code, invalid_reason
    if now_ms - observed_at_ms > MARKET_CLOCK_MAX_AGE_MS:
        return stale_code, stale_reason
    return None


def unknown_market_liveness(
    symbol: str,
    *,
    observed_at_ms: int,
    reason_code: str = "MARKET_LIVENESS_UNAVAILABLE",
    reason: str = "Live market-liveness evidence is unavailable.",
    market_data: SymbolMarketDataEvidence | None = None,
) -> MarketLivenessFact:
    """Return the typed fail-closed fact used before a live source is installed."""
    return MarketLivenessFact(
        symbol=symbol.upper(),
        state="UNKNOWN", market_data=market_data,
        observed_at_ms=observed_at_ms,
        market_clock=MarketClockLivenessEvidence(
            state="UNKNOWN",
            source=_UNKNOWN_CLOCK_SOURCE,
            observed_at_ms=observed_at_ms,
            reason=reason,
        ),
        symbol_status=None,
        reason_code=reason_code,
        reason=reason,
    )


def compose_market_liveness(
    symbol: str,
    *,
    now_ms: int,
    market_clock: MarketClockLivenessEvidence | None,
    connected: bool,
    connection_changed_at_ms: int,
    symbol_status: SymbolTradingStatusEvidence | None,
    market_data: SymbolMarketDataEvidence | None = None,
    require_market_data: bool = False,
) -> MarketLivenessFact:
    """Compose the market-wide clock and the status stream's connection and
    per-symbol evidence into one fail-closed fact.

    IBKR requires current subscription evidence in addition to clock and
    status. A reported halt always blocks; a missing initial status does not
    substitute for a quote or trade receipt.
    """
    normalized_symbol = symbol.upper()
    if market_clock is None:
        return unknown_market_liveness(
            normalized_symbol,
            observed_at_ms=now_ms,
            reason_code="MARKET_CLOCK_UNAVAILABLE",
            reason="No live broker clock evidence is available.",
            market_data=market_data,
        )
    clock_violation = _freshness_violation(
        now_ms,
        market_clock.observed_at_ms,
        invalid_code="MARKET_CLOCK_INVALID",
        invalid_reason="Live broker clock evidence is dated after the evaluation time.",
        stale_code="MARKET_CLOCK_STALE",
        stale_reason="Live broker clock evidence is older than the 5-second liveness boundary.",
    )
    if clock_violation is not None:
        reason_code, reason = clock_violation
        if reason_code == "MARKET_CLOCK_INVALID":
            # The clock is stamped locally at ingestion, so a future-dated
            # stamp is either a caller that captured its instant before the
            # evidence landed (#2256) or a wall clock that stepped backward.
            # Both block new exposure, so neither may be silent.
            logger.warning(
                "broker clock evidence is dated after the evaluation instant; new exposure is blocked",
                extra={
                    "action": "market_liveness_clock_future_dated",
                    "symbol": normalized_symbol,
                    "now_ms": now_ms,
                    "observed_at_ms": market_clock.observed_at_ms,
                    "lead_ms": market_clock.observed_at_ms - now_ms,
                },
            )
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            market_data=market_data,
            symbol_status=symbol_status,
            reason_code=reason_code,
            reason=reason,
        )
    # Checked before the market-clock CLOSED branch below: a symbol can be
    # individually halted at any time, independent of the broker-wide clock
    # (which is RTH-only and reports CLOSED through every extended session
    # regardless — see clock_liveness_evidence), and that record is latched
    # across reconnects specifically so it survives independent of the
    # stream's current connection state (see
    # MarketLivenessStore.clear_symbol_statuses). If the CLOSED branch
    # returned first, a proven extended-hours override at the
    # liveness_blocks_entry call site would see only "CLOSED" and never
    # learn the symbol was actually halted.
    blocking_status = (
        symbol_status
        if symbol_status is not None and symbol_status.symbol.upper() == normalized_symbol
        else None
    )
    if blocking_status is not None and blocking_status.state == "HALTED":
        return MarketLivenessFact(
            symbol=normalized_symbol,
            state="HALTED", market_data=market_data,
            observed_at_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_HALTED",
            reason=blocking_status.reason or "Live vendor evidence reports this symbol halted.",
        )
    if blocking_status is not None and blocking_status.state != "TRADABLE" and not (
        require_market_data and blocking_status.state == "NOT_REPORTED"
    ):
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            market_data=market_data,
            symbol_status=symbol_status,
            reason_code="SYMBOL_STATUS_UNKNOWN",
            reason=blocking_status.reason or "Live vendor evidence cannot prove this symbol is tradable.",
        )
    if require_market_data and market_data is not None and now_ms < market_data.observed_at_ms:
        # Same shape as the clock's future-dated refusal: a caller that
        # captured its instant before the tick publisher re-stamped this
        # evidence (#2257). It reads as MARKET_DATA_RECOVERING, so say why.
        logger.warning(
            "market-data evidence is dated after the evaluation instant; new exposure is blocked",
            extra={
                "action": "market_liveness_market_data_future_dated",
                "symbol": normalized_symbol,
                "now_ms": now_ms,
                "observed_at_ms": market_data.observed_at_ms,
                "lead_ms": market_data.observed_at_ms - now_ms,
            },
        )
    if require_market_data and (
        market_data is None or market_data.symbol != normalized_symbol
        or market_data.state != "READY" or market_data.valid_until_ms is None
        or not market_data.observed_at_ms <= now_ms <= market_data.valid_until_ms
    ):
        return _unknown(
            normalized_symbol, now_ms=now_ms, market_clock=market_clock,
            market_data=market_data, symbol_status=symbol_status,
            reason_code=("MARKET_DATA_STARTING" if market_data is None else market_data.reason_code if market_data.state != "READY" else "MARKET_DATA_RECOVERING"),
            reason=(market_data.reason if market_data is not None and market_data.state != "READY" else "Waiting for current live market data for this symbol."),
        )
    if market_clock.state == "CLOSED":
        return MarketLivenessFact(
            symbol=normalized_symbol,
            state="CLOSED", market_data=market_data,
            observed_at_ms=market_clock.observed_at_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="MARKET_CLOSED",
            reason="Fresh broker clock evidence reports the market closed.",
        )
    if market_clock.state != "OPEN":
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            market_data=market_data,
            symbol_status=symbol_status,
            reason_code="MARKET_CLOCK_UNKNOWN",
            reason="The live broker clock cannot prove whether the market is open.",
        )
    if not connected:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            market_data=market_data,
            symbol_status=symbol_status,
            reason_code="STATUS_STREAM_DISCONNECTED",
            reason="The live market-data trading-status source is not connected.",
        )
    return MarketLivenessFact(
        symbol=normalized_symbol,
        state="TRADABLE", market_data=market_data,
        # Not min(market_clock.observed_at_ms, connection_changed_at_ms):
        # the connection watermark stays fixed at the original connect
        # instant for the life of a long-lived healthy connection, so that
        # min() would make every TRADABLE fact look older by the second
        # long after the underlying evidence (the clock, re-polled and
        # re-validated fresh above every ~1s) stayed current — aging a
        # freshness-gated caller like ``evaluate_run_admission`` out of its
        # window a few seconds after every reconnect. Every input that
        # feeds this conclusion was just reconfirmed fresh as of ``now_ms``.
        observed_at_ms=now_ms,
        market_clock=market_clock,
        symbol_status=symbol_status,
        reason_code="MARKET_TRADABLE",
        reason=(
            "Fresh market-wide evidence, a connected trading-status stream, "
            "and no negative evidence prove this symbol tradable."
        ),
    )


def market_data_bars_live(health: FeedHealth | None) -> bool:
    """Whether the market-data feed is actually printing for this symbol.

    The only *live* evidence an extended session is really running. Alpaca's
    clock is RTH-only — it reports CLOSED through every scheduled PRE and
    POST session (see :func:`clock_liveness_evidence`), so "CLOSED" carries
    no extended-hours information at all, and the schedule alone cannot tell
    a live pre-market from an unscheduled closure. Bars can: a live extended
    session prints them and a closed venue does not.

    The one predicate behind both readings of that fact — this one over a
    raw :class:`FeedHealth`, and the clerk's ``StreamHealthGate``
    market-data channel, whose ``healthy`` is computed from it — so the ENTER
    gate and the submission-boundary recheck cannot disagree (#1671).
    """
    return health is not None and health.connected and not health.stale


def liveness_blocks_entry(
    liveness: MarketLivenessFact,
    *,
    use_rth: bool,
    extended_phase_proven: Callable[[], bool],
    extended_session_live: Callable[[], bool],
) -> bool:
    """Decide whether a live liveness fact should block one ENTER (#1671).

    HALTED and UNKNOWN always block — the foundational safety signal this
    module exists to provide. CLOSED is different: the broker clock behind
    it is RTH-only (see :func:`clock_liveness_evidence`), so for a non-RTH
    caller it cannot distinguish a genuinely closed market from an ordinary
    extended-hours session.

    Exempting that caller needs **two** facts, not one, and both are
    consulted lazily so every other branch resolves without paying for a
    lookup. ``extended_phase_proven`` says the *schedule* puts this instant
    in PRE or POST — a declared window or a fresh capability snapshot, never
    a live signal. ``extended_session_live`` says the venue is *actually*
    printing bars for this symbol right now. The schedule alone would admit
    new exposure straight through an unscheduled extended-hours closure,
    which the RTH-only clock reports exactly as it reports an ordinary
    extended session; the calendar answers "was this a scheduled session?"
    and the feed answers "is it live?" (ADR 0022). A symbol-specific halt is
    already refused upstream — ``compose_market_liveness`` resolves HALTED
    before the CLOSED branch, precisely so this exemption cannot see past it.

    The single shared predicate for both the ENTER gate
    (``bot_trade_strategy.py``) and its Clerk submission-boundary recheck
    (``runtime.py``), so the two can never silently diverge.
    """
    if liveness.state != "CLOSED":
        return liveness.state != "TRADABLE"
    if use_rth:
        return True
    return not (extended_phase_proven() and extended_session_live())


@dataclass(frozen=True)
class MarketEntryPolicy:
    """One configured entry policy shared by strategy, intake and submission."""

    symbol: str
    use_rth: bool
    capability_account_id: str | None
    extended_window: ExtendedHoursWindow | None
    clock: Callable[[], int]
    extended_session_live: Callable[[], bool]

    def refusal(self, liveness: MarketLivenessFact, *, generation: str | None = None) -> str | None:
        from app.services.market_data_capability_service import extended_phase_proven_at_ms

        if generation is not None and (
            liveness.market_data is None or liveness.market_data.generation != generation
        ):
            return "Market-data subscription changed after admission; current evidence must be admitted again."
        if liveness_blocks_entry(
            liveness, use_rth=self.use_rth,
            extended_phase_proven=lambda: extended_phase_proven_at_ms(
                now_ms=self.clock(), symbol=self.symbol,
                account_id=self.capability_account_id, extended_window=self.extended_window,
            ),
            extended_session_live=self.extended_session_live,
        ):
            return f"{liveness.reason_code}: {liveness.reason}"
        return None

    def submission_guard(
        self, read: Callable[[], MarketLivenessFact], *, admitted: MarketLivenessFact,
    ) -> Callable[[], str | None]:
        """Capture the admission generation with no dependence on caller locals."""
        generation = None if admitted.market_data is None else admitted.market_data.generation
        return lambda: self.refusal(read(), generation=generation)


def clock_liveness_evidence(clock: BrokerClockEvidence) -> MarketClockLivenessEvidence:
    """Translate the narrow broker clock input without giving it symbol authority."""
    return MarketClockLivenessEvidence(
        state="OPEN" if clock.is_open else "CLOSED",
        source=f"{clock.broker}.clock",
        observed_at_ms=clock.observed_at_ms,
        vendor_timestamp_ms=clock.vendor_timestamp_ms,
    )


def _unknown(
    symbol: str,
    *,
    now_ms: int,
    market_clock: MarketClockLivenessEvidence,
    market_data: SymbolMarketDataEvidence | None,
    symbol_status: SymbolTradingStatusEvidence | None,
    reason_code: str,
    reason: str,
) -> MarketLivenessFact:
    return MarketLivenessFact(
        symbol=symbol,
        state="UNKNOWN", market_data=market_data,
        # This is the time of the refusal, not the age of a connection or
        # latched halt. Underlying freshness is checked before composing it.
        observed_at_ms=now_ms,
        market_clock=market_clock,
        symbol_status=symbol_status,
        reason_code=reason_code,
        reason=reason,
    )


class MarketLivenessStore:
    """Process-local, source-ordered cache populated by the Alpaca consumer."""

    def __init__(self) -> None:
        self._market_clock: MarketClockLivenessEvidence | None = None
        self._symbol_statuses: dict[str, SymbolTradingStatusEvidence] = {}
        # Transport health is independent of per-symbol decision data.
        self._connected = False
        self._connection_changed_at_ms = 0
        self._upstream_observed_at_ms: int | None = None
        self._status_source = MarketStatusSource.ALPACA
        self._expected_source: MarketStatusSource | None = None
        self._requested_symbols: dict[str, int] = {}
        self._quotes: dict[str, TopOfBookQuote] = {}
        self._subscriptions: dict[str, SymbolMarketDataEvidence] = {}

    def require_source(self, source: MarketStatusSource) -> None:
        """Pin the configured provider; snapshots cannot disable its policy."""
        self._expected_source = self._status_source = MarketStatusSource(source)

    def request_symbol(self, symbol: str, *, now_ms: int) -> None:
        """Explicit, bounded preview/preparation demand; fact reads are pure."""
        self._requested_symbols[symbol.upper()] = now_ms

    def requested_symbols(self) -> tuple[str, ...]:
        """Explicit preparation requests within the last minute."""
        from app.utils.timestamps import now_ms_utc

        cutoff = now_ms_utc() - 60_000
        self._requested_symbols = {
            symbol: at for symbol, at in self._requested_symbols.items() if at >= cutoff
        }
        return tuple(self._requested_symbols)

    def observe_clock(self, clock: BrokerClockEvidence) -> None:
        """Record one fresh market-wide broker-clock observation."""
        candidate = clock_liveness_evidence(clock)
        current = self._market_clock
        if current is None or candidate.observed_at_ms >= current.observed_at_ms:
            self._market_clock = candidate

    def mark_clock_unavailable(self, *, observed_at_ms: int, reason: str) -> None:
        """Replace prior clock evidence so an outage cannot leave it usable."""
        self._market_clock = MarketClockLivenessEvidence(
            state="UNKNOWN",
            source=_UNKNOWN_CLOCK_SOURCE,
            observed_at_ms=observed_at_ms,
            reason=reason,
        )

    def mark_stream_connected(self, *, observed_at_ms: int) -> None:
        """Record the status stream's connection watermark going healthy."""
        if not self._connected:
            self._connected = True
            self._connection_changed_at_ms = observed_at_ms

    def mark_stream_disconnected(self, *, observed_at_ms: int) -> None:
        """Flip the connection watermark unhealthy on disconnect/shutdown."""
        self._connected = False
        self._connection_changed_at_ms = observed_at_ms

    def observe_symbol_status(self, evidence: SymbolTradingStatusEvidence) -> None:
        """Keep the newest vendor event for one normalized symbol."""
        symbol = evidence.symbol.upper()
        current = self._symbol_statuses.get(symbol)
        if (
            evidence.source == MarketStatusSource.IBKR and current is not None
            and current.state == "HALTED" and evidence.state in {"UNKNOWN", "NOT_REPORTED"}
        ):
            return
        if current is None or _status_order(evidence) >= _status_order(current):
            self._symbol_statuses[symbol] = evidence.model_copy(update={"symbol": symbol})

    def clear_symbol_statuses(self) -> None:
        """Invalidate all per-symbol status evidence.

        Called only at consumer start/stop (full lifecycle boundaries), never
        on an ordinary stream reconnect: Alpaca's status stream has no
        snapshot-on-subscribe and no REST fallback for current halt state, so
        a HALTED record is the only proof of a still-halted symbol once the
        socket reconnects. Wiping it on every reconnect would silently report
        that symbol TRADABLE the moment any frame arrives on the new
        connection, before a fresh transition ever confirms a resume.
        """
        self._symbol_statuses.clear()

    def status_snapshot(self, *, now_ms: int) -> MarketStatusSnapshot:
        """Export connection proof and unchanged vendor halt/resume evidence."""
        return MarketStatusSnapshot(
            source=self._status_source,
            connected=self._status_connected(now_ms),
            observed_at_ms=(
                now_ms if self._upstream_observed_at_ms is None
                else min(now_ms, self._upstream_observed_at_ms)
            ),
            connection_changed_at_ms=self._connection_changed_at_ms,
            symbol_statuses=tuple(self._symbol_statuses[key] for key in sorted(self._symbol_statuses)),
            quotes=tuple(self._quotes[key] for key in sorted(self._quotes)),
            subscriptions=tuple(self._subscriptions[key] for key in sorted(self._subscriptions)),
        )

    def apply_status_snapshot(self, snapshot: MarketStatusSnapshot, *, now_ms: int) -> None:
        """Import fresh source proof without refreshing or erasing vendor events."""
        source = MarketStatusSource(snapshot.source)
        if self._expected_source is not None and source is not self._expected_source:
            raise ValueError("Market-status snapshot does not match the configured provider.")
        if not 0 <= now_ms - snapshot.observed_at_ms <= STATUS_PUBLICATION_MAX_AGE_MS:
            raise ValueError("Shared market-status snapshot is stale or future-dated.")
        if self._upstream_observed_at_ms is not None and snapshot.observed_at_ms < self._upstream_observed_at_ms:
            raise ValueError("Shared market-status snapshot is older than retained evidence.")
        self._upstream_observed_at_ms = snapshot.observed_at_ms
        self._status_source = source
        self._connected = snapshot.connected
        self._connection_changed_at_ms = snapshot.connection_changed_at_ms
        self._subscriptions = {item.symbol: item for item in snapshot.subscriptions}
        for evidence in snapshot.symbol_statuses:
            self.observe_symbol_status(evidence)
        # The snapshot is the whole live book, so it *replaces* what is held:
        # a symbol it omits — a dropped subscription, a disconnect, a
        # reconnect before the book arrives — has no live quote, and keeping
        # the last one would let ``top_of_book`` answer a remembered price
        # inside the freshness window (Codex review 2026-09-19).
        self._quotes = {
            quote.symbol.upper(): quote.model_copy(update={"symbol": quote.symbol.upper()})
            for quote in snapshot.quotes
        }

    def _status_connected(self, now_ms: int) -> bool:
        return self._connected and (
            self._upstream_observed_at_ms is None
            or 0 <= now_ms - self._upstream_observed_at_ms <= STATUS_PUBLICATION_MAX_AGE_MS
        )

    def top_of_book(self, symbol: str, *, now_ms: int) -> TopOfBookQuote | None:
        """The live best bid and ask an operator may price a limit from, if fresh.

        Preparation registers demand explicitly. Reading a quote neither
        starts a subscription nor renews a broker receipt.
        """
        normalized_symbol = symbol.upper()
        quote = self._quotes.get(normalized_symbol)
        if (
            quote is None
            or not self._status_connected(now_ms)
            or not 0 <= now_ms - quote.observed_at_ms <= QUOTE_MAX_AGE_MS
        ):
            return None
        return quote

    def fact(self, symbol: str, *, now_ms: int) -> MarketLivenessFact:
        """Return the one current live fact for Start, panel, and Clerk gates."""
        normalized_symbol = symbol.upper()
        status = self._symbol_statuses.get(normalized_symbol)
        market_data = self._subscriptions.get(normalized_symbol)
        return compose_market_liveness(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=self._market_clock,
            connected=self._status_connected(now_ms),
            connection_changed_at_ms=self._connection_changed_at_ms,
            symbol_status=status,
            market_data=market_data, require_market_data=self._status_source.requires_live_data,
        )


def _status_order(evidence: SymbolTradingStatusEvidence) -> tuple[int, int]:
    if evidence.source == MarketStatusSource.IBKR:
        # IBKR halt ticks have a local callback receipt, no vendor timestamp.
        return evidence.observed_at_ms, evidence.observed_at_ms
    return evidence.source_timestamp_ms or evidence.observed_at_ms, evidence.observed_at_ms


_store = MarketLivenessStore()


def get_market_liveness_store() -> MarketLivenessStore:
    """Return the process-wide cache installed at service startup."""
    return _store


def market_liveness_fact(symbol: str, now_ms: int) -> MarketLivenessFact:
    """Resolve the shared live fact without consulting scheduled session logic."""
    return _store.fact(symbol, now_ms=now_ms)


def reset_market_liveness_store_for_testing() -> None:
    """Reset process state between isolated tests."""
    global _store
    _store = MarketLivenessStore()
