"""Schemas for the rebuildable lifecycle Postgres projection."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.live_runs import LifecycleChartStatus, LifecycleEventCategory, LifecycleEventSeverity

LifecycleProjectionTable = Literal["bot_lifecycle_events", "account_lifecycle_events"]


class LifecycleProjectionEventRow(BaseModel):
    """One persisted lifecycle projection row returned by the read API."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    account_id: str
    strategy_instance_id: str | None = None
    run_id: str | None = None
    event_id: str
    event_type: str
    category: LifecycleEventCategory
    node_id: str | None = None
    gate_id: str | None = None
    status: LifecycleChartStatus | None = None
    severity: LifecycleEventSeverity
    ts_ms: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    ts_ms_resolved: bool
    source_artifact: str
    source_type: str
    source_rank: int = Field(ge=0)
    source_seq: int | None = Field(default=None, ge=0)
    source_offset: int | None = Field(default=None, ge=0)
    source_hash: str | None = Field(default=None, min_length=64, max_length=64)
    summary: str
    why: str | None = None
    operator_next_step: str | None = None
    receipt_payload: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    rendered_headline: str | None = None
    rendered_template_id: str | None = None
    inserted_at_ms: int | None = Field(default=None, ge=0)
    updated_at_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _timestamp_resolution_contract(self) -> LifecycleProjectionEventRow:
        if self.ts_ms is None and self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved cannot be true when ts_ms is absent")
        if self.ts_ms is not None and not self.ts_ms_resolved:
            raise ValueError("ts_ms_resolved=false is reserved for absent timestamps")
        return self


class LifecycleTimelineResponse(BaseModel):
    """Bounded timeline response for bot/account/run lifecycle events."""

    model_config = ConfigDict(extra="forbid")

    projection_available: bool
    canonical_fallback_required: bool
    rows: list[LifecycleProjectionEventRow]


LifecycleSafetySeverity = Literal["warning", "critical"]


class LifecycleSafetyProjectionEventRow(LifecycleProjectionEventRow):
    """Safety triage row with the endpoint's warning/critical invariant."""

    severity: LifecycleSafetySeverity


class LifecycleSafetyTriageResponse(BaseModel):
    """Bounded safety query over warning/critical lifecycle projection rows."""

    model_config = ConfigDict(extra="forbid")

    projection_available: bool
    canonical_fallback_required: bool
    rows: list[LifecycleSafetyProjectionEventRow]


class AccountOwnerStatusSnapshotRow(BaseModel):
    """Persisted AccountOwner generation and phase snapshot."""

    model_config = ConfigDict(extra="forbid")

    account_id: str
    generation: int = Field(ge=0)
    phase: Literal["accepting", "reconnecting", "draining", "frozen"]
    recorded_at_ms: int = Field(ge=0)
    ts_ms_resolved: Literal[True] = True
    source_artifact: str
    source_seq: int | None = Field(default=None, ge=0)
    source_offset: int | None = Field(default=None, ge=0)
    source_hash: str | None = Field(default=None, min_length=64, max_length=64)
    receipt_payload: dict[str, Any] = Field(default_factory=dict)
    inserted_at_ms: int = Field(ge=0)
    updated_at_ms: int = Field(ge=0)
