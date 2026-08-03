from __future__ import annotations

from types import SimpleNamespace

from app.marketdata.feed import FeedHealth
from app.services.broker_v2_panel import market_pulse


class _Feed:
    feed_id = "test-feed"

    def __init__(self, health: FeedHealth) -> None:
        self._health = health

    def health(self) -> FeedHealth:
        return self._health


def _session(monkeypatch, phase: str) -> None:
    monkeypatch.setattr(
        market_pulse,
        "session_state_at_ms",
        lambda **_kwargs: SimpleNamespace(phase=phase),
    )


def _admission_fact(monkeypatch, *, state: str, last_bar_ms: int | None, reason: str = "") -> None:
    monkeypatch.setattr(
        market_pulse,
        "market_data_admission_fact",
        lambda *_args, **_kwargs: SimpleNamespace(
            state=state,
            feed_id="test-feed" if state != "UNAVAILABLE" else None,
            last_bar_ms=last_bar_ms,
            observed_at_ms=121_000,
            reason=reason,
        ),
    )


def test_open_session_stale_feed_requires_attention(monkeypatch) -> None:
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

    pulse = market_pulse.build_market_pulse(feed, now_ms=121_000)

    assert pulse.session == "OPEN"
    assert pulse.feed_state == "STALE"
    assert pulse.age_ms == 120_000
    assert pulse.attention_required is True
    assert pulse.next_step is not None


def test_closed_session_does_not_turn_idle_feed_into_false_alarm(monkeypatch) -> None:
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

    pulse = market_pulse.build_market_pulse(feed, now_ms=121_000)

    assert pulse.session == "CLOSED"
    assert pulse.feed_state == "LIVE"
    assert pulse.headline == "Market closed — no live bar expected"
    assert pulse.attention_required is False
    assert pulse.next_step is None


def test_missing_feed_is_visible_and_blocks_during_open_session(monkeypatch) -> None:
    _session(monkeypatch, "RTH")
    _admission_fact(monkeypatch, state="UNAVAILABLE", last_bar_ms=None)

    pulse = market_pulse.build_market_pulse(None, now_ms=121_000)

    assert pulse.feed_state == "MISSING"
    assert pulse.attention_required is True
    assert pulse.latest_bar_at_ms is None
