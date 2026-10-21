"""Internal measurement receipts for the Options archive benchmark (#3433)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from app.options_backtest.timings import PhaseTiming
from app.utils.session_anchors import MAX_TIMESTAMP_MS

Count = Annotated[int, Field(strict=True, ge=0)]


class BenchmarkResourcesResponse(BaseModel):
    observed_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    runtime_instance: str
    cgroup_memory_bytes: Count | None
    cgroup_memory_limit_bytes: Count | None
    cpu_quota_us: Count | None
    cpu_period_us: Count | None
    frozen_input_bytes: Count
    database_connections: Count
    database_connection_limit: Count


class BenchmarkResultResponse(BaseModel):
    run_id: int
    accepted_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    saved_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    result_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    phase_timings: list[PhaseTiming]
    execution_metadata: dict[str, Any]


class BenchmarkPreflightRequest(BaseModel):
    case: Literal["spy_typical_v1", "spy_eight_leg_cover_v1"]


class BenchmarkPreflightResponse(BaseModel):
    case_digest: str
    inventory_digest: str
    sessions: Count
    incomplete_session_ms: list[int]
