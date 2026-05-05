"""Adapter from IBKR wire bars to engine TradeBar.

The IBKR broker boundary stores timestamps as ``int64`` ms UTC (see
``app.broker.ibkr.bars`` and the timestamp policy in
``.claude/rules/numerical-rigor.md``). The engine consumes
``TradeBar`` with timezone-aware datetimes so wall-clock session rules
(``force_flat_at``) can be expressed directly. This module is the only
place that converts between the two; everything downstream of
``LiveEngine`` consumes ``TradeBar``.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from zoneinfo import ZoneInfo

from app.broker.ibkr.models import IbkrMinuteBar
from app.engine.data.trade_bar import TradeBar

_ENGINE_TZ = ZoneInfo("America/New_York")


def ibkr_minute_bar_to_trade_bar(bar: IbkrMinuteBar) -> TradeBar:
    """Convert one ``IbkrMinuteBar`` to an engine ``TradeBar``.

    ``IbkrMinuteBar.start_ms`` / ``end_ms`` are the canonical wire
    timestamps. ``TradeBar.time`` / ``end_time`` are tz-aware in the
    exchange timezone — strategies that gate on session wall-clock
    times (``bar.time.time() >= force_flat_at``) need that to work
    without re-converting per access.
    """
    start = datetime.fromtimestamp(bar.start_ms / 1000, tz=_ENGINE_TZ)
    end = datetime.fromtimestamp(bar.end_ms / 1000, tz=_ENGINE_TZ)
    return TradeBar(
        symbol=bar.symbol,
        time=start,
        end_time=end,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )


async def trade_bars_from_ibkr(
    source: AsyncIterable[IbkrMinuteBar],
) -> AsyncIterator[TradeBar]:
    """Yield ``TradeBar`` values converted from an ``IbkrMinuteBar`` stream."""
    async for bar in source:
        yield ibkr_minute_bar_to_trade_bar(bar)
