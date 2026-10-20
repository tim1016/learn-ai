"""Server-authored premium panes and time-bounded rules (#3368)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.options.contract import OptionContract
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class ChartRuleLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["starting_price", "momentum", "stop"]
    price: str
    from_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    to_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    leg_id: str
    entry_number: int = Field(ge=0)
    source: Literal["live", "backtest"] = "live"


class PremiumPoint(BaseModel):
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    value: str | None


class PremiumFill(BaseModel):
    filled_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    symbol: str
    order_ref: str
    event_key: str
    exit_reason: str | None = None
    source: Literal["live", "backtest"] = "live"


class ChartPremiumPane(BaseModel):
    contract: OptionContract
    symbol: str
    multiplier: int
    leg_ids: list[str]
    entry_numbers: list[int]
    points: list[PremiumPoint]
    rule_lines: list[ChartRuleLine]
    fill_markers: list[PremiumFill]
    notice: str | None = None
    backtest_only: bool = False
    backtest_points: list[PremiumPoint] = Field(default_factory=list)
