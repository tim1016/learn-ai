"""Unit coverage for the live market-liveness composition boundary (#1671)."""

from __future__ import annotations

from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services.market_liveness import (
    MARKET_LIVENESS_MAX_AGE_MS,
    compose_market_liveness,
)

_NOW = 1_700_000_000_000


def _clock(state: str = "OPEN", observed_at_ms: int = _NOW) -> MarketClockLivenessEvidence:
    return MarketClockLivenessEvidence(
        state=state,
        source="alpaca.clock",
        observed_at_ms=observed_at_ms,
        vendor_timestamp_ms=observed_at_ms,
    )


def _status(state: str = "TRADABLE", observed_at_ms: int = _NOW) -> SymbolTradingStatusEvidence:
    return SymbolTradingStatusEvidence(
        symbol="SPY",
        state=state,
        source="alpaca.stock_data.status",
        observed_at_ms=observed_at_ms,
        source_timestamp_ms=observed_at_ms,
    )


def test_open_clock_and_tradable_symbol_prove_tradability() -> None:
    fact = compose_market_liveness(
        "spy",
        now_ms=_NOW,
        market_clock=_clock(),
        symbol_status=_status(),
    )

    assert fact.symbol == "SPY"
    assert fact.state == "TRADABLE"
    assert fact.reason_code == "MARKET_TRADABLE"


def test_open_clock_and_halted_symbol_never_claim_tradability() -> None:
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("OPEN"),
        symbol_status=_status("HALTED"),
    )

    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_closed_market_clock_overrides_a_tradable_symbol_status() -> None:
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("CLOSED"),
        symbol_status=_status("TRADABLE"),
    )

    assert fact.state == "CLOSED"
    assert fact.reason_code == "MARKET_CLOSED"


def test_missing_or_stale_symbol_evidence_fails_closed() -> None:
    missing = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(),
        symbol_status=None,
    )
    stale = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(),
        symbol_status=_status(observed_at_ms=_NOW - MARKET_LIVENESS_MAX_AGE_MS - 1),
    )

    assert missing.state == "UNKNOWN"
    assert missing.reason_code == "SYMBOL_STATUS_UNAVAILABLE"
    assert stale.state == "UNKNOWN"
    assert stale.reason_code == "SYMBOL_STATUS_STALE"
