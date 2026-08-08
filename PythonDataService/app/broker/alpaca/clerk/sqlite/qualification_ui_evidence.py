"""Typed validation for the bounded synthetic UI correlation receipt."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.broker.alpaca.clerk.sqlite.qualification_synthetic_rehearsal import (
    SyntheticTestOutcome,
)
from app.broker.alpaca.clerk.sqlite.qualification_ui_campaign_contract import (
    BOUNDED_UI_CAMPAIGN,
    UI_CAMPAIGN_CONTRACT_SHA256,
    BoundedUiCampaignContract,
)
from app.schemas.account_custody_qualification import INT64_MAX
from app.schemas.account_custody_synthetic_qualification import (
    SyntheticUiCorrelationEvidence,
    SyntheticUiCorrelationRecord,
)
from app.services.account_custody_synthetic_scenarios import (
    SyntheticScenarioId,
    synthetic_scenario,
)

_UI_SCENARIO = synthetic_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY)
if len(_UI_SCENARIO.test_refs) != 1:
    raise RuntimeError("the bounded UI campaign requires exactly one canonical test")
_UI_TEST_REF = _UI_SCENARIO.test_refs[0]


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_COMMIT_SHA_PATTERN = r"^[0-9a-f]{40}$"
_EvidenceRef = Annotated[str, Field(min_length=1, max_length=1_024)]


class PreverifiedUiMeasurements(BaseModel):
    """Exact measured counts emitted by the bounded browser campaign."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    campaign_id: str
    page_load_count: int
    browser_reload_count: int
    interaction_count: int
    evidence_request_count: int
    evidence_response_proof_reference_count: int
    native_event_source_connection_count: int
    lifecycle_request_count: int
    observed_revision_count: int
    lifecycle_request_action_ids: tuple[str, ...]
    revision_sequence: tuple[int, ...]
    historical_snapshot_state: str
    presented_action_id: str
    correlation_record_count: int

    @model_validator(mode="after")
    def validate_bounded_campaign(self) -> PreverifiedUiMeasurements:
        if self.model_dump(mode="python") != BOUNDED_UI_CAMPAIGN.expected_measurements():
            raise ValueError("preverified UI evidence measurements do not match the bounded campaign")
        return self


class PreverifiedUiReceiptV5(BaseModel):
    """Version-five content-addressed receipt accepted by qualification."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[5]
    test_ref: str
    command: tuple[str, ...]
    git_commit_sha: str = Field(pattern=_GIT_COMMIT_SHA_PATTERN)
    test_source_sha256: str = Field(pattern=_SHA256_PATTERN)
    campaign_contract_sha256: str = Field(pattern=_SHA256_PATTERN)
    executed_at_ms: int = Field(ge=1_000_000_000_000, le=INT64_MAX)
    duration_ms: int = Field(
        ge=1,
        le=BOUNDED_UI_CAMPAIGN.timeout_seconds * 1_000,
    )
    return_code: Literal[0]
    stdout_sha256: str = Field(pattern=_SHA256_PATTERN)
    stderr_sha256: str = Field(pattern=_SHA256_PATTERN)
    measurements: PreverifiedUiMeasurements
    correlation_records: tuple[SyntheticUiCorrelationRecord, ...]
    evidence_refs: tuple[_EvidenceRef, ...] = Field(
        min_length=1,
        max_length=BOUNDED_UI_CAMPAIGN.max_evidence_refs,
    )

    @model_validator(mode="after")
    def validate_exact_campaign(self) -> PreverifiedUiReceiptV5:
        if self.test_ref != _UI_TEST_REF:
            raise ValueError("preverified UI evidence does not identify the exact focused regression")
        if self.command != BOUNDED_UI_CAMPAIGN.command:
            raise ValueError("preverified UI evidence did not run the exact focused command")
        if self.campaign_contract_sha256 != UI_CAMPAIGN_CONTRACT_SHA256:
            raise ValueError("preverified UI evidence does not authenticate the campaign contract")
        if len(self.correlation_records) != self.measurements.correlation_record_count:
            raise ValueError("preverified UI correlation record count does not match measurements")
        return self

    def correlation_evidence(
        self,
        *,
        receipt_sha256: str,
    ) -> SyntheticUiCorrelationEvidence:
        """Project the authenticated receipt into the report's evidence model."""

        measurements = self.measurements
        return SyntheticUiCorrelationEvidence(
            campaign_id=BOUNDED_UI_CAMPAIGN.campaign_id,
            test_ref=self.test_ref,
            command=self.command,
            git_commit_sha=self.git_commit_sha,
            test_source_sha256=self.test_source_sha256,
            campaign_contract_sha256=self.campaign_contract_sha256,
            executed_at_ms=self.executed_at_ms,
            duration_ms=self.duration_ms,
            stdout_sha256=self.stdout_sha256,
            stderr_sha256=self.stderr_sha256,
            evidence_receipt_sha256=receipt_sha256,
            interaction_count=measurements.interaction_count,
            page_load_count=measurements.page_load_count,
            browser_reload_count=measurements.browser_reload_count,
            evidence_request_count=measurements.evidence_request_count,
            evidence_response_proof_reference_count=(measurements.evidence_response_proof_reference_count),
            native_event_source_connection_count=(measurements.native_event_source_connection_count),
            observed_revision_count=measurements.observed_revision_count,
            revision_sequence=measurements.revision_sequence,
            historical_snapshot_state=measurements.historical_snapshot_state,
            presented_action_id=measurements.presented_action_id,
            correlation_records=self.correlation_records,
            lifecycle_request_count=measurements.lifecycle_request_count,
            lifecycle_action_ids=measurements.lifecycle_request_action_ids,
        )


