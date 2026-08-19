"""Compose live Alpaca clock and symbol-status evidence into one safe fact.

This module intentionally has no calendar dependency. Calendar-backed session
structure and real-time liveness answer different questions (ADR 0022): callers
receive the scheduled phase separately, while this module owns only the live
operational answer used by Start/Resume, V2 pulse, and new-exposure effects.
"""

from __future__ import annotations

from app.broker.contract.models import BrokerClockEvidence
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)

MARKET_LIVENESS_MAX_AGE_MS = 5_000
_UNKNOWN_CLOCK_SOURCE = "market_liveness.unavailable"


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
    symbol_status: SymbolTradingStatusEvidence | None,
) -> MarketLivenessFact:
    """Compose fresh market-wide and symbol-scoped evidence fail-closed."""
    normalized_symbol = symbol.upper()
    if market_clock is None:
        return unknown_market_liveness(
            normalized_symbol,
            observed_at_ms=now_ms,
            reason_code="MARKET_CLOCK_UNAVAILABLE",
            reason="No live broker clock evidence is available.",
        )
    if now_ms < market_clock.observed_at_ms:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="MARKET_CLOCK_INVALID",
            reason="Live broker clock evidence is dated after the evaluation time.",
        )
    if now_ms - market_clock.observed_at_ms > MARKET_LIVENESS_MAX_AGE_MS:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="MARKET_CLOCK_STALE",
            reason="Live broker clock evidence is older than the 5-second liveness boundary.",
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
            symbol_status=symbol_status,
            reason_code="MARKET_CLOCK_UNKNOWN",
            reason="The live broker clock cannot prove whether the market is open.",
        )
    if symbol_status is None or symbol_status.symbol.upper() != normalized_symbol:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_STATUS_UNAVAILABLE",
            reason="No live trading-status evidence is available for this symbol.",
        )
    if now_ms < symbol_status.observed_at_ms:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_STATUS_INVALID",
            reason="Symbol trading-status evidence is dated after the evaluation time.",
        )
    if now_ms - symbol_status.observed_at_ms > MARKET_LIVENESS_MAX_AGE_MS:
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_STATUS_STALE",
            reason="Symbol trading-status evidence is older than the 5-second liveness boundary.",
        )
    if symbol_status.state == "HALTED":
        return MarketLivenessFact(
            symbol=normalized_symbol,
            state="HALTED",
            observed_at_ms=min(market_clock.observed_at_ms, symbol_status.observed_at_ms),
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_HALTED",
            reason=symbol_status.reason or "Fresh vendor evidence reports this symbol halted.",
        )
    if symbol_status.state != "TRADABLE":
        return _unknown(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=market_clock,
            symbol_status=symbol_status,
            reason_code="SYMBOL_STATUS_UNKNOWN",
            reason="Live vendor evidence cannot prove this symbol is tradable.",
        )
    return MarketLivenessFact(
        symbol=normalized_symbol,
        state="TRADABLE",
        observed_at_ms=min(market_clock.observed_at_ms, symbol_status.observed_at_ms),
        market_clock=market_clock,
        symbol_status=symbol_status,
        reason_code="MARKET_TRADABLE",
        reason="Fresh market-wide and symbol-scoped evidence proves this symbol tradable.",
    )


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
    symbol_status: SymbolTradingStatusEvidence | None,
    reason_code: str,
    reason: str,
) -> MarketLivenessFact:
    return MarketLivenessFact(
        symbol=symbol,
        state="UNKNOWN",
        observed_at_ms=min(
            market_clock.observed_at_ms,
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

    def observe_symbol_status(self, evidence: SymbolTradingStatusEvidence) -> None:
        """Keep the newest vendor event for one normalized symbol."""
        symbol = evidence.symbol.upper()
        current = self._symbol_statuses.get(symbol)
        if current is None or _status_order(evidence) >= _status_order(current):
            self._symbol_statuses[symbol] = evidence.model_copy(update={"symbol": symbol})

    def clear_symbol_statuses(self) -> None:
        """Invalidate pre-disconnect status evidence before a reconnect begins."""
        self._symbol_statuses.clear()

    def fact(self, symbol: str, *, now_ms: int) -> MarketLivenessFact:
        """Return the one current live fact for Start, panel, and Clerk gates."""
        normalized_symbol = symbol.upper()
        return compose_market_liveness(
            normalized_symbol,
            now_ms=now_ms,
            market_clock=self._market_clock,
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
