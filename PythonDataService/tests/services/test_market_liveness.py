"""Unit coverage for the live market-liveness composition boundary (#1671)."""

from __future__ import annotations

import pytest

from app.marketdata.feed import FeedHealth
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services.market_liveness import (
    MARKET_CLOCK_MAX_AGE_MS,
    compose_market_liveness,
    liveness_blocks_entry,
    market_data_bars_live,
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


def test_future_dated_market_clock_fails_closed_and_logs_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """#2256: the refusal used to leave no trace, so an operator saw
    "Broker clock invalid" with no way to learn why."""
    with caplog.at_level("WARNING", logger="app.services.market_liveness"):
        fact = compose_market_liveness(
            "SPY",
            now_ms=_NOW,
            market_clock=_clock(observed_at_ms=_NOW + 40),
            connected=True,
            connection_changed_at_ms=_NOW,
            symbol_status=_status(),
        )

    assert (fact.state, fact.reason_code) == ("UNKNOWN", "MARKET_CLOCK_INVALID")
    [record] = [r for r in caplog.records if getattr(r, "action", None) == "market_liveness_clock_future_dated"]
    assert (record.now_ms, record.observed_at_ms, record.lead_ms) == (_NOW, _NOW + 40, 40)


def _health(*, connected: bool, stale: bool) -> FeedHealth:
    return FeedHealth(
        connected=connected,
        stale=stale,
        last_bar_ms=_NOW - 60_000,
        reason="" if connected and not stale else "test",
        active_subscription_count=1,
        observed_at_ms=_NOW,
    )


def test_only_a_connected_unstale_feed_proves_bars_are_printing() -> None:
    assert market_data_bars_live(_health(connected=True, stale=False)) is True
    assert market_data_bars_live(_health(connected=True, stale=True)) is False
    assert market_data_bars_live(_health(connected=False, stale=False)) is False
    assert market_data_bars_live(None) is False


def test_a_closed_clock_needs_both_the_schedule_and_live_bars_to_admit_an_enter() -> None:
    """ADR 0022: the calendar owns scheduled structure, the feed owns liveness.

    The declared window resolves the same PRE/POST phase during an
    unscheduled venue closure as during a live session, because Alpaca's
    clock is RTH-only and reports CLOSED through both. Only the feed
    printing bars separates them.
    """
    closed = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock("CLOSED"),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=None,
    )

    def blocks(*, proven: bool, live: bool, use_rth: bool = False) -> bool:
        return liveness_blocks_entry(
            closed,
            use_rth=use_rth,
            extended_phase_proven=lambda: proven,
            extended_session_live=lambda: live,
        )

    assert blocks(proven=True, live=True) is False
    assert blocks(proven=True, live=False) is True
    assert blocks(proven=False, live=True) is True
    assert blocks(proven=True, live=True, use_rth=True) is True


def test_neither_extended_predicate_is_consulted_off_the_closed_branch() -> None:
    """Both stay lazy: every other state resolves without a lookup."""

    def _never() -> bool:
        raise AssertionError("the extended-hours predicates must not be consulted here")

    tradable = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status(),
    )
    halted = compose_market_liveness(
        "SPY",
        now_ms=_NOW,
        market_clock=_clock(),
        connected=True,
        connection_changed_at_ms=_NOW,
        symbol_status=_status("HALTED"),
    )

    assert (
        liveness_blocks_entry(
            tradable,
            use_rth=False,
            extended_phase_proven=_never,
            extended_session_live=_never,
        )
        is False
    )
    assert (
        liveness_blocks_entry(
            halted,
            use_rth=False,
            extended_phase_proven=_never,
            extended_session_live=_never,
        )
        is True
    )


def test_composer_always_retains_the_subscription_evidence_it_evaluates() -> None:
    from app.schemas.market_liveness import SymbolMarketDataEvidence

    data = SymbolMarketDataEvidence(
        symbol="SPY", generation="one", state="READY", observed_at_ms=1000,
        valid_until_ms=6000, reason_code="MARKET_DATA_READY", reason="Live",
    )
    for state in ("OPEN", "CLOSED", "UNKNOWN"):
        for at in (1000, 6001):
            result = compose_market_liveness(
                "SPY", now_ms=at,
                market_clock=MarketClockLivenessEvidence(state=state, source="test.clock", observed_at_ms=at),
                connected=True, connection_changed_at_ms=0, symbol_status=None,
                market_data=data, require_market_data=True,
            )
            assert result.market_data == data


def test_configured_ibkr_provider_cannot_be_downgraded_by_a_snapshot() -> None:
    from pydantic import ValidationError

    from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource
    from app.services.market_liveness import MarketLivenessStore

    store = MarketLivenessStore()
    store.require_source(MarketStatusSource.IBKR)
    snapshot = MarketStatusSnapshot(
        source=MarketStatusSource.ALPACA, connected=True, observed_at_ms=1000,
        connection_changed_at_ms=1000, symbol_statuses=(),
    )
    with pytest.raises(ValueError, match="configured provider"):
        store.apply_status_snapshot(snapshot, now_ms=1000)
    with pytest.raises(ValidationError):
        MarketStatusSnapshot.model_validate({**snapshot.model_dump(), "source": "ibkr.market_data.statu"})


def test_future_dated_market_data_fails_closed_and_logs_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """#2257: READY evidence evaluated before it was published reads as
    MARKET_DATA_RECOVERING; the refusal must say why."""
    from app.schemas.market_liveness import SymbolMarketDataEvidence

    market_data = SymbolMarketDataEvidence(
        symbol="SPY", generation="gen-1", state="READY", observed_at_ms=_NOW + 40,
        valid_until_ms=_NOW + 5_040, reason_code="MARKET_DATA_READY", reason="ready",
    )
    with caplog.at_level("WARNING", logger="app.services.market_liveness"):
        fact = compose_market_liveness(
            "SPY", now_ms=_NOW, market_clock=_clock(), connected=True,
            connection_changed_at_ms=_NOW, symbol_status=_status(),
            market_data=market_data, require_market_data=True,
        )

    assert (fact.state, fact.reason_code) == ("UNKNOWN", "MARKET_DATA_RECOVERING")
    [record] = [r for r in caplog.records if getattr(r, "action", None) == "market_liveness_market_data_future_dated"]
    assert (record.now_ms, record.observed_at_ms, record.lead_ms) == (_NOW, _NOW + 40, 40)
