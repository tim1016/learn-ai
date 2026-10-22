"""The single ib_async-to-receipt conversion for local and remote readers."""

from __future__ import annotations

import math
from typing import Any

from app.broker.ibkr.live.protocol import GREEK_BLOCKS, QuoteUpdate, Tick
from app.utils.timestamps import now_ms_utc

QUOTE_FIELDS = (
    "bid",
    "ask",
    "last",
    "close",
    "bidSize",
    "askSize",
    "lastSize",
    "halted",
    "callOpenInterest",
    "putOpenInterest",
    "callVolume",
    "putVolume",
)
GREEK_FIELDS = ("impliedVol", "delta", "gamma", "theta", "vega", "undPrice", "optPrice", "pvDividend")
REQUEST_FAILURE_CODES = frozenset({101, 200, 354, 10089, 10090, 10167, 10186})


def number(value: Any) -> float | None:
    return float(value) if value is not None and math.isfinite(value) else None


def generic_ticks(contract: Any) -> str:
    return "100,101,106" if contract.secType == "OPT" else "233"


def reset_greeks(ticker: Any) -> None:
    # ib_async retains tickers by conId after cancellation. A new source must
    # not give retained analytics a current receipt (#3388).
    for block in GREEK_BLOCKS:
        setattr(ticker, block, None)


def quote_update(ticker: Any, previous_greeks: dict[str, Any]) -> QuoteUpdate:
    changed = tuple(block for block in GREEK_BLOCKS if getattr(ticker, block, None) is not previous_greeks.get(block))
    previous_greeks.update({block: getattr(ticker, block, None) for block in GREEK_BLOCKS})
    trade = getattr(ticker, "rtTime", None)
    market_price = ticker.marketPrice()
    return QuoteUpdate(
        received_at_ms=now_ms_utc(),
        data_type=ticker.marketDataType,
        ticks=tuple(Tick(kind=t.tickType, price=number(t.price), size=number(t.size)) for t in ticker.ticks),
        values={key: number(getattr(ticker, key, None)) for key in QUOTE_FIELDS},
        greeks={
            block: {key: number(getattr(greeks, key, None)) for key in GREEK_FIELDS}
            for block in GREEK_BLOCKS
            if (greeks := getattr(ticker, block, None)) is not None
        },
        greeks_changed=changed,
        trade_ms=None if trade is None else int(trade.timestamp() * 1000),
        market_price=number(market_price),
    )


def install_market_data_callbacks(wrapper: Any) -> None:
    """Keep tick 49's -1/0 distinction at the ib_async ingestion boundary.

    ib_async 2.x's generic-size normalization maps nonpositive values to its
    empty size (zero). For halt status that turns unavailable (-1) into a
    resume (0). All other tick types keep the library decoder unchanged.
    Installed idempotently by the status-subscription owner; history-only
    clients keep their decoder unchanged.
    """
    from ib_async.objects import TickData

    if getattr(wrapper, "_market_status_callbacks_installed", False) is True:
        return
    wrapper._market_status_callbacks_installed = True
    generic = wrapper.tickGeneric
    data_type = wrapper.marketDataType

    def tick_generic(req_id: int, tick_type: int, value: float) -> None:
        if tick_type != 49:
            generic(req_id, tick_type, value)
            return
        ticker = wrapper.reqId2Ticker.get(req_id)
        if ticker is not None:
            ticker.halted = float(value)
            ticker.ticks.append(TickData(wrapper.lastTime, tick_type, ticker.halted, 0))
            wrapper.pendingTickers.add(ticker)

    wrapper.tickGeneric = tick_generic

    def market_data_type(req_id: int, kind: int) -> None:
        data_type(req_id, kind)
        ticker = wrapper.reqId2Ticker.get(req_id)
        if ticker is not None:
            # The vendor decoder changes the cache without emitting an update.
            # Readiness must see a delayed/frozen transition on this callback.
            wrapper.pendingTickers.add(ticker)

    wrapper.marketDataType = market_data_type
