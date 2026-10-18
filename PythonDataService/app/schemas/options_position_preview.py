"""Read-only contract resolution and expiry-risk preview for the options setup desk."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class PositionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    setup: OptionsBacktestSetup
    source: Literal["historical", "current"] = "historical"
    trading_date_ms: int | None = Field(None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    mode: Literal["position", "replay"] = "position"
    replay_at_ms: int | None = Field(None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)

    @model_validator(mode="after")
    def require_history(self) -> PositionPreviewRequest:
        if self.source == "historical" and self.trading_date_ms is None:
            raise ValueError("Choose a historical session to preview")
        if self.mode == "replay" and self.source != "historical":
            raise ValueError("Session replay requires historical data")
        return self


class PositionPreviewLeg(BaseModel):
    leg_id: str
    symbol: str
    side: Literal["buy", "sell"]
    right: Literal["call", "put"]
    contracts: int
    strike_usd: str
    expiry_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    premium_usd: str
    price_at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    price_source: str


class PositionPreviewCurve(BaseModel):
    leg_id: str | None = None
    pnl_usd: list[str]


class PositionPreviewRisk(BaseModel):
    entry_debit_usd: str
    max_profit_usd: str | None
    max_loss_usd: str | None
    breakevens_usd: list[str]


class PositionPreviewEvent(BaseModel):
    at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    leg_id: str
    kind: str
    detail: str | None = None


class PositionPreviewResponse(BaseModel):
    source: Literal["historical", "current"]
    mode: Literal["position", "replay"]
    at_ms: int = Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    spot_usd: str
    session_open_ms: int | None = Field(None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    session_close_ms: int | None = Field(None, strict=True, ge=0, le=MAX_TIMESTAMP_MS)
    legs: list[PositionPreviewLeg]
    prices_usd: list[str]
    curves: list[PositionPreviewCurve]
    risk: PositionPreviewRisk | None
    notes: list[str]
    events: list[PositionPreviewEvent] = Field(default_factory=list)
