"""Read-only feed wire identity and evidence (ADR 0082)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.session_anchors import MAX_TIMESTAMP_MS


class WireModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Instrument(WireModel):
    """A qualified instrument; exchange and currency are price semantics."""

    con_id: int = Field(gt=0)
    exchange: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=1, max_length=8)
    sec_type: str = Field(min_length=1, max_length=8)


class Subscription(WireModel):
    instrument: Instrument
    kind: Literal["bars", "quotes"]
    # One owner/entitlement session. Never silently downgrade to delayed data.
    data_type: Literal[1] = 1


class SourceBar(WireModel):
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    open: str
    high: str
    low: str
    close: str
    volume: str
    venue: str | None = None


class Tick(WireModel):
    kind: int
    price: float | None
    size: float | None


class QuoteUpdate(WireModel):
    data_type: int
    ticks: tuple[Tick, ...] = Field(max_length=512)
    values: dict[str, float | None]
    greeks: dict[str, dict[str, float | None]]
    trade_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)


class Frame(WireModel):
    kind: Literal["bar", "quote", "idle", "error"]
    epoch: str = Field(max_length=128)
    received_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    sequence: int = Field(default=0, ge=0)
    bar: SourceBar | None = None
    quote: QuoteUpdate | None = None
    reason: str = ""
    cause: Literal["socket_down", "soft_loss_1100", "data_lost_1101", "generation_changed", "stalled"] | None = None
    error_code: int | None = None

    @model_validator(mode="after")
    def payload_matches_tag(self) -> Frame:
        if (self.bar is not None) != (self.kind == "bar") or (self.quote is not None) != (self.kind == "quote"):
            raise ValueError("Shared feed frame payload does not match its kind")
        return self


class FeedUnavailable(Exception):
    """A source or delivery failed. It never grants permission to trade."""


class SourceHealth(WireModel):
    connected: bool
    epoch: str
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