def load_preverified_ui_outcome(
    path: Path,
    *,
    ui_source_path: Path,
    checkout_root: Path | None = None,
    expected_git_commit_sha: str | None = None,
) -> SyntheticTestOutcome:
    """Validate and authenticate one bounded UI receipt from disk."""

    encoded = path.read_bytes()
    if len(encoded) > BOUNDED_UI_CAMPAIGN.max_receipt_bytes:
        raise ValueError(f"preverified UI evidence exceeds the {BOUNDED_UI_CAMPAIGN.max_receipt_bytes}-byte bound")
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("preverified UI evidence must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("preverified UI evidence must be a JSON object")
    if set(payload) != set(PreverifiedUiReceiptV5.model_fields):
        raise ValueError("preverified UI evidence has an unexpected shape")
    try:
        receipt = PreverifiedUiReceiptV5.model_validate_json(encoded, strict=True)
    except ValidationError as exc:
        raise ValueError(f"preverified UI evidence is invalid: {exc}") from exc

    try:
        observed_source_sha256 = hashlib.sha256(ui_source_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("preverified UI test source is unavailable") from exc
    if observed_source_sha256 != receipt.test_source_sha256:
        raise ValueError("preverified UI evidence does not authenticate the mounted test source")
    _verify_receipt_commit(
        receipt,
        ui_source_path=ui_source_path,
        checkout_root=checkout_root,
        expected_git_commit_sha=expected_git_commit_sha,
    )

    receipt_sha256 = hashlib.sha256(encoded).hexdigest()
    try:
        ui_correlation = receipt.correlation_evidence(receipt_sha256=receipt_sha256)
    except ValidationError as exc:
        raise ValueError(f"preverified UI correlation evidence is invalid: {exc}") from exc
    return SyntheticTestOutcome(
        status="PASSED",
        evidence_refs=(
            _UI_TEST_REF,
            *receipt.evidence_refs,
            f"git-commit:{receipt.git_commit_sha}",
            f"test-source-sha256:{receipt.test_source_sha256}",
            f"campaign-contract-sha256:{receipt.campaign_contract_sha256}",
            f"stdout-sha256:{receipt.stdout_sha256}",
            f"stderr-sha256:{receipt.stderr_sha256}",
            f"preverified-ui-sha256:{receipt_sha256}",
        ),
        log_refs=(str(path),),
        started_at_ms=receipt.executed_at_ms,
        completed_at_ms=receipt.executed_at_ms + receipt.duration_ms,
        ui_correlation=ui_correlation,
    )


def _verify_receipt_commit(
    receipt: PreverifiedUiReceiptV5,
    *,
    ui_source_path: Path,
    checkout_root: Path | None,
    expected_git_commit_sha: str | None,
) -> None:
    """Bind the receipt commit to the tested source and a clean checkout."""

    if checkout_root is None:
        if expected_git_commit_sha != receipt.git_commit_sha:
            raise ValueError("preverified UI evidence commit does not match the qualification image")
        return
    try:
        relative_source = ui_source_path.resolve(strict=True).relative_to(checkout_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("preverified UI source is outside the tested checkout") from exc
    relative_text = relative_source.as_posix()
    commands = (
        ("merge-base", "--is-ancestor", receipt.git_commit_sha, "HEAD"),
        ("diff", "--quiet", "HEAD", "--", relative_text),
        ("diff", "--cached", "--quiet", "HEAD", "--", relative_text),
    )
    try:
        for command in commands:
            subprocess.run(
                ["git", "-C", str(checkout_root), *command],
                check=True,
                capture_output=True,
            )
        committed_source = subprocess.run(
            [
                "git",
                "-C",
                str(checkout_root),
                "show",
                f"{receipt.git_commit_sha}:{relative_text}",
            ],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "preverified UI evidence commit is unavailable, unrelated, or the test source is dirty"
        ) from exc
    if hashlib.sha256(committed_source).hexdigest() != receipt.test_source_sha256:
        raise ValueError("preverified UI evidence commit does not contain the authenticated test source")


__all__ = [
    "BOUNDED_UI_CAMPAIGN",
    "BoundedUiCampaignContract",
    "PreverifiedUiMeasurements",
    "PreverifiedUiReceiptV5",
    "load_preverified_ui_outcome",
]
