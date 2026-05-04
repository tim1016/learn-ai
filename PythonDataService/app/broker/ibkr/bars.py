"""Real-time underlying minute bars from IBKR.

IBKR's ``reqRealTimeBars`` emits 5-second TRADES bars. This module
aggregates those into closed 1-minute bars for the live engine, enforcing
the repo's timestamp policy at the ingestion boundary: every yielded model
uses ``int64`` ms UTC and duplicate/non-monotonic source timestamps fail
fast.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.broker.ibkr.client import IbkrClient
from app.broker.ibkr.contracts import qualify_underlying
from app.broker.ibkr.models import IbkrMinuteBar

logger = logging.getLogger(__name__)


class IBKRBarStreamError(Exception):
    """Raised when IBKR real-time bars violate timestamp invariants."""


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _to_utc_ms(value: datetime | int | float) -> int:
    """Convert an IBKR bar timestamp to canonical int64 ms UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise IBKRBarStreamError("IBKR bar timestamp is naive; expected tz-aware UTC datetime.")
        return int(value.astimezone(UTC).timestamp() * 1000)
    numeric = float(value)
    # ib_async/IB API bars commonly expose epoch seconds. Accept ms too for
    # tests/future wrappers by checking magnitude.
    if numeric > 10_000_000_000:
        return int(numeric)
    return int(numeric * 1000)


def _minute_start_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % 60_000)


@dataclass
class _MinuteAccumulator:
    symbol: str
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def update(self, high: Decimal, low: Decimal, close: Decimal, volume: int) -> None:
        self.high = max(self.high, high)
        self.low = min(self.low, low)
        self.close = close
        self.volume += volume

    def to_model(self) -> IbkrMinuteBar:
        return IbkrMinuteBar(
            symbol=self.symbol,
            start_ms=self.start_ms,
            end_ms=self.start_ms + 60_000,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            fetched_at_ms=_now_ms(),
        )


def _decimal_attr(obj, name: str) -> Decimal:
    return Decimal(str(getattr(obj, name)))


def _volume_attr(obj) -> int:
    return int(getattr(obj, "volume", getattr(obj, "barCount", 0)) or 0)


def _bar_time_ms(obj) -> int:
    value = getattr(obj, "time", getattr(obj, "date", None))
    if value is None:
        raise IBKRBarStreamError("IBKR 5-second bar is missing a time/date field.")
    return _to_utc_ms(value)


def aggregate_realtime_bar(
    current: _MinuteAccumulator | None,
    bar,
    *,
    symbol: str,
    last_source_ms: int | None,
) -> tuple[_MinuteAccumulator, IbkrMinuteBar | None, int]:
    """Fold one IBKR 5-second bar into a minute accumulator."""
    source_ms = _bar_time_ms(bar)
    if last_source_ms is not None:
        if source_ms == last_source_ms:
            raise IBKRBarStreamError(f"Duplicate IBKR 5-second bar timestamp: {source_ms}.")
        if source_ms < last_source_ms:
            raise IBKRBarStreamError(
                f"Non-monotonic IBKR 5-second bar timestamp: {source_ms} after {last_source_ms}."
            )

    start_ms = _minute_start_ms(source_ms)
    open_price = _decimal_attr(bar, "open")
    high = _decimal_attr(bar, "high")
    low = _decimal_attr(bar, "low")
    close = _decimal_attr(bar, "close")
    volume = _volume_attr(bar)

    if current is None:
        return (
            _MinuteAccumulator(
                symbol=symbol,
                start_ms=start_ms,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            ),
            None,
            source_ms,
        )

    if start_ms == current.start_ms:
        current.update(high, low, close, volume)
        return current, None, source_ms

    if start_ms < current.start_ms:
        raise IBKRBarStreamError(f"IBKR bar minute regressed from {current.start_ms} to {start_ms}.")

    emitted = current.to_model()
    return (
        _MinuteAccumulator(
            symbol=symbol,
            start_ms=start_ms,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
        ),
        emitted,
        source_ms,
    )


async def stream_minute_bars(
    client: IbkrClient,
    symbol: str,
    *,
    use_rth: bool = True,
) -> AsyncIterator[IbkrMinuteBar]:
    """Yield closed 1-minute bars built from IBKR 5-second TRADES bars."""
    client.require_connected()
    contract = await qualify_underlying(client, symbol)
    bars = client.ib.reqRealTimeBars(contract, 5, "TRADES", useRTH=use_rth)
    index = 0
    current: _MinuteAccumulator | None = None
    last_source_ms: int | None = None
    try:
        while True:
            if index >= len(bars):
                await asyncio.sleep(0.1)
                continue
            raw_bar = bars[index]
            index += 1
            current, emitted, last_source_ms = aggregate_realtime_bar(
                current,
                raw_bar,
                symbol=symbol.upper(),
                last_source_ms=last_source_ms,
            )
            if emitted is not None:
                yield emitted
    finally:
        client.ib.cancelRealTimeBars(bars)
        logger.debug("Cancelled reqRealTimeBars for %s", symbol)
