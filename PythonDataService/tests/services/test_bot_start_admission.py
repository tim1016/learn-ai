"""Tests for app.services.bot_start_admission.market_data_admission_fact.

Covers the Aug-7 canary regression for issue #1383: a connected-but-nonadvancing
IBKR feed must not authorize exposure. This is the one seam that was previously
untested end-to-end — tests/services/test_run_admission.py injects
MarketDataAdmissionFact directly, and tests/broker/v2panel/test_market_pulse.py
monkeypatches market_data_admission_fact itself, so neither exercises the real
FeedHealth -> MarketDataAdmissionFact translation.

The 2026-08-07 qualification made H1 and H3 release blockers; their regression
tests below require bounded fail-closed behavior. H2 remains a deliberate
candidate-subscription policy for a stopped bot:

- H1: source activity, not only completed minutes, now drives a 30-second
  stale bound after the first closed bar.
- H2: a stale feed with no active subscription resolves to AVAILABLE/idle
  rather than STALE, since stalled_subscription requires
  active_subscription_count > 0.
- H3: an active feed with no completed closed bar is stale immediately, so a
  one-print subscription cannot authorize exposure while the accumulator waits.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.services.bot_start_admission as bot_start_admission
from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.broker.alpaca.clerk.stream_health import market_data_channel_health
from app.marketdata.feed import FeedHealth
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.schemas.run_admission import (
    MarketDataAdmissionFact,
    RunProcessAdmissionFact,
    StartRunFacts,
    StartRuntimeAdmissionFact,
)
from app.services.bot_start_admission import market_data_admission_fact
from app.services.run_admission import evaluate_run_admission

_NOW = 1_700_000_010_000
_SID = "alpaca-start-1"


class _Feed:
    feed_id = "ibkr"

    def __init__(self, health: FeedHealth) -> None:
        self._health = health

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return self._health


class _RaisingFeed:
    feed_id = "ibkr"

    def health(self, _symbol: str | None = None) -> FeedHealth:
        raise RuntimeError("simulated health-probe failure")


def _fake_connected_client() -> MagicMock:
    """A minimal IbkrClient double: connected, never lost."""
    client = MagicMock()
    client.is_connected.return_value = True
    client.connection_lost = False
    return client


def _session(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    monkeypatch.setattr(
        bot_start_admission,
        "session_state_at_ms",
        lambda **_kwargs: SimpleNamespace(phase=phase),
    )


def _count(state: str = "zero") -> CustodyCountFact:
    return CustodyCountFact(state=state, count=0 if state == "zero" else None)


def _clerk(observed_at_ms: int = _NOW - 500) -> ClerkCustodySnapshot:
    """Flat, clean custody — isolates the market-data admission check."""
    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id="paper-account",
        strategy_instance_id=_SID,
        clerk_generation="clerk-1",
        journal_sequence=7,
        reconciliation_state="clean",
        reconciliation_fresh=True,
        reconciled_at_ms=observed_at_ms,
        exposure=CustodyExposureFact(state="zero", positions={}),
        working_orders=_count(),
        pending_orders=_count(),
        terminal_orders=_count(),
        unresolved_effects=_count(),
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code="CLERK_CUSTODY_PROVEN",
        evidence_refs=("clerk:paper-account:7",),
        observed_at_ms=observed_at_ms,
    )


def _start_facts(
    market_data: MarketDataAdmissionFact, *, observed_at_ms: int = _NOW - 1_000
) -> StartRunFacts:
    return StartRunFacts(
        strategy_instance_id=_SID,
        proposed_run_id="run-new",
        configuration_hash="a" * 64,
        runtime=StartRuntimeAdmissionFact(
            state="READY",
            observed_at_ms=observed_at_ms,
            explanation="The bot runtime is ready for Start.",
        ),
        process=RunProcessAdmissionFact(
            state="ABSENT",
            run_id=None,
            process_identity=None,
            registry_generation="registry-1",
            observed_at_ms=observed_at_ms,
        ),
        market_data=market_data,
    )


# ── the headline regression ─────────────────────────────────────────────


def test_connected_nonadvancing_feed_during_rth_is_stale_and_blocks_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected-but-nonadvancing feed cannot authorize exposure.

    This is the Aug-7 canary shape: the IBKR socket stayed connected, but the
    feed stopped advancing well past its stale threshold during RTH with an
    active subscription. That must resolve to STALE at the admission-fact
    layer and refuse Start end-to-end.
    """
    _session(monkeypatch, "RTH")
    health = FeedHealth(
        connected=True,
        stale=True,
        last_bar_ms=_NOW - 400_000,
        reason="No bar in 400s (threshold 180s)",
        active_subscription_count=1,
        observed_at_ms=_NOW - 1_000,
    )
    feed = _Feed(health)

    fact = market_data_admission_fact(feed, _NOW - 1_000, use_rth=True)
    assert fact.state == "STALE"

    decision = evaluate_run_admission(
        _start_facts(fact), _clerk(), evaluated_at_ms=_NOW
    )

    assert decision.allowed is False
    assert decision.reason_code == "MARKET_DATA_STALE"


# ── the other admission-fact states ─────────────────────────────────────


