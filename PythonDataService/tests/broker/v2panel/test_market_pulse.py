from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.marketdata.feed import FeedHealth
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.services.broker_v2_panel import market_pulse
from app.services.market_liveness import compose_market_liveness


class _Feed:
    feed_id = "test-feed"

    def __init__(self, health: FeedHealth) -> None:
        self._health = health

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return self._health


@pytest.fixture(autouse=True)
def _fresh_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(symbol: str, now_ms: int):
        return compose_market_liveness(
            symbol or "SPY",
            now_ms=now_ms,
            market_clock=MarketClockLivenessEvidence(
                state="OPEN",
                source="test.clock",
                observed_at_ms=now_ms,
                vendor_timestamp_ms=now_ms,
            ),
            connected=True,
            connection_changed_at_ms=now_ms,
            symbol_status=SymbolTradingStatusEvidence(
                symbol=symbol or "SPY",
                state="TRADABLE",
                source="test.symbol-status",
                observed_at_ms=now_ms,
                source_timestamp_ms=now_ms,
            ),
        )

    monkeypatch.setattr(market_pulse, "market_liveness_fact", resolve)


def _session(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    monkeypatch.setattr(
        market_pulse,
        "session_state_at_ms",
        lambda **_kwargs: SimpleNamespace(
            phase=phase,
            source="nyse_calendar",
            extended_phase_proven=False,
        ),
        raising=False,
    )


def _admission_fact(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str,
    last_bar_ms: int | None,
    reason: str = "",
    stale: bool = False,
    active_subscription_count: int = 1,
) -> None:
    def fact(*args: object, **_kwargs: object) -> SimpleNamespace:
        session = market_pulse.session_state_at_ms(now_ms=args[1])
        return SimpleNamespace(
            state=state,
            feed_id="test-feed" if state != "UNAVAILABLE" else None,
            last_bar_ms=last_bar_ms,
            observed_at_ms=121_000,
            reason=reason,
            stale=stale,
            active_subscription_count=active_subscription_count,
            scheduled_phase=session.phase,
            session_authority_source=session.source,
            extended_phase_proven=session.extended_phase_proven,
        )

    monkeypatch.setattr(market_pulse, "market_data_admission_fact", fact)


def test_open_session_stale_feed_requires_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch, "RTH")
    _admission_fact(
        monkeypatch,
        state="STALE",
        last_bar_ms=1_000,
        reason="No bar arrived.",
    )
    feed = _Feed(
        FeedHealth(
            connected=True,
            stale=True,
            last_bar_ms=1_000,
            reason="No bar arrived.",
            active_subscription_count=1,
            observed_at_ms=121_000,
        )
    )

    pulse = market_pulse.build_market_pulse(
        feed,
        now_ms=121_000,
        use_rth=True,
        bot_running=True,
    )

    assert pulse.session == "OPEN"
    assert pulse.feed_state == "STALE"
    assert pulse.age_ms == 120_000
    assert pulse.attention_required is True
    assert pulse.next_step is not None


def test_closed_session_does_not_turn_idle_feed_into_false_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch, "CLOSED")
    _admission_fact(monkeypatch, state="AVAILABLE", last_bar_ms=1_000)
    feed = _Feed(
        FeedHealth(
            connected=True,
            stale=True,
            last_bar_ms=1_000,
            reason="No bar arrived.",
            active_subscription_count=1,
            observed_at_ms=121_000,
        )
    )

    pulse = market_pulse.build_market_pulse(
        feed,
        now_ms=121_000,
        use_rth=True,
        bot_running=True,
    )

    assert pulse.session == "CLOSED"
    assert pulse.feed_state == "LIVE"
    assert pulse.headline == "Market closed — no live bar expected"
    assert pulse.attention_required is False
    assert pulse.next_step is None


def test_missing_feed_is_visible_and_blocks_during_open_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch, "RTH")
    _admission_fact(monkeypatch, state="UNAVAILABLE", last_bar_ms=None)

    pulse = market_pulse.build_market_pulse(
        None,
        now_ms=121_000,
        use_rth=True,
        bot_running=False,
    )

    assert pulse.feed_state == "MISSING"
    assert pulse.attention_required is True
    assert pulse.latest_bar_at_ms is None


def test_extended_hours_bot_expects_premarket_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "PRE")
    _admission_fact(
        monkeypatch,
        state="STALE",
        last_bar_ms=1_000,
        reason="No extended-hours bar arrived.",
        stale=True,
    )
    feed = _Feed(
        FeedHealth(
            connected=True,
            stale=True,
            last_bar_ms=1_000,
            reason="No extended-hours bar arrived.",
            active_subscription_count=1,
            observed_at_ms=121_000,
        )
    )

    pulse = market_pulse.build_market_pulse(
        feed,
        now_ms=121_000,
        use_rth=False,
        bot_running=True,
    )

    assert pulse.session == "PRE_MARKET"
    assert pulse.feed_state == "STALE"
    assert pulse.attention_required is True


def test_extended_hours_without_capability_states_that_phase_is_unproved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "CLOSED")
    _admission_fact(monkeypatch, state="AVAILABLE", last_bar_ms=None)

    pulse = market_pulse.build_market_pulse(
        None,
        now_ms=121_000,
        use_rth=False,
        bot_running=False,
    )

    assert pulse.session == "CLOSED"
    assert pulse.headline == "Extended-session phase unproved"
    assert "capability" in pulse.explanation.lower()


def test_idle_feed_does_not_claim_to_be_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "RTH")
    _admission_fact(
        monkeypatch,
        state="AVAILABLE",
        last_bar_ms=None,
        reason="The connected feed is idle.",
        stale=True,
        active_subscription_count=0,
    )
    feed = _Feed(
        FeedHealth(
            connected=True,
            stale=True,
            last_bar_ms=None,
            reason="The connected feed is idle.",
            active_subscription_count=0,
            observed_at_ms=121_000,
        )
    )

    pulse = market_pulse.build_market_pulse(
        feed,
        now_ms=121_000,
        use_rth=True,
        bot_running=False,
    )

    assert pulse.feed_state == "IDLE"
    assert pulse.headline == "Market data ready — waiting for the run subscription"
    assert pulse.attention_required is False

    running_pulse = market_pulse.build_market_pulse(
        feed,
        now_ms=121_000,
        use_rth=True,
        bot_running=True,
    )
    assert running_pulse.feed_state == "IDLE"
    assert running_pulse.headline == "Market data idle for a running bot"
    assert running_pulse.attention_required is True


def test_open_scheduled_phase_shows_fresh_symbol_halt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "RTH")
    _admission_fact(monkeypatch, state="AVAILABLE", last_bar_ms=120_000)
    liveness = compose_market_liveness(
        "SPY",
        now_ms=121_000,
        market_clock=MarketClockLivenessEvidence(
            state="OPEN",
            source="test.clock",
            observed_at_ms=121_000,
            vendor_timestamp_ms=121_000,
        ),
        connected=True,
        connection_changed_at_ms=121_000,
        symbol_status=SymbolTradingStatusEvidence(
            symbol="SPY",
            state="HALTED",
            source="test.symbol-status",
            observed_at_ms=121_000,
            source_timestamp_ms=121_000,
        ),
    )

    pulse = market_pulse.build_market_pulse(
        None,
        now_ms=121_000,
        symbol="SPY",
        use_rth=True,
        bot_running=True,
        liveness=liveness,
    )

    assert pulse.session == "OPEN"
    assert pulse.market_state == "HALTED"
    assert pulse.headline == "Trading halted for"
    assert pulse.halted_symbol == "SPY"
    assert pulse.attention_required is True
