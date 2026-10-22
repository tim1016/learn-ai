"""Expected preparation remains visible and never supplies order evidence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.stream_health import StreamHealthGate, market_data_channel_health, stream_health_refusal
from app.marketdata.ibkr_feed import IbkrMarketDataFeed
from app.services.broker_v2_panel.bot_page_projection import _channel_line
from app.services.broker_v2_panel.channel_health import channel_state
from app.services.broker_v2_panel.panel_projection_service import _channel_views
from tests._helpers.ibkr_feed_adversarial import NeverFirstBarFeedFixture


def presentation(feed: IbkrMarketDataFeed, now_ms: int) -> Any:
    channel = market_data_channel_health(feed.health("SPY"), now_ms=now_ms)
    gate = StreamHealthGate(
        market_data=lambda: channel,
        execution=lambda: ChannelHealth(stream="execution", healthy=True, connected=True, observed_at_ms=now_ms),
    )
    assert stream_health_refusal(gate) is not None
    return _channel_views(SimpleNamespace(channel_healths=[channel]), now_ms)[0]


async def test_preopen_first_source_wait_is_preparation_and_missing_rth_data_becomes_a_fault(monkeypatch: Any) -> None:
    fixture = NeverFirstBarFeedFixture()
    # RTH opening 2026-10-20 from the fixture calendar; no hardcoded session clock.
    from app.lean_sidecar.trading_calendar import session_open_ms_utc
    from app.utils.session_anchors import et_date_at_ms

    opening = session_open_ms_utc(et_date_at_ms(1792503000000))
    fixture.clock.advance_ms(opening - 60_000 - fixture.clock.now_ms())
    fixture.install(monkeypatch)
    feed = IbkrMarketDataFeed(fixture.client)
    stream = feed.stream_bars("SPY")
    pending = asyncio.create_task(anext(stream))
    try:
        await fixture.wait_until_subscribed()
        fact = feed.health("SPY")
        assert fact.waiting and not fact.warming and fact.stale
        view = presentation(feed, fixture.clock.now_ms())
        assert view.state == "warming"
        line = _channel_line(view, running=True)
        assert line.state == "attention" and "down" not in line.note.lower()
        fixture.clock.advance_ms(60_000 + 30_000)
        assert not feed.health("SPY").waiting
        assert presentation(feed, fixture.clock.now_ms()).state == "unhealthy"
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


def test_october_20_delivering_but_incomplete_bar_is_presented_as_warming_and_still_refuses() -> None:
    from app.marketdata.feed import FeedHealth

    fact = FeedHealth(
        connected=True,
        stale=True,
        warming=True,
        last_bar_ms=None,
        reason="The feed is delivering; its first complete minute is being assembled",
        active_subscription_count=1,
        observed_at_ms=1792503057393,
    )
    channel = market_data_channel_health(fact, now_ms=fact.observed_at_ms)
    assert not channel.healthy and channel.connected
    view = _channel_views(SimpleNamespace(channel_healths=[channel]), fact.observed_at_ms)[0]
    assert view.state == "warming" and "down" not in view.explanation.lower()
    assert channel_state(healthy=False, warming=True, observed_at_ms=0, now_ms=1792503057393) == "unknown"


@pytest.mark.parametrize("healthy,connected", [(True, True), (False, False)])
def test_warming_cannot_claim_readiness_or_cover_a_disconnection(healthy: bool, connected: bool) -> None:
    with pytest.raises(ValueError, match="warming"):
        ChannelHealth(stream="market_data", warming=True, healthy=healthy, connected=connected, observed_at_ms=1)


def test_deployment_guidance_waits_for_preparation_but_does_not_hide_stale_health() -> None:
    from app.services.broker_v2_panel.channel_health import evaluate_channel_health
    from app.services.broker_v2_panel.paper_deploy_service import _channel_recovery

    channels = [
        ChannelHealth(stream="market_data", healthy=False, connected=True, warming=True, observed_at_ms=1000),
        ChannelHealth(stream="execution", healthy=True, connected=True, observed_at_ms=1000),
    ]
    evaluation = evaluate_channel_health(channels, 1000)
    assert not evaluation.ready
    assert _channel_recovery(channels, evaluation).startswith("Wait")
    stale = evaluate_channel_health(channels, 100000)
    assert not _channel_recovery(channels, stale).startswith("Wait")


async def test_an_established_stalled_feed_stays_unhealthy_until_replacement_actually_delivers(
    monkeypatch: Any,
) -> None:
    from tests._helpers.ibkr_feed_adversarial import RTH_MINUTE, ScriptedLinesFeedFixture, raw_minute

    fixture = ScriptedLinesFeedFixture(raw_minute(RTH_MINUTE, tuple(range(0, 60, 5))), ())
    fixture.install(monkeypatch)
    feed = IbkrMarketDataFeed(fixture.client)
    stream = feed.stream_bars("SPY")
    pending = None
    try:
        await asyncio.wait_for(anext(stream), 1)
        assert not feed.health("SPY").stale
        pending = asyncio.create_task(anext(stream))
        fixture.clock.advance_ms(60_000)
        await fixture.wait_until_subscribed(2)
        health = feed.health("SPY")
        assert health.stale and not health.waiting and not health.warming
        assert presentation(feed, fixture.clock.now_ms()).state == "unhealthy"
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("session_date", ["2026-10-20", "2026-11-27"])
def test_extended_session_initial_wait_has_grace_at_the_calendar_open(session_date: str, monkeypatch: Any) -> None:
    from datetime import date

    from app.marketdata.ibkr_feed import _SymbolLiveness
    from app.services.session_authority import scheduled_extended_session_bounds

    bounds = scheduled_extended_session_bounds(date.fromisoformat(session_date))
    assert bounds is not None
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: bounds.open_ms - 60_000)
    state = _SymbolLiveness(active_count=1)
    state.await_first_bar(initial=True)
    assert [state.waiting(bounds.open_ms + offset, 30_000) for offset in [-1, 0, 1000, 29_999, 30_000]] == [
        True,
        True,
        True,
        True,
        False,
    ]
