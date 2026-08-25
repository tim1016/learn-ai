"""Pydantic schemas for run-scoped replay proof receipts (Direction 2).

All temporal fields are ``int64 ms UTC`` and all response fields are snake_case.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_INT64_MAX = 2**63 - 1
"""Signed int64 ceiling: persisted ms-UTC values must fit the wire/storage contract."""

RunReplayStatus = Literal[
    "pending",
    "parity",
    "parity_with_expected_live_effects",
    "indeterminate",
    "drift",
    "replay_failed",
]


class EngineParityDivergenceModel(BaseModel):
    """The first field where the BacktestEngine and runner-seam traces disagree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    evaluation_id: str | None
    field: str
    expected: str
    observed: str


class RunReplayDivergenceModel(BaseModel):
    """One classified fidelity-leg disagreement between replay and live record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_id: str
    bar_close_ms: int = Field(ge=0, le=_INT64_MAX)
    classification: Literal["expected_live_effect", "drift"]
    reason_code: str
    replay_staged: str | None
    live_outcome: str | None
    detail: str


class RunReplayReceipt(BaseModel):
    """Durable parity receipt for one completed run (Direction 2). All temporal fields are int64 ms UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_instance_id: str
    run_id: str
    strategy_key: str
    symbol: str
    provider: str
    status: RunReplayStatus
    bar_set_digest: str
    retained_bar_count: int = Field(ge=0)
    ledger_end_seq: int | None
    engine_parity_trace_root: str | None
    engine_parity_compared_count: int = Field(ge=0)
    engine_parity_divergence: EngineParityDivergenceModel | None
    live_compared_count: int = Field(ge=0)
    match_count: int = Field(ge=0)
    expected_live_effect_count: int = Field(ge=0)
    drift_count: int = Field(ge=0)
    digest_verified_count: int = Field(ge=0)
    records_truncated: bool
    divergences: list[RunReplayDivergenceModel]
    program_version: str | None
    sealed_program_hash: str | None
    generated_at_ms: int = Field(ge=0, le=_INT64_MAX)
    error: str | None = None


__all__ = [
    "EngineParityDivergenceModel",
    "RunReplayDivergenceModel",
    "RunReplayReceipt",
    "RunReplayStatus",
]