def test_market_data_admission_fact_unavailable_when_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "RTH")
    health = FeedHealth(
        connected=False,
        stale=False,
        last_bar_ms=None,
        reason="IBKR connection lost",
        active_subscription_count=0,
        observed_at_ms=_NOW - 1_000,
    )
    feed = _Feed(health)

    fact = market_data_admission_fact(feed, _NOW - 1_000, use_rth=True)

    assert fact.state == "UNAVAILABLE"


def test_market_data_admission_fact_unknown_when_health_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing health probe fails closed to UNKNOWN, not AVAILABLE."""
    feed = _RaisingFeed()

    fact = market_data_admission_fact(feed, _NOW - 1_000, use_rth=True)

    assert fact.state == "UNKNOWN"
    assert fact.feed_id == "ibkr"

    decision = evaluate_run_admission(
        _start_facts(fact), _clerk(), evaluated_at_ms=_NOW
    )

    assert decision.allowed is False
    assert decision.reason_code == "MARKET_DATA_UNKNOWN"


def test_market_data_admission_fact_available_when_advancing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "RTH")
    health = FeedHealth(
        connected=True,
        stale=False,
        last_bar_ms=_NOW - 5_000,
        reason="",
        active_subscription_count=1,
        observed_at_ms=_NOW - 1_000,
    )
    feed = _Feed(health)

    fact = market_data_admission_fact(feed, _NOW - 1_000, use_rth=True)

    assert fact.state == "AVAILABLE"


# ── bounded liveness regressions plus the one remaining candidate policy ──


def test_source_stall_is_bounded_at_thirty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411 H1: raw source activity bounds liveness between closed minutes."""
    fixed_now = _NOW - 1_000
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: fixed_now)
    _session(monkeypatch, "RTH")

    feed = IbkrMarketDataFeed(_fake_connected_client())
    state = feed._state_for("SPY")
    state.last_bar_wall_ms = fixed_now - 170_000
    state.last_bar_ms = fixed_now - 170_000
    state.last_source_wall_ms = fixed_now - 31_000
    state.first_bar_seen = True
    state.active_count = 1

    health = feed.health()
    assert health.stale is True

    fact = market_data_admission_fact(feed, fixed_now, use_rth=True)

    assert fact.state == "STALE"


def test_never_advanced_feed_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1411 H3: no completed bar means admission and submission are closed."""
    far_future = _NOW + 60 * 60 * 1_000
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: far_future)
    _session(monkeypatch, "RTH")

    feed = IbkrMarketDataFeed(_fake_connected_client())
    feed._state_for("SPY").active_count = 1  # a run is actively subscribed
    # last_bar_ms / last_bar_wall_ms deliberately remain None, and
    # first_bar_seen remains False: no bar completed, as in the 2026-08-07 canary.

    health = feed.health()
    assert health.connected is True
    assert health.stale is True
    assert health.last_bar_ms is None

    fact = market_data_admission_fact(feed, far_future, use_rth=True)
    assert fact.state == "STALE"

    decision = evaluate_run_admission(
        _start_facts(fact, observed_at_ms=far_future),
        _clerk(observed_at_ms=far_future),
        evaluated_at_ms=far_future,
    )
    assert decision.allowed is False

    channel_health = market_data_channel_health(health, now_ms=far_future)
    assert channel_health.healthy is False


def test_admission_uses_the_requested_symbols_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy SPY sibling cannot authorize a never-advanced AAPL run."""
    fixed_now = _NOW + 60 * 60 * 1_000
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: fixed_now)
    _session(monkeypatch, "RTH")
    feed = IbkrMarketDataFeed(_fake_connected_client())
    spy = feed._state_for("SPY")
    spy.active_count = 1
    spy.first_bar_seen = True
    spy.last_bar_ms = fixed_now - 60_000
    spy.last_bar_wall_ms = fixed_now - 5_000
    spy.last_source_wall_ms = fixed_now - 5_000
    aapl = feed._state_for("AAPL")
    aapl.active_count = 1

    spy_fact = market_data_admission_fact(
        feed,
        fixed_now,
        symbol="SPY",
        use_rth=True,
    )
    aapl_fact = market_data_admission_fact(
        feed,
        fixed_now,
        symbol="AAPL",
        use_rth=True,
    )

    assert spy_fact.state == "AVAILABLE"
    assert aapl_fact.state == "STALE"


def test_stale_feed_with_no_active_subscription_is_idle_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H2: a stale feed with no active subscription resolves to AVAILABLE/idle.

    stalled_subscription requires active_subscription_count > 0, so a feed
    that is stale but has no attached consumer is read as idle rather than
    STALE. This is correct for the "waiting for a run to subscribe" case, but
    it means a feed that went stale between runs offers no positive proof of
    liveness before the next Start. Tracked, deferred limitation — see the
    2026-08-07 incident note.
    """
    _session(monkeypatch, "RTH")
    health = FeedHealth(
        connected=True,
        stale=True,
        last_bar_ms=None,
        reason="The connected feed is idle; a new run may establish the candidate subscription.",
        active_subscription_count=0,
        observed_at_ms=_NOW - 1_000,
    )
    feed = _Feed(health)

    fact = market_data_admission_fact(feed, _NOW - 1_000, use_rth=True)

    assert fact.state == "AVAILABLE"
    assert "idle" in (fact.reason or "").lower()
