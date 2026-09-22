"""Typed real-time market-liveness evidence for operational decisions.

Scheduled session structure remains the canonical calendar's responsibility.
These models answer the separate, present-tense question of whether a symbol can
be traded now, using only live vendor evidence. Every timestamp is ``int64 ms
UTC`` at the model boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.session_anchors import MAX_TIMESTAMP_MS

MarketLivenessState = Literal["TRADABLE", "HALTED", "CLOSED", "UNKNOWN"]
MarketClockState = Literal["OPEN", "CLOSED", "UNKNOWN"]
SymbolTradingState = Literal["TRADABLE", "HALTED", "UNKNOWN"]


class SymbolMarketDataEvidence(BaseModel):
    """Subscription readiness, separate from reported halt state and prices.

    Publication does not extend ``valid_until_ms``. Only a live quote or
    vendor-timestamped trade received on this generation can do that.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    generation: str = Field(min_length=1)
    state: Literal["STARTING", "READY", "RECOVERING", "UNAVAILABLE"]
    observed_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    valid_until_ms: int | None = Field(default=None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    last_received_at_ms: int | None = Field(default=None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    quote_received_at_ms: int | None = Field(default=None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    trade_timestamp_ms: int | None = Field(default=None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    reason_code: str
    reason: str

    @model_validator(mode="after")
    def validate_readiness(self) -> SymbolMarketDataEvidence:
        if self.state == "READY":
            if self.valid_until_ms is None or self.valid_until_ms < self.observed_at_ms:
                raise ValueError("Ready market data requires an unexpired evidence deadline.")
        elif self.valid_until_ms is not None:
            raise ValueError("Unavailable market data cannot carry a trading deadline.")
        return self


class MarketClockLivenessEvidence(BaseModel):
    """One market-wide, broker-originated live-clock observation.

    This is deliberately not scheduled-session evidence: an ``OPEN`` result
    can only say that the broker currently considers the market open. It says
    nothing about whether an individual symbol is halted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: MarketClockState
    source: str
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    vendor_timestamp_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    reason: str | None = None


class SymbolTradingStatusEvidence(BaseModel):
    """One symbol-scoped live trading-status observation.

    A status stream sends state transitions rather than scheduled-calendar
    windows. Its connection epoch is recorded separately so reconnection can
    invalidate every previously remembered symbol state before new evidence is
    accepted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    state: SymbolTradingState
    source: str
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    source_timestamp_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    reason_code: str | None = None
    reason: str | None = None


class MarketLivenessFact(BaseModel):
    """Fresh, symbol-scoped operational answer composed from live evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    state: MarketLivenessState
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    market_clock: MarketClockLivenessEvidence
    symbol_status: SymbolTradingStatusEvidence | None
    reason_code: str
    reason: str
    market_data: SymbolMarketDataEvidence | None = None


class TopOfBookQuote(BaseModel):
    """One symbol's live IBKR best bid and ask, as the status source last read them.

    ``observed_at_ms`` is the older receipt of the current bid and ask.
    Reading or publishing this value never advances its freshness.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    source: str
    observed_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)


class MarketStatusSnapshot(BaseModel):
    """Authenticated status-source observation shared with a Paper worker.

    Vendor transition times remain unchanged. The snapshot timestamp proves
    only the source's current connection state, never a new symbol event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["alpaca.stock_data.status", "ibkr.market_data.status"] = "alpaca.stock_data.status"
    connected: bool
    observed_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    connection_changed_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    symbol_statuses: tuple[SymbolTradingStatusEvidence, ...]
    quotes: tuple[TopOfBookQuote, ...] = ()
    subscriptions: tuple[SymbolMarketDataEvidence, ...] = ()
