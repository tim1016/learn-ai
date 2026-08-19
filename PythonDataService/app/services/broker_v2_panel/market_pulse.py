"""Backend-owned market session and feed-recency copy for the V2 header."""

from __future__ import annotations

from app.marketdata.feed import MarketDataFeed
from app.schemas.broker_capability import SessionDataCapability
from app.schemas.broker_v2_panel import MarketPulseView
from app.services.bot_start_admission import market_data_admission_fact

# The admission feed emits closed one-minute bars. This is the contracted source
# cadence shown to operators, not the feed implementation's longer stale cutoff.
_EXPECTED_CADENCE_MS = 60_000


def build_market_pulse(
    feed: MarketDataFeed | None,
    *,
    now_ms: int,
    symbol: str | None = None,
    account_id: str | None = None,
    capability: SessionDataCapability | None = None,
    use_rth: bool,
    bot_running: bool,
) -> MarketPulseView:
    """Present the same typed market-data fact Start admission consumes."""
    session_label = {
        "PRE": "PRE_MARKET", "RTH": "OPEN", "POST": "AFTER_HOURS",
        "OVERNIGHT": "AFTER_HOURS", "CLOSED": "CLOSED", "UNKNOWN": "UNKNOWN",
    }
    fact = market_data_admission_fact(
        feed,
        now_ms,
        symbol=symbol,
        use_rth=use_rth,
        capability=capability,
        account_id=account_id,
    )
    session = fact.scheduled_phase
    bars_expected = session == "RTH" if use_rth else session in {"PRE", "RTH", "POST", "OVERNIGHT"}
    feed_state = {
        "AVAILABLE": "IDLE" if fact.stale else "LIVE",
        "STALE": "STALE",
        "UNAVAILABLE": "MISSING",
        "UNKNOWN": "MISSING",
    }[fact.state]
    age_ms = (
        max(0, fact.observed_at_ms - fact.last_bar_ms)
        if fact.last_bar_ms is not None
        else None
    )

    if not use_rth and not fact.extended_phase_proven and session == "CLOSED":
        headline = "Extended-session phase unproved"
        explanation = (
            "No fresh capability matches this account and instrument, so the "
            "canonical NYSE calendar can prove only regular hours or closed."
        )
        next_step = "Refresh the instrument's account-scoped session capability before relying on extended hours."
        attention_required = bot_running
    elif not bars_expected:
        headline = "Market closed — no live bar expected"
        explanation = (
            "The session calendar does not expect a new regular-session bar now."
            if feed_state == "LIVE"
            else "No regular-session bar is expected now; feed availability remains visible for diagnosis."
        )
        next_step = None
        attention_required = False
    elif feed_state == "LIVE":
        headline = "Market data live"
        explanation = "The feed is connected and delivering data within its expected cadence."
        next_step = None
        attention_required = False
    elif feed_state == "IDLE":
        headline = (
            "Market data idle for a running bot"
            if bot_running
            else "Market data ready — waiting for the run subscription"
        )
        explanation = fact.reason or "The feed is connected but has no active bar subscription."
        next_step = (
            "Inspect the bot's market-data subscription before relying on new decisions."
            if bot_running
            else None
        )
        attention_required = bot_running
    elif feed_state == "STALE":
        headline = "Market data stale"
        explanation = fact.reason or "No current bar arrived within the expected cadence."
        next_step = "Check the market-data connection before starting or resuming a run."
        attention_required = True
    else:
        headline = "Market data missing"
        explanation = fact.reason or "The backend cannot prove that market data is available."
        next_step = "Restore the market-data connection before starting or resuming a run."
        attention_required = True

    return MarketPulseView(
        session=session_label[session],
        feed_state=feed_state,
        latest_bar_at_ms=fact.last_bar_ms,
        age_ms=age_ms,
        source=fact.feed_id,
        expected_cadence_ms=_EXPECTED_CADENCE_MS,
        headline=headline,
        explanation=explanation,
        next_step=next_step,
        attention_required=attention_required,
        observed_at_ms=fact.observed_at_ms,
    )
