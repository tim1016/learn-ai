"""Run-owned evidence that indicators have settled, separate from feed health."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class IndicatorProgress(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bars_done: int = Field(ge=0)
    bars_needed: int = Field(ge=0)
    converged: bool
    decision_timeframe_ms: int = Field(gt=0)


class SpawningView(IndicatorProgress):
    label: str
    explanation: str
    sessions_left: int | None = Field(default=None, ge=0)
