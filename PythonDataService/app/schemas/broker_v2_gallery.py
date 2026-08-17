"""Contract schemas for the broker-v2 bot gallery aggregated stream.

Backend-authored, broker-generic Pydantic models for the live 20-bot
candlestick wall (one Alpaca account). This module aggregates across bots and
symbols into a single snapshot/update pair for the gallery's SSE channel; it
reuses ``ChartBar`` and ``ChartFillMarker`` from ``app.schemas.broker_v2_panel``
rather than redefining bar/marker shapes.

Temporal fields are ``int64 ms UTC`` per ``.claude/rules/temporal-rigor.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.broker_v2_panel import ChartBar, ChartFillMarker

GalleryResolution = Literal["5s", "1m"]


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
    realized_pnl_today: float | None
    open_pnl: float | None
    # Null-safe sum of the two fields above, computed once here (not
    # re-derived client-side — CLAUDE.md single-source-of-truth rule: a
    # frontend addition of two already-fetched numbers is still a second P&L
    # authority outside Python). `None` only when both components are `None`;
    # a lone-present component contributes its own value, matching the
    # existing "show whichever side is present" display intent.
    day_pnl: float | None
    # Session return `(last_close - session_open) / session_open`, scoped to
    # TODAY's session (see gallery_hub._session_change_pct). Computed here,
    # not client-side from `bars[0]`: the shared per-symbol bar buffer can
    # (and does) retain a prior session's tail bars, so `bars[0]` is not
    # reliably today's session open — the same single-numerical-authority
    # reasoning as `day_pnl`. `None` when no bar has fallen within today's
    # session yet, or the session's first open is zero.
    session_change_pct: float | None
    fills_today: int | None
    last_bar_at_ms: int | None = None
    primary_action: GalleryPrimaryAction


class GalleryBotDelta(GalleryBotView):
    """Incremental per-bot update carried in a ``GalleryLiveUpdate``.

    Self-contained: a delta carries the changed bot's full view (including
    ``symbol``/``label``) so the client can replace a bot by ``sid`` alone.
    """


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
    resolution: GalleryResolution
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
