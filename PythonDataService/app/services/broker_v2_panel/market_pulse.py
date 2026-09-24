"""Backend-owned market session and feed-recency copy for the V2 header."""

from __future__ import annotations

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.marketdata.feed import MarketDataFeed
from app.schemas.broker_capability import SessionDataCapability
from app.schemas.broker_v2_panel import ChartFeedView, MarketPulseView
from app.schemas.market_liveness import MarketLivenessFact
from app.services.bot_start_admission import market_data_admission_fact
from app.services.market_data_capability_service import extended_phase_proven_at_ms
from app.services.market_liveness import market_liveness_fact

# The admission feed emits closed one-minute bars. This is the contracted source
# cadence shown to operators, not the feed implementation's longer stale cutoff.
_EXPECTED_CADENCE_MS = 60_000


_LIVENESS_COPY = {
    "MARKET_DATA_STARTING": ("Preparing live market data", "Wait for the first live quote or trade; the service is preparing the subscription."),
    "MARKET_DATA_RECOVERING": ("Market data recovering", "The service is repairing the subscription. If recovery persists, check Gateway and the symbol's live-data entitlement."),
    "MARKET_DATA_DISCONNECTED": ("Market data disconnected", "Restore the read-only Gateway connection and verify live data delivery."),
    "STATUS_STREAM_DISCONNECTED": ("Market data disconnected", "Restore the read-only Gateway connection and verify live data delivery."),
    "MARKET_DATA_UNAVAILABLE": ("Market data unavailable", "Check Gateway subscription capacity, instrument availability and live-data permissions."),
    "MARKET_HALT_PERSISTENCE_FAILED": ("Market status storage unavailable", "Restore clerk storage; the service retries the halt write before admitting new exposure."),
    "MARKET_DATA_NOT_LIVE": ("Live market data required", "Restore live IBKR data and verify entitlements; delayed or frozen prices cannot authorize entry."),
    "MARKET_CLOCK_UNAVAILABLE": ("Broker clock unavailable", "Restore Alpaca account API connectivity so the broker clock can be read."),
    "MARKET_CLOCK_STALE": ("Broker clock stale", "Check Alpaca account API connectivity and the broker-clock polling service."),
    "MARKET_CLOCK_INVALID": ("Broker clock invalid", "Check system time synchronization and the broker-clock timestamps."),
    "MARKET_CLOCK_UNKNOWN": ("Broker clock unproven", "Check the Alpaca account API clock response before trading."),
    "SYMBOL_STATUS_UNKNOWN": ("Symbol trading status unavailable", "Check IBKR's trading-status report and entitlement for this instrument."),
}


