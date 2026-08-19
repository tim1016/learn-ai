"""Compose live Alpaca clock and symbol-status evidence into one safe fact.

This module intentionally has no calendar dependency. Calendar-backed session
structure and real-time liveness answer different questions (ADR 0022): callers
receive the scheduled phase separately, while this module owns only the live
operational answer used by Start/Resume, V2 pulse, and new-exposure effects.

Symbol-status freshness is event-ordered rather than wall-clock-expiring, the
same principle ``app.broker.alpaca.trade_updates`` uses for the execution
stream: Alpaca's trading-status stream sends halt/resume *transitions*, not a
per-symbol heartbeat, so a symbol that simply never halts has no event to stay
"fresh" against. What actually proves a quiet symbol is still tradable is the
status *stream's connection*, not a 5-second-old positive record for that one
symbol — treating silence as missing proof blocked essentially all new
exposure within seconds of every reconnect. A symbol is blocked only by
*negative* evidence (a HALTED or unrecognized-status record that hasn't since
been superseded by a resume) or by the stream being disconnected outright.
"""

from __future__ import annotations

from collections.abc import Callable

from app.broker.contract.models import BrokerClockEvidence
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)

# The broker clock is polled on a fixed interval (unlike the transition-only
# status stream), so wall-clock freshness is still the right check for it.
MARKET_CLOCK_MAX_AGE_MS = 5_000
_UNKNOWN_CLOCK_SOURCE = "market_liveness.unavailable"


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
) -> MarketLivenessFact:
    """Return the typed fail-closed fact used before a live source is installed."""
    return MarketLivenessFact(
        symbol=symbol.upper(),
        state="UNKNOWN",
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
) -> MarketLivenessFact:
    """Compose the market-wide clock and the status stream's connection and
    per-symbol evidence into one fail-closed fact.

    A symbol is blocked only by *negative* evidence: a HALTED or
    unrecognized-status record not yet superseded by a resume, or the status
    stream being disconnected outright. A symbol with no record at all is
    exactly the common case — one that has simply never halted — and
    proceeds toward TRADABLE; see the module docstring.
    """
    normalized_symbol = symbol.upper()
    if market_clock is None:
        return unknown_market_liveness(
            normalized_symbol,
            observed_at_ms=now_ms,
            reason_code="MARKET_CLOCK_UNAVAILABLE",
            reason="No live broker clock evidence is available.",
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
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            connection_changed_at_ms=connection_changed_at_ms,
            symbol_status=symbol_status,
            reason_code=reason_code,
            reason=reason,
        )
    if market_clock.state == "CLOSED":
        return MarketLivenessFact(
            symbol=normalized_symbol,
            state="CLOSED",
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
            connection_changed_at_ms=connection_changed_at_ms,
            symbol_status=symbol_status,
            reason_code="MARKET_CLOCK_UNKNOWN",
            reason="The live broker clock cannot prove whether the market is open.",
        )
    if not connected:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            connection_changed_at_ms=connection_changed_at_ms,
            symbol_status=symbol_status,
            reason_code="STATUS_STREAM_DISCONNECTED",
            reason="The live Alpaca trading-status stream is not connected.",
        )
    blocking_status = (
        symbol_status
        if symbol_status is not None and symbol_status.symbol.upper() == normalized_symbol
        else None
    )
    if blocking_status is not None and blocking_status.state == "HALTED":
        return MarketLivenessFact(
            symbol=normalized_symbol,
            state="HALTED",
            observed_at_ms=min(market_clock.observed_at_ms, blocking_status.observed_at_ms),
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_HALTED",
            reason=blocking_status.reason or "Live vendor evidence reports this symbol halted.",
        )
    if blocking_status is not None and blocking_status.state != "TRADABLE":
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            connection_changed_at_ms=connection_changed_at_ms,
            symbol_status=symbol_status,
            reason_code="SYMBOL_STATUS_UNKNOWN",
            reason="Live vendor evidence cannot prove this symbol is tradable.",
        )
    return MarketLivenessFact(
        symbol=normalized_symbol,
        state="TRADABLE",
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


def liveness_blocks_entry(
    liveness: MarketLivenessFact,
    *,
    use_rth: bool,
    extended_phase_proven: Callable[[], bool],
) -> bool:
    """Decide whether a live liveness fact should block one ENTER (#1671).

    HALTED and UNKNOWN always block — the foundational safety signal this
    module exists to provide. CLOSED is different: the broker clock behind
    it is RTH-only (see :func:`clock_liveness_evidence`), so for a non-RTH
    caller it cannot distinguish a genuinely closed market from an ordinary
    extended-hours session — only in that specific case is
    ``extended_phase_proven`` consulted, and lazily: every other branch
    resolves without paying for a capability-service lookup.

    The single shared predicate for both the ENTER gate
    (``bot_trade_strategy.py``) and its Clerk submission-boundary recheck
    (``runtime.py``), so the two can never silently diverge.
    """
    if liveness.state != "CLOSED":
        return liveness.state != "TRADABLE"
    if use_rth:
        return True
    return not extended_phase_proven()


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
    connection_changed_at_ms: int,
    symbol_status: SymbolTradingStatusEvidence | None,
    reason_code: str,
    reason: str,
) -> MarketLivenessFact:
    return MarketLivenessFact(
        symbol=symbol,
        state="UNKNOWN",
        observed_at_ms=min(
            market_clock.observed_at_ms,
            connection_changed_at_ms,
            symbol_status.observed_at_ms if symbol_status is not None else now_ms,
        ),
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
        # Status-stream socket-cycle watermark (mirrors
        # app.broker.alpaca.trade_updates's ``_connected``): True from the
        # first accepted frame of a connection cycle until that source is
        # exhausted or errors. Proves a quiet, never-halted symbol is still
        # tradable — the per-symbol map alone cannot, since silence there is
        # the expected, common case.
        self._connected = False
        self._connection_changed_at_ms = 0

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

    def fact(self, symbol: str, *, now_ms: int) -> MarketLivenessFact:
        """Return the one current live fact for Start, panel, and Clerk gates."""
        normalized_symbol = symbol.upper()
        return compose_market_liveness(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=self._market_clock,
            connected=self._connected,
            connection_changed_at_ms=self._connection_changed_at_ms,
            symbol_status=self._symbol_statuses.get(normalized_symbol),
        )


def _status_order(evidence: SymbolTradingStatusEvidence) -> tuple[int, int]:
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
