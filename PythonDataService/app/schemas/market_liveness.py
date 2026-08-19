"""Typed real-time market-liveness evidence for operational decisions.

Scheduled session structure remains the canonical calendar's responsibility.
These models answer the separate, present-tense question of whether a symbol can
be traded now, using only live vendor evidence. Every timestamp is ``int64 ms
UTC`` at the model boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MarketLivenessState = Literal["TRADABLE", "HALTED", "CLOSED", "UNKNOWN"]
MarketClockState = Literal["OPEN", "CLOSED", "UNKNOWN"]
SymbolTradingState = Literal["TRADABLE", "HALTED", "UNKNOWN"]


class MarketClockLivenessEvidence(BaseModel):
    """One market-wide, broker-originated live-clock observation.

    This is deliberately not scheduled-session evidence: an ``OPEN`` result
    can only say that the broker currently considers the market open. It says
    nothing about whether an individual symbol is halted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: MarketClockState
    source: str
    observed_at_ms: int = Field(ge=0)
    vendor_timestamp_ms: int | None = Field(default=None, ge=0)
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
    observed_at_ms: int = Field(ge=0)
    source_timestamp_ms: int | None = Field(default=None, ge=0)
    reason_code: str | None = None
    reason: str | None = None


class MarketLivenessFact(BaseModel):
    """Fresh, symbol-scoped operational answer composed from live evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    state: MarketLivenessState
    observed_at_ms: int = Field(ge=0)
    market_clock: MarketClockLivenessEvidence
    symbol_status: SymbolTradingStatusEvidence | None
    reason_code: str
    reason: str
