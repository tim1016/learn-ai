"""Callback-owned evidence for one IBKR subscription generation.

Watchlist prices are change-driven, not heartbeats. A recent two-sided quote
or RTVolume trade proves decision data; source progress and halt events are
recorded separately. Polling never renews any of these receipts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.broker.ibkr.live.protocol import QuoteUpdate
from app.broker.ibkr.live.quotes import QuoteLease
from app.broker.ibkr.models import _coerce_quote, _coerce_size
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
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
MAX_RETRY_MS = 30_000


def retry_delay_ms(attempt: int) -> int:
    """How long a failed request waits before it is tried again: 1, 2, 4, 8, 16, then at most 30 seconds."""
    return min(1_000 * 2 ** min(attempt, 5), MAX_RETRY_MS)




@dataclass
class MarketSubscription:
    """Mutable ingestion state; only the subscription owner writes it."""

    symbol: str
    started_at_ms: int
    generation: str = field(default_factory=lambda: uuid4().hex)
    update: QuoteUpdate | None = None
    callback: Any = None
    failure_callback: Any = None
    lease: QuoteLease | None = None
    last_received_at_ms: int | None = None
    bid_received_at_ms: int | None = None
    ask_received_at_ms: int | None = None
    # Whether a tick of the side's price, not only of its size, has arrived in this subscription: until
    # then the price the library holds for it may be from before the subscription began.
    bid_priced: bool = False
    ask_priced: bool = False
    trade_timestamp_ms: int | None = None
    trade_received_at_ms: int | None = None
    status: SymbolTradingStatusEvidence | None = None
    failure: str | None = None
    market_data_type: int | None = None

    def ingest(self, update: QuoteUpdate) -> tuple[SymbolTradingStatusEvidence, ...]:
        """Accept actual callbacks, preserving every halt/resume in a batch."""
        self.update = update
        now = update.received_at_ms
        self.failure = None
        self.last_received_at_ms = now
        if self.market_data_type != update.data_type:
            self.bid_received_at_ms = self.ask_received_at_ms = None
            self.bid_priced = self.ask_priced = False
            self.trade_timestamp_ms = self.trade_received_at_ms = None
            self.market_data_type = update.data_type
        trade_ms = update.trade_ms
        transitions: list[SymbolTradingStatusEvidence] = []
        for tick in update.ticks:
            # Negative status evidence remains load-bearing even if the
            # subscription switches mode in the same network packet. Only
            # a live clear can release a retained halt.
            if update.data_type != 1 and (tick.kind != 49 or tick.price == 0):
                continue
            if tick.kind in (0, 1):
                self.bid_received_at_ms = now
                self.bid_priced = self.bid_priced or tick.kind == 1
            elif tick.kind in (2, 3):
                self.ask_received_at_ms = now
                self.ask_priced = self.ask_priced or tick.kind == 2
            elif tick.kind in (48, 77) and trade_ms is not None:
                if self.trade_timestamp_ms is None or trade_ms > self.trade_timestamp_ms:
                    self.trade_timestamp_ms = trade_ms
                    self.trade_received_at_ms = now
            elif tick.kind == 49:
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
        if self.update is None or self.update.data_type != 1 or self.failure:
            return None
        if self.bid_received_at_ms is None or self.ask_received_at_ms is None:
            return None
        received = min(self.bid_received_at_ms, self.ask_received_at_ms)
        bid, ask = _coerce_quote(self.update.values.get("bid")), _coerce_quote(self.update.values.get("ask"))
        if not bid or not ask or bid > ask or not 0 <= now - received <= DECISION_DATA_MAX_AGE_MS:
            return None
        return TopOfBookQuote(
            symbol=self.symbol, bid=bid, ask=ask,
            bid_size=_coerce_size(self.update.values.get("bidSize")), ask_size=_coerce_size(self.update.values.get("askSize")),
            source=SOURCE, observed_at_ms=received,
        )

    def held(self, generation: int) -> HeldQuote | None:
        """The book as it was received, each side with its own receipt time, untested: for a reader that
        asks the usability tests itself (ADR 0076). None until a live price tick has arrived for each side
        in this subscription: a tick of a side's size alone would hand over a price from before it."""
        if self.update is None or self.failure or self.update.data_type != 1:
            return None
        if not (self.bid_priced and self.ask_priced):
            return None
        if self.bid_received_at_ms is None or self.ask_received_at_ms is None:
            return None
        bid, ask = _coerce_quote(self.update.values.get("bid")), _coerce_quote(self.update.values.get("ask"))
        if bid is None or ask is None:
            return None
        return HeldQuote(
            bid=QuoteSide(Decimal(str(bid)), self.bid_received_at_ms),
            ask=QuoteSide(Decimal(str(ask)), self.ask_received_at_ms),
            data_type=QuoteDataType.LIVE,
            generation=generation,
        )

    def evidence(self, now: int, *, recovering: bool = False) -> SymbolMarketDataEvidence:
        """Evaluate readiness without modifying source state."""
        quote = self.quote(now)
        trade = self.trade_timestamp_ms
        trade_valid = (
            self.trade_received_at_ms is not None and trade is not None
            and self.update is not None and self.update.data_type == 1
            and 0 <= now - trade <= DECISION_DATA_MAX_AGE_MS
            and 0 <= now - self.trade_received_at_ms <= DECISION_DATA_MAX_AGE_MS
        )
        proof_times = ([quote.observed_at_ms] if quote is not None else []) + (
            [min(trade, self.trade_received_at_ms)] if trade_valid else []
        )
        if self.failure:
            state, code, reason = "UNAVAILABLE", "MARKET_DATA_UNAVAILABLE", self.failure
        elif self.update is not None and self.update.data_type != 1:
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
