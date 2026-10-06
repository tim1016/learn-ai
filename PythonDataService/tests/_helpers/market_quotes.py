"""Fresh IBKR quotes for tests, published to the real process store as the status source does."""

from __future__ import annotations

from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource, TopOfBookQuote
from app.services import market_liveness


def publish_quotes(at_ms: int, **asks: float) -> None:
    """Publish one fresh IBKR book per symbol, bid and ask both at the given price."""
    market_liveness.get_market_liveness_store().apply_status_snapshot(MarketStatusSnapshot(
        source=MarketStatusSource.IBKR, connected=True, observed_at_ms=at_ms, connection_changed_at_ms=at_ms,
        symbol_statuses=(), quotes=tuple(
            TopOfBookQuote(symbol=symbol, bid=ask, ask=ask, source="ibkr.market_data.status", observed_at_ms=at_ms)
            for symbol, ask in asks.items()
        ),
    ), now_ms=at_ms)
