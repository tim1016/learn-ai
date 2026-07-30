"""Wire contracts for deterministic offline bot-trading replay.

All temporal fields are ``int64 ms UTC``. A replay trading date is anchored
at the canonical NYSE session open; no date strings cross the API boundary.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

OfflineReplaySymbol = Literal["SPY", "TSLA"]
OfflineReplaySpeed = Literal[1, 10, 60]
OfflineReplayStatus = Literal[
    "preparing",
    "warming_up",
    "running",
    "paused",
    "stopping",
    "completed",
    "stopped",
    "failed",
    "interrupted",
]
OfflineReplayBotStatus = Literal["pending", "running", "completed", "failed", "stopped"]
OfflineReplayCommandAction = Literal["pause", "resume", "step", "set_speed", "stop"]


class OfflineReplayCatalogSession(BaseModel):
    """One selectable historical NYSE session."""

    model_config = ConfigDict(frozen=True)

    session_date_ms: int
    session_open_ms: int
    session_close_ms: int
    eligible: bool
    cached_symbols: list[OfflineReplaySymbol]


class OfflineReplayCatalogResponse(BaseModel):
    """Recent completed sessions and the recommended launch date."""

    model_config = ConfigDict(frozen=True)

    sessions: list[OfflineReplayCatalogSession]
    recommended_session_date_ms: int | None


class OfflineReplayCreateRequest(BaseModel):
    """Launch one synchronized SPY/TSLA offline replay session."""

    model_config = ConfigDict(frozen=True)

    session_date_ms: int = Field(gt=0)
    symbols: list[OfflineReplaySymbol] = Field(default_factory=lambda: ["SPY", "TSLA"])
    playback_minutes: Literal[30, 60] = 60
    speed: OfflineReplaySpeed = 60
    initial_cash_usd: Decimal = Field(default=Decimal("100000"), gt=0)
    auto_fetch: bool = True

    @field_validator("symbols")
    @classmethod
    def _symbols_are_unique(cls, symbols: list[OfflineReplaySymbol]) -> list[OfflineReplaySymbol]:
        if len(symbols) != len(set(symbols)):
            raise ValueError("replay symbols must be unique")
        if set(symbols) != {"SPY", "TSLA"}:
            raise ValueError("offline replay v1 requires exactly SPY and TSLA")
        return ["SPY", "TSLA"]


class OfflineReplayCommandRequest(BaseModel):
    """Media-style command for an active replay."""

    model_config = ConfigDict(frozen=True)

    action: OfflineReplayCommandAction
    speed: OfflineReplaySpeed | None = None

    @model_validator(mode="after")
    def _speed_matches_action(self) -> OfflineReplayCommandRequest:
        if self.action == "set_speed" and self.speed is None:
            raise ValueError("speed is required when action is set_speed")
        if self.action != "set_speed" and self.speed is not None:
            raise ValueError("speed is only valid when action is set_speed")
        return self


class OfflineReplayBotResult(BaseModel):
    """Current or terminal result for one isolated simulated bot account."""

    model_config = ConfigDict(frozen=True)

    symbol: OfflineReplaySymbol
    status: OfflineReplayBotStatus
    run_id: str
    bars_processed: int = Field(ge=0)
    decisions_emitted: int = Field(ge=0)
    orders_submitted: int = Field(ge=0)
    fills_observed: int = Field(ge=0)
    open_position_quantity: int
    final_equity_usd: Decimal
    data_sha256: str
    failure_code: str | None = None
    failure_message: str | None = None


class OfflineReplaySessionResponse(BaseModel):
    """Durable replay-session projection consumed by the control panel."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    status: OfflineReplayStatus
    strategy_key: Literal["ema_crossover_signal"]
    session_date_ms: int
    session_open_ms: int
    session_close_ms: int
    warmup_end_ms: int
    playback_end_ms: int
    playhead_ms: int | None
    playback_minutes: Literal[30, 60]
    speed: OfflineReplaySpeed
    created_at_ms: int
    started_at_ms: int | None
    completed_at_ms: int | None
    symbols: list[OfflineReplaySymbol]
    bots: list[OfflineReplayBotResult]
    failure_code: str | None = None
    failure_message: str | None = None


class OfflineReplaySessionListResponse(BaseModel):
    """Newest-first replay session listing."""

    model_config = ConfigDict(frozen=True)

    sessions: list[OfflineReplaySessionResponse]
