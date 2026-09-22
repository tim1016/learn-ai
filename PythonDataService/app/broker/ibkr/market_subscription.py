"""Callback-owned evidence for one IBKR subscription generation.

Watchlist prices are change-driven, not heartbeats. A recent two-sided quote
or RTVolume trade proves decision data; source progress and halt events are
recorded separately. Polling never renews any of these receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.broker.ibkr.models import _coerce_quote, _coerce_size
from app.schemas.market_liveness import (
    MarketStatusSource,
    SymbolMarketDataEvidence,
    SymbolTradingStatusEvidence,
    TopOfBookQuote,
)

SOURCE = MarketStatusSource.IBKR
# Decision-data age is an admission policy, not an assumed vendor cadence.
DECISION_DATA_MAX_AGE_MS = 5_000
# Establishment/stall recovery has its own budget. A stale decision input
# already blocks ENTER; waiting for repair never grants additional trading time.
SUBSCRIPTION_TIMEOUT_MS = 30_000


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


@dataclass
class MarketSubscription:
    """Mutable ingestion state; only the subscription owner writes it."""

    symbol: str
    started_at_ms: int
    generation: str = field(default_factory=lambda: uuid4().hex)
    ticker: Any = None
    callback: Any = None
    request_id: int | None = None
    last_received_at_ms: int | None = None
    bid_received_at_ms: int | None = None
    ask_received_at_ms: int | None = None
    trade_timestamp_ms: int | None = None
    trade_received_at_ms: int | None = None
    status: SymbolTradingStatusEvidence | None = None
    failure: str | None = None
    market_data_type: int | None = None

    def ingest(self, ticker: Any, now: int) -> tuple[SymbolTradingStatusEvidence, ...]:
        """Accept actual callbacks, preserving every halt/resume in a batch."""
        self.last_received_at_ms = now
        if self.market_data_type != ticker.marketDataType:
            self.bid_received_at_ms = self.ask_received_at_ms = None
            self.trade_timestamp_ms = self.trade_received_at_ms = None
            self.market_data_type = ticker.marketDataType
        transitions: list[SymbolTradingStatusEvidence] = []
        for tick in ticker.ticks:
            # Negative status evidence remains load-bearing even if the
            # subscription switches mode in the same network packet. Only
            # a live clear can release a retained halt.
            if ticker.marketDataType != 1 and (tick.tickType != 49 or tick.price == 0):
                continue
            if tick.tickType in (0, 1):
                self.bid_received_at_ms = now
            elif tick.tickType in (2, 3):
                self.ask_received_at_ms = now
            elif tick.tickType in (48, 77) and ticker.rtTime is not None:
                trade_ms = int(ticker.rtTime.timestamp() * 1000)
                if self.trade_timestamp_ms is None or trade_ms > self.trade_timestamp_ms:
                    self.trade_timestamp_ms = trade_ms
                    self.trade_received_at_ms = now
            elif tick.tickType == 49:
                state = "HALTED" if tick.price in (1, 2) else "TRADABLE" if tick.price == 0 else "UNKNOWN"
                self.status = SymbolTradingStatusEvidence(
                    symbol=self.symbol, state=state, source=SOURCE, observed_at_ms=now,
                    reason_code={"HALTED": "IBKR_SYMBOL_HALTED", "TRADABLE": "IBKR_SYMBOL_CLEAR", "UNKNOWN": "IBKR_STATUS_UNAVAILABLE"}[state],
                    reason={"HALTED": "IBKR reports this symbol halted.", "TRADABLE": "IBKR reports this symbol not halted.", "UNKNOWN": "IBKR cannot report this symbol's trading status."}[state],
                )
                transitions.append(self.status)
        return tuple(transitions)

    def quote(self, now: int) -> TopOfBookQuote | None:
        """Return a usable book with its original two-sided receipt age."""
        if self.ticker is None or self.ticker.marketDataType != 1 or self.failure:
            return None
        if self.bid_received_at_ms is None or self.ask_received_at_ms is None:
            return None
        received = min(self.bid_received_at_ms, self.ask_received_at_ms)
        bid, ask = _coerce_quote(self.ticker.bid), _coerce_quote(self.ticker.ask)
        if not bid or not ask or bid > ask or not 0 <= now - received <= DECISION_DATA_MAX_AGE_MS:
            return None
        return TopOfBookQuote(
            symbol=self.symbol, bid=bid, ask=ask,
            bid_size=_coerce_size(self.ticker.bidSize), ask_size=_coerce_size(self.ticker.askSize),
            source=SOURCE, observed_at_ms=received,
        )

    def evidence(self, now: int, *, recovering: bool = False) -> SymbolMarketDataEvidence:
        """Evaluate readiness without modifying source state."""
        quote = self.quote(now)
        trade = self.trade_timestamp_ms
        trade_valid = (
            self.trade_received_at_ms is not None and trade is not None
            and self.ticker is not None and self.ticker.marketDataType == 1
            and 0 <= now - trade <= DECISION_DATA_MAX_AGE_MS
            and 0 <= now - self.trade_received_at_ms <= DECISION_DATA_MAX_AGE_MS
        )
        proof_times = ([quote.observed_at_ms] if quote is not None else []) + (
            [min(trade, self.trade_received_at_ms)] if trade_valid else []
        )
        if self.failure:
            state, code, reason = "UNAVAILABLE", "MARKET_DATA_UNAVAILABLE", self.failure
        elif self.ticker is not None and self.ticker.marketDataType != 1:
            state, code, reason = "UNAVAILABLE", "MARKET_DATA_NOT_LIVE", "IBKR is delivering delayed or frozen market data."
        elif proof_times:
            state, code, reason = "READY", "MARKET_DATA_READY", "Current IBKR market data is ready for trading."
        elif self.last_received_at_ms is None and not recovering:
            state, code, reason = "STARTING", "MARKET_DATA_STARTING", "Preparing live market data."
        else:
            state, code, reason = "RECOVERING", "MARKET_DATA_RECOVERING", "Live market data is interrupted or too old; waiting for current evidence."
        return SymbolMarketDataEvidence(
            symbol=self.symbol, generation=self.generation, state=state, observed_at_ms=now,
            valid_until_ms=max(proof_times) + DECISION_DATA_MAX_AGE_MS if proof_times and state == "READY" else None,
            last_received_at_ms=self.last_received_at_ms,
            quote_received_at_ms=quote.observed_at_ms if quote is not None else None,
            trade_timestamp_ms=trade, reason_code=code, reason=reason,
        )

    def reported_status(self, now: int) -> SymbolTradingStatusEvidence:
        """Absence of an initial halt tick remains absence, never a clear tick."""
        return self.status or SymbolTradingStatusEvidence(
            symbol=self.symbol, state="NOT_REPORTED", source=SOURCE, observed_at_ms=now,
            reason_code="IBKR_STATUS_NOT_REPORTED", reason="IBKR has not reported an initial halt state.",
        )
