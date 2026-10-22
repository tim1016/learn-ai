"""Authenticated dispatch of the API's ordinary internal preparation job (#3440)."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.options_backtest_runs import JOB_ID_MAX_LENGTH, JOB_ID_PATTERN, RUN_ID_MAX, OptionsBacktestJobRequest


class OptionsBacktestPreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(min_length=1, max_length=JOB_ID_MAX_LENGTH, pattern=JOB_ID_PATTERN)
    run_id: int = Field(ge=1, le=RUN_ID_MAX)
    generation: int = Field(ge=1)
    identity: str = Field(pattern="^[a-f0-9]{64}$")
    parent: OptionsBacktestJobRequest
