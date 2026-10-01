"""HTTP contracts for Golden Search qualified versions, their stock defaults and the Deploy offer (#2696)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from pydantic.alias_generators import to_camel

from app.research.golden_search.qualification_service import (
    JudgedDefault,
    JudgedQualification,
    StatusName,
    public_parameters,
)
from app.research.golden_search.qualifications import QualificationEvent
from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: Same shape as the Deploy submission key: 8-64 letters, digits, ``-`` or ``_``.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"


class _CamelTolerantModel(BaseModel):
    """Accepts camelCase (the .NET passthrough) and snake_case (direct FastAPI calls)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class RevokeQualificationRequest(_CamelTolerantModel):
    reason: str = Field(min_length=3, max_length=4000)
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)

    @field_validator("reason")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


class ReproveQualificationRequest(_CamelTolerantModel):
    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)


class GoldenQualificationResearch(BaseModel):
    """What the research said when the version was approved; preserved forever, never upgraded by approval."""

    exam_outcome: str | None = None
    claim: str | None = None
    exposure_state: str | None = None
    validation_verdict_label: str | None = None
    research_override: bool = False
    weakness: list[str] = Field(default_factory=list)

    @classmethod
    def from_research(cls, research: Mapping[str, Any]) -> GoldenQualificationResearch:
        def text(name: str) -> str | None:
            value = research.get(name)
            return value if isinstance(value, str) else None

        weakness = research.get("weakness")
        return cls(
            exam_outcome=text("exam_outcome"),
            claim=text("claim"),
            exposure_state=text("exposure_state"),
            validation_verdict_label=text("validation_verdict_label"),
            research_override=research.get("research_override") is True,
            weakness=[str(code) for code in weakness] if isinstance(weakness, list) else [],
        )


class GoldenQualificationSummary(BaseModel):
    id: str
    program_key: str
    program_version: str
    symbol: str
    # Canonical parameters without the stock: what the Deploy form takes.
    parameters: dict[str, JsonValue]
    status: StatusName
    status_explanation: str
    is_default: bool
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    note: str
    approved_by: str
    research: GoldenQualificationResearch
    study_id: str
    golden_run_id: int

    @classmethod
    def from_judged(cls, judged: JudgedQualification) -> GoldenQualificationSummary:
        row = judged.qualification
        return cls(
            id=row.id,
            program_key=row.program_key,
            program_version=row.program_version,
            symbol=row.symbol,
            parameters=public_parameters(row),
            status=judged.status,
            status_explanation=judged.explanation,
            is_default=judged.is_default,
            created_at_ms=row.created_at_ms,
            note=row.note,
            approved_by=row.approved_by,
            research=GoldenQualificationResearch.from_research(row.research),
            study_id=row.study_id,
            golden_run_id=row.golden_run_id,
        )


class GoldenQualificationEventView(BaseModel):
    id: int
    kind: Literal["reproved", "revoked"]
    artifact_digest: str | None
    wiring_digest: str | None
    reason: str | None
    actor: str
    created_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)

    @classmethod
    def from_event(cls, event: QualificationEvent) -> GoldenQualificationEventView:
        return cls(
            id=event.id,
            kind=event.kind,
            artifact_digest=event.artifact_digest,
            wiring_digest=event.wiring_digest,
            reason=event.reason,
            actor=event.actor,
            created_at_ms=event.created_at_ms,
        )


class GoldenQualificationProofView(BaseModel):
    """The approval proof's identity: what was replayed, over which window, to which trace root."""

    window_start_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    window_end_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    warmup_from_ms: int | None = Field(ge=0, le=MAX_TIMESTAMP_MS)
    trace_count: int | None
    lake_trace_root: str | None
    restored_trace_root: str | None
    input_count: int

    @classmethod
    def from_proof(cls, proof: Mapping[str, Any]) -> GoldenQualificationProofView:
        def number(value: object) -> int | None:
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        def text(value: object) -> str | None:
            return value if isinstance(value, str) else None

        window = proof.get("window")
        window = window if isinstance(window, Mapping) else {}
        manifest = proof.get("manifest")
        return cls(
            window_start_ms=number(window.get("start_ms")),
            window_end_ms=number(window.get("end_ms")),
            warmup_from_ms=number(proof.get("warmup_from_ms")),
            trace_count=number(proof.get("trace_count")),
            lake_trace_root=text(proof.get("lake_trace_root")),
            restored_trace_root=text(proof.get("restored_trace_root")),
            input_count=len(manifest) if isinstance(manifest, Mapping) else 0,
        )


class GoldenQualificationDetail(GoldenQualificationSummary):
    parameter_schema_version: str
    params_sha256: str
    artifact_digest: str
    wiring_digest: str
    golden_review_id: int
    proof_sha256: str
    proof: GoldenQualificationProofView
    events: list[GoldenQualificationEventView]

    @classmethod
    def from_judged(cls, judged: JudgedQualification) -> GoldenQualificationDetail:
        row = judged.qualification
        return cls(
            **GoldenQualificationSummary.from_judged(judged).model_dump(),
            parameter_schema_version=row.parameter_schema_version,
            params_sha256=row.params_sha256,
            artifact_digest=row.artifact_digest,
            wiring_digest=row.wiring_digest,
            golden_review_id=row.golden_review_id,
            proof_sha256=row.proof_sha256,
            proof=GoldenQualificationProofView.from_proof(row.proof),
            events=[GoldenQualificationEventView.from_event(event) for event in judged.events],
        )


class GoldenDefaultView(BaseModel):
    """One stock's current default for one program: the version Deploy offers first."""

    program_key: str
    symbol: str
    qualification_id: str
    revision: int
    updated_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    status: StatusName
    status_explanation: str

    @classmethod
    def from_judged(cls, item: JudgedDefault) -> GoldenDefaultView:
        return cls(
            program_key=item.default.program_key,
            symbol=item.default.symbol,
            qualification_id=item.judged.qualification.id,
            revision=item.default.revision,
            updated_at_ms=item.default.updated_at_ms,
            status=item.judged.status,
            status_explanation=item.judged.explanation,
        )


class GoldenQualificationDeployOffer(BaseModel):
    """The exact tuple Deploy applies when the owner chooses "Use in Deploy"; never applied unless ready."""

    qualification_id: str
    program_key: str
    program_version: str
    symbol: str
    parameters: dict[str, JsonValue]
    status: StatusName
    is_default: bool
    explanation: str
    research: GoldenQualificationResearch

    @classmethod
    def from_judged(cls, judged: JudgedQualification) -> GoldenQualificationDeployOffer:
        row = judged.qualification
        return cls(
            qualification_id=row.id,
            program_key=row.program_key,
            program_version=row.program_version,
            symbol=row.symbol,
            parameters=public_parameters(row),
            status=judged.status,
            is_default=judged.is_default,
            explanation=judged.explanation,
            research=GoldenQualificationResearch.from_research(row.research),
        )
