"""Durable, source-preserving evidence for a Paper/Live experiment (#2371)."""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionOutcome
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptPageResource
from app.utils.session_anchors import MAX_TIMESTAMP_MS

EpochMs = Annotated[int, Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)]
ExperimentLane = Literal["paper", "live"]


class ExperimentDecision(BaseModel):
    """One Clerk receipt; observation time is never substituted for decision time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(strict=True, ge=1)
    run_id: str | None = Field(min_length=1)
    recorded_at_ms: EpochMs
    decision_bar_close_ms: EpochMs | None = None
    trace_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: DecisionOutcome
    reason_code: str
    decision_id: str | None = Field(default=None, min_length=1)
    order_ref: str | None = None


class DecisionComparisonRow(BaseModel):
    """A full outer join on the decision clock, preserving ambiguous evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    decision_bar_close_ms: EpochMs | None
    status: Literal["same", "different", "paper_only", "live_only", "unverifiable"]
    detail: str
    paper: tuple[ExperimentDecision, ...]
    live: tuple[ExperimentDecision, ...]
    outcomes_match: bool | None = None
    reasons_match: bool | None = None


class DecisionComparisonCounts(BaseModel):
    """Disjoint decision categories, plus the separate execution-outcome count."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matching_decisions: int = Field(ge=0)
    divergent_decisions: int = Field(ge=0)
    unmatched_decisions: int = Field(ge=0)
    unverifiable_decisions: int = Field(ge=0)
    execution_outcome_differences: int = Field(ge=0)


class ExperimentSessionComparison(DecisionComparisonCounts):
    session_open_ms: EpochMs
    paper_run_ids: tuple[str, ...]
    live_run_ids: tuple[str, ...]


class PaperLiveComparison(DecisionComparisonCounts):
    rows: tuple[DecisionComparisonRow, ...]
    sessions: tuple[ExperimentSessionComparison, ...]
    evidence_complete: bool
    # This is specifically trace equivalence, never a trading qualification
    # or proof that entry/fill/exit paths have been exercised.
    all_decisions_match: bool
    first_difference_at_ms: EpochMs | None


class ExperimentSource(BaseModel):
    """Frozen provenance; an archive never follows a reassigned lane or database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    clerk_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    binding_generation: int = Field(strict=True, ge=1)
    db_identity_token: str = Field(min_length=1)
    strategy_instance_id: str = Field(min_length=1)


class PaperLiveEvidencePair(BaseModel):
    """An evidence pair, not deployment approval or proof of equal configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = Field(pattern=r"^plx_[0-9a-f]{32}$")
    created_at_ms: EpochMs
    paper: ExperimentSource
    live: ExperimentSource

    @model_validator(mode="after")
    def distinct_sources(self) -> Self:
        if self.paper.clerk_id == self.live.clerk_id or self.paper.account_id == self.live.account_id:
            raise ValueError("Paper and Live require distinct clerks and accounts")
        if self.paper.db_identity_token == self.live.db_identity_token:
            raise ValueError("Paper and Live require distinct custody databases")
        return self


class ExperimentEvidenceCapture(BaseModel):
    """A source observation; highest_seq is the source watermark, not the page size.

    Callers must include revisions to previously seen receipts as well as new
    sequences. Sequence coverage alone cannot prove that a final outcome was
    observed; captured_at_ms always accompanies the resulting report.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: ExperimentSource
    captured_at_ms: EpochMs
    highest_seq: int = Field(strict=True, ge=0)
    decisions: tuple[ExperimentDecision, ...]

    @model_validator(mode="after")
    def coherent_sequences(self) -> Self:
        sequences = [decision.seq for decision in self.decisions]
        if len(sequences) != len(set(sequences)):
            raise ValueError("A capture must contain each receipt sequence at most once")
        if any(seq > self.highest_seq for seq in sequences):
            raise ValueError("A receipt exceeds the source sequence watermark")
        return self


class ExperimentLaneCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    captured_at_ms: EpochMs | None
    highest_seq: int = Field(ge=0)
    missing_receipts: int = Field(ge=0)
    conflicting_receipts: int = Field(ge=0)


class PaperLiveEvidenceReport(BaseModel):
    """Atomically persisted evidence and comparison, as of both source captures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    pair: PaperLiveEvidencePair
    paper: ExperimentLaneCoverage
    live: ExperimentLaneCoverage
    comparison: PaperLiveComparison


class ExperimentDecisionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    captured_at_ms: EpochMs
    decision: ExperimentDecision


class ClerkDecisionEvidencePage(BaseModel):
    """A bounded source page; missing retained sequences are never concealed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str = Field(min_length=1)
    strategy_instance_id: str = Field(min_length=1)
    db_identity_token: str = Field(min_length=1)
    authority_generation: int = Field(strict=True, ge=1)
    config_hash: str = Field(min_length=1)
    account_mode: Literal["paper", "live"]
    authority_kind: Literal["sqlite", "synthetic", "shadow"]
    observed_at_ms: EpochMs
    after_seq: int = Field(strict=True, ge=0)
    highest_seq: int = Field(strict=True, ge=0)
    next_after_seq: int | None = Field(strict=True, ge=1)
    decisions: tuple[ExperimentDecision, ...] = Field(max_length=500)

    @model_validator(mode="after")
    def ordered_page(self) -> Self:
        sequences = [receipt.seq for receipt in self.decisions]
        if sequences != sorted(set(sequences)):
            raise ValueError("Evidence page sequences must be unique and ascending")
        if self.after_seq > self.highest_seq or any(
            seq <= self.after_seq or seq > self.highest_seq for seq in sequences
        ):
            raise ValueError("Evidence page is outside its sequence bounds")
        if self.next_after_seq is not None and (
            not sequences or self.next_after_seq != sequences[-1] or self.next_after_seq >= self.highest_seq
        ):
            raise ValueError("Evidence page continuation must advance within its source watermark")
        return self

    @classmethod
    def from_resource(
        cls,
        page: DecisionReceiptPageResource,
        *,
        account_mode: Literal["paper", "live"],
        authority_kind: Literal["sqlite", "synthetic", "shadow"],
    ) -> Self:
        decisions = []
        for receipt in page.receipts:
            facts = json.loads(receipt.facts_json)
            if not isinstance(facts, dict):
                raise ValueError("Decision receipt facts must be an object")
            decisions.append(
                ExperimentDecision.model_validate(
                    {
                        "seq": receipt.seq,
                        "run_id": facts.get("run_id"),
                        "recorded_at_ms": receipt.observed_at_ms,
                        "decision_bar_close_ms": facts.get("decision_bar_close_ms"),
                        "trace_digest": facts.get("trace_digest"),
                        "outcome": receipt.outcome,
                        "reason_code": facts.get("reason_code") or receipt.outcome,
                        "decision_id": facts.get("decision_id") or facts.get("evaluation_id"),
                        "order_ref": receipt.order_ref,
                    }
                )
            )
        return cls(
            account_id=page.meta.account_id,
            strategy_instance_id=page.strategy_instance_id,
            db_identity_token=page.meta.db_identity_token,
            authority_generation=page.meta.authority_generation,
            config_hash=page.config_hash,
            account_mode=account_mode,
            authority_kind=authority_kind,
            observed_at_ms=page.observed_at_ms,
            after_seq=page.after_seq,
            highest_seq=page.highest_seq,
            next_after_seq=page.next_after_seq,
            decisions=tuple(decisions),
        )
