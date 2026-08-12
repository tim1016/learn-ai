"""Contract schemas for the broker-v2 bot gallery aggregated stream.

Backend-authored, broker-generic Pydantic models for the live 20-bot
candlestick wall (one Alpaca account). This module aggregates across bots and
symbols into a single snapshot/update pair for the gallery's SSE channel; it
reuses ``ChartBar`` and ``ChartFillMarker`` from ``app.schemas.broker_v2_panel``
rather than redefining bar/marker shapes.

Temporal fields are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.broker_v2_panel import ChartBar, ChartFillMarker


class GalleryPrimaryAction(BaseModel):
    """The single most relevant action for a gallery tile (§ gallery spec)."""

    model_config = ConfigDict(frozen=True)

    action_id: str
    label: str
    enabled: bool
    disabled_reason: str | None = None


class GalleryBotView(BaseModel):
    """One bot's tile state in the gallery wall."""

    model_config = ConfigDict(frozen=True)

    sid: str
    symbol: str
    label: str
    running: bool
    phase: str
    desired_state: str
    needs_attention: bool
    realized_pnl_today: float
    open_pnl: float
    fills_today: int
    last_bar_at_ms: int | None = None
    primary_action: GalleryPrimaryAction


class GalleryBotDelta(BaseModel):
    """Incremental per-bot update carried in a ``GalleryLiveUpdate``."""

    model_config = ConfigDict(frozen=True)

    sid: str
    realized_pnl_today: float
    open_pnl: float
    fills_today: int
    phase: str
    desired_state: str
    needs_attention: bool
    running: bool
    last_bar_at_ms: int | None = None
    primary_action: GalleryPrimaryAction


class GallerySymbolBars(BaseModel):
    """Bars for one symbol shared across the tiles that chart it."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    bars: list[ChartBar] = Field(default_factory=list)


class GalleryLiveSnapshot(BaseModel):
    """Versioned complete state document for the gallery's REST bootstrap and SSE."""

    model_config = ConfigDict(frozen=True)

    stream_epoch: str
    surface_version: int
    as_of_ms: int
    resolution: str = "1m"
    bots: list[GalleryBotView]
    symbols: list[GallerySymbolBars]
    markers: dict[str, list[ChartFillMarker]] = Field(default_factory=dict)


class GalleryLiveUpdate(BaseModel):
    """Incremental gallery update carried over the SSE stream."""

    model_config = ConfigDict(frozen=True)

    surface_version: int
    as_of_ms: int
    symbols: list[GallerySymbolBars] = Field(default_factory=list)
    markers_delta: dict[str, list[ChartFillMarker]] = Field(default_factory=dict)
    bots_delta: list[GalleryBotDelta] = Field(default_factory=list)
    removed_sids: list[str] = Field(default_factory=list)
