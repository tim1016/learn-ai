"""Dual-health submission gate composition (S4, #1262 — P5/P6/P7).

Composes the two channel-health facts the clerk's submit gate consumes:

- **market_data** — the shared :class:`MarketDataFeed`'s ``FeedHealth``
  (S1). Unhealthy when the feed is not installed, disconnected, or stale
  past its bounded threshold.
- **execution** — the Alpaca ``trade_updates`` websocket connection plus the
  consumer's latest evidence-bearing frame outcome. Unhealthy when the consumer
  is not running, the socket is down, or a received frame was unusable.

Every fact carries its own ``observed_at_ms`` (P7: truth has age). The gate
itself is dumb composition — the *hold* raised on an unhealthy channel lives
at the clerk boundary and reuses the existing journal-derived hold semantics
unchanged (blocks new submissions, never reductions/cancels, restart-durable,
clearable — never auto-cleared).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.broker.alpaca.clerk.models import ChannelHealth
from app.marketdata.feed import FeedHealth

ChannelHealthProvider = Callable[[], ChannelHealth]
SymbolChannelHealthProvider = Callable[[str], ChannelHealth]


@dataclass(frozen=True)
class ExecutionEvidenceHealth:
    """Latest evidence-bearing frame outcome and its int64-ms observation."""

    healthy: bool
    observed_at_ms: int


def market_data_channel_health(
    feed_health: FeedHealth | None, *, now_ms: int
) -> ChannelHealth:
    """Fold the shared feed's health snapshot into the gate's fact shape."""
    if feed_health is None:
        return ChannelHealth(
            stream="market_data",
            healthy=False,
            reason="Shared market-data feed is not installed.",
            observed_at_ms=now_ms,
        )
    healthy = feed_health.connected and not feed_health.stale
    return ChannelHealth(
        stream="market_data",
        healthy=healthy,
        reason="" if healthy else (feed_health.reason or "Market-data feed is unhealthy."),
        observed_at_ms=feed_health.observed_at_ms,
    )


def execution_channel_health(
    *,
    connected: bool | None,
    connection_changed_at_ms: int | None,
    evidence_health: ExecutionEvidenceHealth | None,
    now_ms: int,
) -> ChannelHealth:
    """Fold trade_updates connectivity and evidence health into one gate fact.

    ``connected=None`` means the consumer is not running at all — fail-safe
    unhealthy, observed now.
    """
    if connected is None:
        return ChannelHealth(
            stream="execution",
            healthy=False,
            reason="Alpaca trade_updates consumer is not running.",
            observed_at_ms=now_ms,
        )
    if not connected:
        return ChannelHealth(
            stream="execution",
            healthy=False,
            reason="Alpaca trade_updates websocket is disconnected.",
            observed_at_ms=(
                connection_changed_at_ms
                if connection_changed_at_ms is not None
                else now_ms
            ),
        )
    if evidence_health is None or not evidence_health.healthy:
        return ChannelHealth(
            stream="execution",
            healthy=False,
            reason="Alpaca trade_updates received an unusable evidence frame.",
            observed_at_ms=(
                evidence_health.observed_at_ms
                if evidence_health is not None
                else now_ms
            ),
        )
    return ChannelHealth(
        stream="execution",
        healthy=True,
        reason="",
        observed_at_ms=max(
            observed_at_ms
            for observed_at_ms in (
                connection_changed_at_ms,
                evidence_health.observed_at_ms,
                now_ms,
            )
            if observed_at_ms is not None
        ),
    )


@dataclass(frozen=True)
class StreamHealthGate:
    """The two channel-health providers the clerk's submit gate snapshots."""

    market_data: ChannelHealthProvider
    execution: ChannelHealthProvider
    market_data_for_symbol: SymbolChannelHealthProvider | None = None

    def snapshot(self, symbol: str | None = None) -> tuple[ChannelHealth, ChannelHealth]:
        market_data = (
            self.market_data_for_symbol(symbol)
            if symbol is not None and self.market_data_for_symbol is not None
            else self.market_data()
        )
        return (market_data, self.execution())

    def broken(self, symbol: str | None = None) -> tuple[ChannelHealth, ...]:
        return tuple(
            health for health in self.snapshot(symbol) if not health.healthy
        )


def stream_health_refusal(
    gate: StreamHealthGate | None,
    *,
    symbol: str | None = None,
) -> tuple[str, str] | None:
    """``(reason, detail)`` for a submit refusal, or ``None`` when clear.

    ``reason`` names the broken stream(s) for the hold line; ``detail`` adds
    each stream's why and observation time. A ``None`` gate (not installed)
    refuses nothing — production wiring always installs it.
    """
    if gate is None:
        return None
    broken = gate.broken(symbol)
    if not broken:
        return None
    names = ", ".join(health.stream for health in broken)
    detail = "; ".join(
        f"{health.stream}: {health.reason} (observed_at_ms={health.observed_at_ms})"
        for health in broken
    )
    return (f"Order submission is paused: unhealthy stream(s): {names}.", detail)


def build_default_stream_health_gate() -> StreamHealthGate:
    """Production wiring: shared MarketDataFeed + trade_updates consumer.

    Providers resolve their singletons lazily per call, so the gate can be
    constructed before either exists (the consumer is created after the
    clerk). Imports are deferred inside the providers to avoid the
    ``trade_updates`` → ``clerk`` import cycle.
    """
    from app.utils.timestamps import now_ms_utc

    def _market_data_scoped(symbol: str | None) -> ChannelHealth:
        from app.marketdata.ibkr_feed import get_market_data_feed

        feed = get_market_data_feed()
        return market_data_channel_health(
            feed.health(symbol) if feed is not None else None,
            now_ms=now_ms_utc(),
        )

    def _market_data() -> ChannelHealth:
        return _market_data_scoped(None)

    def _market_data_for_symbol(symbol: str) -> ChannelHealth:
        return _market_data_scoped(symbol)

    def _execution() -> ChannelHealth:
        from app.broker.alpaca.trade_updates import get_trade_updates_consumer

        consumer = get_trade_updates_consumer()
        if consumer is None:
            return execution_channel_health(
                connected=None,
                connection_changed_at_ms=None,
                evidence_health=None,
                now_ms=now_ms_utc(),
            )
        return execution_channel_health(
            connected=consumer.connected,
            connection_changed_at_ms=consumer.connection_changed_at_ms,
            evidence_health=consumer.evidence_health,
            now_ms=now_ms_utc(),
        )

    return StreamHealthGate(
        market_data=_market_data,
        execution=_execution,
        market_data_for_symbol=_market_data_for_symbol,
    )
