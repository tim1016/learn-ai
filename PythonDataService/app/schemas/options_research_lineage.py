"""Frozen procedure and bounded durable trial reads for Options research (#3400)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.options_golden_search import EvidenceItem, Exposure, GateOutcome
from app.utils.session_anchors import MAX_TIMESTAMP_MS


class OptionsLineageWindow(BaseModel):
    key: str
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    scheduled_sessions: int
    activity_floor: int


class OptionsLineageSetting(BaseModel):
    path: str
    mode: Literal["search", "fixed"]
    values: list[str]
    priority: int


class OptionsLineageFold(BaseModel):
    index: int
    training: OptionsLineageWindow
    forward: OptionsLineageWindow
    state: Literal["not_recorded", "no_eligible_point", "evaluated"]
    winner_hash: str | None
    winner_label: str | None
    forward_net_profit_usd: str | None
    forward_eligible: bool | None


class OptionsLineageCounts(BaseModel):
    receipt_state: Literal["recorded", "not_recorded"]
    distinct_development_candidates: int | None
    eligible_development_candidates: int | None
    rejected_development_candidates: int | None
    evaluation_keys: int | None
    completed_evaluations: int | None
    physical_starts: int | None
    trial_events: int
    evaluated_trial_events: int
    cache_hit_trial_events: int
    invalid_trial_events: int


class OptionsLineageStress(BaseModel):
    key: str
    mandatory: bool
    slippage_multiplier: str
    fee_multiplier: str
    slippage_percent: str | None
    fee_per_contract_usd: str | None


class OptionsLineageFinalist(BaseModel):
    candidate_hash: str
    state: Literal["uncomputed", "retained"]
    verdict: str | None
    gate_outcomes: list[GateOutcome]
    evidence: list[EvidenceItem]


class OptionsLineageExposure(BaseModel):
    receipt_id: str
    start_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    scope: Literal["development", "final", "other_recorded_interval"]


class OptionsResearchLineage(BaseModel):
    study_id: str
    protocol_hash: str
    revision: int
    parent_study_id: str | None
    method: str
    objective: str
    selection_explanation: str
    procedure_details: list[EvidenceItem]
    development_state: Literal["not_recorded", "no_eligible_point", "selected"]
    development_winner_hash: str | None
    development_winner_label: str | None
    selection_complete: bool | None
    selection_limitation: str | None
    development: OptionsLineageWindow
    final: OptionsLineageWindow
    settings: list[OptionsLineageSetting]
    rules: list[EvidenceItem]
    counts: OptionsLineageCounts
    folds: list[OptionsLineageFold]
    finalists: list[OptionsLineageFinalist]
    final_state: Literal["not_opened", "opened"]
    stress: list[OptionsLineageStress]
    exposure: Exposure
    exposure_ledger: list[OptionsLineageExposure]
    exposure_ledger_complete: bool


class OptionsLineageTrial(BaseModel):
    receipt_id: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    window: str
    ordinal: int
    role: str
    status: Literal["evaluated", "cache_hit", "invalid"]
    candidate_hash: str | None
    settings: list[EvidenceItem]
    reason: str | None


class OptionsLineageTrialPage(BaseModel):
    study_id: str
    protocol_hash: str
    items: list[OptionsLineageTrial]
    next_cursor: str | None
    limit: int
