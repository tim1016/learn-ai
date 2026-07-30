"""Backend-owned contract for the static Alpaca Clerk diagnostic gallery.

The gallery consumes committed fixture documents locally, but their shape and
all semantic copy belong to this Pydantic model.  The small read-only router
that references the model exists solely to expose the model through OpenAPI so
the Angular type is mechanically generated rather than re-authored.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScenarioId = Literal[
    "running_ready_flat",
    "running_attributed_long",
    "entry_partial_pending",
    "exit_cancelling_entry",
    "exit_closing",
    "exit_complete",
    "stopped_flat",
    "stop_requires_flatten",
    "stopped_carryover_intact",
    "stopped_carryover_mismatch",
    "instance_submit_uncertain",
    "market_data_stale",
    "account_unattributable",
    "account_unprovable",
    "known_manual_exposure",
]
Tone = Literal["neutral", "verified", "active", "caution", "blocked"]


class AuthoredValue(BaseModel):
    """One server-presented label/value pair; Angular does not compose it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    value: str = Field(min_length=1)


class StaticAction(BaseModel):
    """A deliberately inert action example for the static gallery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    availability: Literal["available", "blocked", "terminal"]
    tone: Tone


class PricePoint(BaseModel):
    """Compact, authored chart context; not a market-data API response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    price: str = Field(pattern=r"^\$[0-9]+(?:\.[0-9]{2})?$")
    fill_label: str | None = None
    fill_tone: Tone | None = None


class TraderDiagnosticView(BaseModel):
    """All Trader-lens meaning, authored by the fixture producer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1)
    status_label: str = Field(min_length=1)
    status_tone: Tone
    truth_rows: list[AuthoredValue] = Field(min_length=3, max_length=3)
    headline: str = Field(min_length=1)
    operation_summary: str = Field(min_length=1)
    price_context_label: str = Field(min_length=1)
    price_points: list[PricePoint] = Field(min_length=3, max_length=8)
    fill_summary: str = Field(min_length=1)
    primary_action: StaticAction


class CustodySpineStep(BaseModel):
    """One explicitly presented custody step in the operator diagnostic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    state_label: str = Field(min_length=1)
    tone: Tone


class ExposureSlice(BaseModel):
    """One attribution bucket, including known Clerk manual activity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    quantity_label: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class ResumeComparison(BaseModel):
    """An already-evaluated Resume checkpoint comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    checkpoint: str = Field(min_length=1)
    observed: str = Field(min_length=1)
    result_label: str = Field(min_length=1)
    tone: Tone


class EvidenceItem(BaseModel):
    """Bounded evidence with a code label and opaque identifier kept verbatim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    code: str = Field(pattern=r"^[A-Z0-9_]+$")
    opaque_id: str = Field(min_length=1)
    age_label: str = Field(min_length=1)


class OperatorDiagnosticView(BaseModel):
    """All Operator-lens diagnosis, evidence, and action availability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope_cards: list[AuthoredValue] = Field(min_length=3, max_length=3)
    admission_label: str = Field(min_length=1)
    admission_explanation: str = Field(min_length=1)
    reduction_label: str = Field(min_length=1)
    reduction_explanation: str = Field(min_length=1)
    custody_spine_label: str = Field(min_length=1)
    custody_spine: list[CustodySpineStep] = Field(min_length=5, max_length=5)
    exposure_label: str = Field(min_length=1)
    exposure_slices: list[ExposureSlice] = Field(min_length=1)
    resume_label: str = Field(min_length=1)
    resume_result: str = Field(min_length=1)
    resume_comparisons: list[ResumeComparison] = Field(min_length=4, max_length=4)
    freeze_label: str = Field(min_length=1)
    freeze_explanation: str = Field(min_length=1)
    next_step: str = Field(min_length=1)
    evidence_label: str = Field(min_length=1)
    evidence: list[EvidenceItem] = Field(min_length=1, max_length=4)
    actions: list[StaticAction] = Field(min_length=1, max_length=4)


class AlpacaBotControlFixtureEnvelope(BaseModel):
    """Versioned server-presented fixture envelope for one diagnostic scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"]
    scenario_id: ScenarioId
    scenario_label: str = Field(min_length=1)
    review_note: str = Field(min_length=1)
    trader: TraderDiagnosticView
    operator: OperatorDiagnosticView
