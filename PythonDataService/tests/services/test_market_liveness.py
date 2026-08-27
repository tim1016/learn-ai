"""Unit coverage for the live market-liveness composition boundary (#1671)."""

from __future__ import annotations

from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services.market_liveness import (
    MARKET_CLOCK_MAX_AGE_MS,
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
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status(),
    )

    assert fact.symbol == "SPY"
    assert fact.state == "TRADABLE"
    assert fact.reason_code == "MARKET_TRADABLE"


def test_tradable_fact_stays_fresh_on_a_long_lived_connection() -> None:
    """Regression: the connection watermark stays fixed at the original
    connect instant for a long-lived healthy connection. A TRADABLE fact's
    ``observed_at_ms`` must not be pinned to that stale instant — a
    freshness-gated caller (e.g. ``evaluate_run_admission``, 5s bound)
    would treat every TRADABLE fact as stale a few seconds after each
    reconnect even though the underlying clock evidence is re-polled and
    re-validated fresh every ~1s."""
    long_lived_connection_ms = _NOW - 60 * 60 * 1_000  # connected an hour ago

    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(observed_at_ms=_NOW),
        connected=True,
        connection_changed_at_ms=long_lived_connection_ms,
        symbol_status=None,
    )

    assert fact.state == "TRADABLE"
    assert fact.observed_at_ms == _NOW


def test_open_clock_and_halted_symbol_never_claim_tradability() -> None:
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("OPEN"),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status("HALTED"),
    )

    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_closed_market_clock_overrides_a_tradable_symbol_status() -> None:
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("CLOSED"),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status("TRADABLE"),
    )

    assert fact.state == "CLOSED"
    assert fact.reason_code == "MARKET_CLOSED"


def test_halted_symbol_status_overrides_a_closed_market_clock() -> None:
    """Regression: Alpaca's market clock is RTH-only and reports CLOSED
    through every extended session, so callers reconcile a CLOSED liveness
    fact against a proven extended-hours capability to allow non-RTH entries
    (see market_data_capability_service.extended_phase_proven_at_ms). Once that
    reconciliation actually works, this HALTED symbol status must still be
    the one that decides the outcome — if the CLOSED branch returned first
    without ever inspecting it, a proven-extended-hours caller would see a
    liveness fact reporting only "CLOSED" and never learn the symbol was
    genuinely halted, and liveness_blocks_entry would wave the ENTER
    through."""
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("CLOSED"),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status("HALTED"),
    )

    assert fact.state == "HALTED"
    assert fact.reason_code == "SYMBOL_HALTED"


def test_unrecognized_symbol_status_overrides_a_closed_market_clock() -> None:
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("CLOSED"),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status("UNKNOWN"),
    )

    assert fact.state == "UNKNOWN"
    assert fact.reason_code == "SYMBOL_STATUS_UNKNOWN"


def test_symbol_with_no_evidence_at_all_is_tradable_on_a_connected_stream() -> None:
    """Regression for #1671's core bug: Alpaca's status stream is
    transition-only (halt/resume), not a per-symbol heartbeat, so a symbol
    that simply never halts has no record here at all — that silence is the
    common, expected case, not missing proof. A symbol goes untouched for
    far longer than any old wall-clock freshness bound would have allowed
    (here, ten times it) and must still resolve TRADABLE as long as the
    stream itself is connected and the clock is fresh and open."""
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(observed_at_ms=_NOW),
        connected=True,
        connection_changed_at_ms=_NOW - (MARKET_CLOCK_MAX_AGE_MS * 10),
        symbol_status=None,
    )

    assert fact.state == "TRADABLE"
    assert fact.reason_code == "MARKET_TRADABLE"


def test_disconnected_stream_fails_closed_even_with_a_stale_tradable_record() -> None:
    """The stream connection — not a per-symbol timestamp — is what proves
    liveness now, so losing the connection must still fail closed even when
    the last-known record for this symbol was TRADABLE."""
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(),
        connected=False,
        connection_changed_at_ms=_NOW - 60_000,
        symbol_status=_status("TRADABLE", observed_at_ms=_NOW - 60_000),
    )

    assert fact.state == "UNKNOWN"
    assert fact.reason_code == "STATUS_STREAM_DISCONNECTED"


def test_unrecognized_symbol_status_fails_closed() -> None:
    """An actual, unrecognized-status record for this symbol is negative
    evidence and blocks — unlike no record at all, this is not silence."""
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status("UNKNOWN"),
    )

    assert fact.state == "UNKNOWN"
    assert fact.reason_code == "SYMBOL_STATUS_UNKNOWN"


def test_stale_market_clock_fails_closed() -> None:
    fact = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(observed_at_ms=_NOW - MARKET_CLOCK_MAX_AGE_MS - 1),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status(),
    )

    assert fact.state == "UNKNOWN"
    assert fact.reason_code == "MARKET_CLOCK_STALE"