def build_market_pulse(
    feed: MarketDataFeed | None,
    *,
    now_ms: int,
    symbol: str | None = None,
    account_id: str | None = None,
    capability: SessionDataCapability | None = None,
    use_rth: bool,
    bot_running: bool,
    liveness: MarketLivenessFact | None = None,
    extended_window: ExtendedHoursWindow | None = None,
) -> MarketPulseView:
    """Present the same typed market-data fact Start admission consumes,
    with scheduled session kept separate from the shared live liveness fact."""
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
        extended_window=extended_window,
    )
    session = fact.scheduled_phase
    bars_expected = session == "RTH" if use_rth else session in {"PRE", "RTH", "POST", "OVERNIGHT"}
    liveness = liveness or market_liveness_fact(symbol or "", now_ms)
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

    # Alpaca's live clock is RTH-only (see clock_liveness_evidence), so a
    # CLOSED liveness fact does not by itself distinguish a genuinely
    # closed market from an ordinary extended-hours session for a non-RTH
    # bot. This must resolve identically to liveness_blocks_entry's own
    # reconciliation — the same function, not a re-derived approximation —
    # or the panel contradicts the execution gate it is describing.
    # Both halves of that reconciliation, in the same order the gate applies
    # them: the schedule says PRE/POST, and the feed proves the venue is
    # actually printing for this symbol. A declared window alone would let
    # the panel report an unscheduled extended-hours closure as tradable,
    # which the RTH-only clock cannot distinguish from a live session.
    live_closed_is_actually_extended_hours = (
        liveness.state == "CLOSED"
        and not use_rth
        and symbol is not None
        and bool(fact.connected)
        and not fact.stale
        and extended_phase_proven_at_ms(
            now_ms=now_ms, symbol=symbol, account_id=account_id, extended_window=extended_window
        )
    )
    # The Market badge (market_state) renders right beside `headline` in the
    # V2 header (panel-header.component.html) — reporting the raw "CLOSED"
    # liveness value here while the headline below says "Market data live"
    # would show operators a self-contradictory panel during every proven
    # extended-hours session. TRADABLE is what the exemption actually
    # concluded, so the badge must say what the headline says.
    reconciled_market_state = "TRADABLE" if live_closed_is_actually_extended_hours else liveness.state

    # Live broker-reported liveness takes priority over the narrower
    # extended-session-capability check below: a HALTED/UNKNOWN liveness
    # fact is the stronger, more foundational safety signal (it governs
    # whether new exposure may be created at all, per #1671), regardless of
    # whether extended-hours capability happens to be proven. A CLOSED fact
    # that extended hours actually cover is exempted (above) and falls
    # through to the feed-state branches below instead.
    if liveness.market_data is not None and liveness.market_data.reason_code == "MARKET_HALT_PERSISTENCE_FAILED":
        headline, next_step = _LIVENESS_COPY["MARKET_HALT_PERSISTENCE_FAILED"]
        explanation = liveness.market_data.reason
        attention_required = True
    elif liveness.state == "HALTED":
        # The symbol is never interpolated into this prose — it renders
        # separately as structured data (``halted_symbol`` below) through
        # the canonical app-asset-identity component.
        headline = "Trading halted for"
        explanation = liveness.reason
        next_step = "Keep new exposure blocked until a fresh trading-status resume arrives."
        attention_required = True
    elif liveness.state == "UNKNOWN":
        headline, next_step = _LIVENESS_COPY.get(
            liveness.reason_code,
            ("Market liveness unproven", "Inspect the broker clock, Gateway connection and symbol-status evidence before trading."),
        )
        explanation = liveness.reason
        attention_required = True
    elif liveness.state == "CLOSED" and not live_closed_is_actually_extended_hours:
        headline = "Market closed by live broker evidence"
        explanation = liveness.reason
        next_step = (
            "Investigate the scheduled-session mismatch before relying on new exposure."
            if bars_expected
            else None
        )
        attention_required = bars_expected
    elif not use_rth and not fact.extended_phase_proven and session == "CLOSED":
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
        market_state=reconciled_market_state,
        market_liveness_reason=liveness.reason,
        market_liveness_observed_at_ms=liveness.observed_at_ms,
        halted_symbol=liveness.symbol if liveness.state == "HALTED" else None,
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


def qualify_pulse_for_chart_feed(pulse: MarketPulseView, chart_feed: ChartFeedView) -> MarketPulseView:
    """Stop a live headline from vouching for a chart line that is down (#2355).

    ``pulse`` describes the bot's own feed, the fact Start admission consumes;
    the LIVE chart runs a separate IBKR bar line. When the bot's feed is live
    and nothing else needs attention, the headline would otherwise say "Market
    data live" beside a frozen chart. The typed ``feed_state`` stays the bot
    feed's (admission reads the same fact); only the operator copy and the
    attention flag change. A pulse already requiring attention keeps its own,
    stronger headline, and the chart's notice still renders on the chart.
    """
    if not chart_feed.attention_required:
        return pulse
    if pulse.attention_required or pulse.feed_state != "LIVE":
        return pulse
    return pulse.model_copy(
        update={
            "headline": "Bot market data live; chart feed not live",
            "explanation": (
                "The bot's feed is delivering bars within its expected cadence, "
                "but the chart draws from its own IBKR bar line, which is not. "
                f"{chart_feed.explanation}"
            ),
            "next_step": chart_feed.next_step,
            "attention_required": True,
        }
    )
