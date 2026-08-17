"""FastAPI response contracts for persisted Exhaustive Run evidence."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.research.exhaustive_run.result import ExhaustiveRunConfig, ExhaustiveRunResult


class ExhaustiveRunResponse(BaseModel):
    """One persisted Exhaustive Run config/result pair."""

    model_config = ConfigDict(extra="forbid")

    config: ExhaustiveRunConfig
    result: ExhaustiveRunResult
