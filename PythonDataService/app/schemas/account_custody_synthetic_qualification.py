"""Synthetic-only evidence contracts for the account-custody rehearsal."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.sqlite.qualification_ui_campaign_contract import (
    BOUNDED_UI_CAMPAIGN,
)
from app.models.account_custody_synthetic import (
    SyntheticAbortCause,
    SyntheticScenarioId,
    synthetic_abort_cause_for_scenario,
)
from app.schemas.account_custody_qualification import (
    INT64_MAX,
    PaperQualificationStatus,
    account_custody_qualification_payload_sha256,
)

SyntheticAbortClass = Literal[
    "NONE",
    "FEED_ABORT",
    "CUSTODY_ABORT",
    "INFRA_ABORT",
]
SyntheticRehearsalStatus = Literal[
    "REHEARSED_PENDING_LIVE_CONFIRMATION",
    "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION",
    "ABORTED",
]
_CUSTODY_ABORT_CAUSES = frozenset(
    {
        SyntheticAbortCause.DUPLICATE_ECONOMIC_INTENT,
        SyntheticAbortCause.BROKER_MUTATION_WITHOUT_FINALIZED_INTENT,
        SyntheticAbortCause.MIXED_AUTHORITY_WRITERS,
        SyntheticAbortCause.UNEXPLAINED_CUSTODY_DRIFT,
        SyntheticAbortCause.CUSTODY_STATE_REGRESSION,
        SyntheticAbortCause.FABRICATED_TERMINAL_RESULT,
        SyntheticAbortCause.STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE,
        SyntheticAbortCause.CORRUPT_MISSING_OR_SUBSTITUTED_AUTHORITY,
        SyntheticAbortCause.RECOVERY_HASH_IDENTITY_FAILURE,
        SyntheticAbortCause.UI_EXECUTION_POLICY_MISMATCH,
    }
)


def synthetic_abort_class_for_cause(
    cause: SyntheticAbortCause,
) -> SyntheticAbortClass:
    """Classify one typed cause without inferring from exception text."""

    if cause == SyntheticAbortCause.NONE:
        return "NONE"
    if cause == SyntheticAbortCause.FEED_REPLAY_FAILURE:
        return "FEED_ABORT"
    if cause in _CUSTODY_ABORT_CAUSES:
        return "CUSTODY_ABORT"
    if cause == SyntheticAbortCause.INFRASTRUCTURE_OR_TOOLING_FAILURE:
        return "INFRA_ABORT"
    raise ValueError(f"unmapped synthetic abort cause: {cause}")


class SyntheticBrokerOrderProof(BaseModel):
    """One typed simulated broker order captured in a rehearsal snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str = Field(min_length=1, max_length=256)
    broker_order_id: str = Field(min_length=1, max_length=256)
    side: str = Field(min_length=1, max_length=32)
    quantity: float | None
    status: str = Field(min_length=1, max_length=64)
    filled_quantity: float


class SyntheticBrokerProof(BaseModel):
    """Simulated broker inventory evidence before or after one rehearsal row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proof_kind: Literal["SIMULATED_BROKER_SNAPSHOT", "NOT_APPLICABLE"]
    proof_reference: str = Field(min_length=1, max_length=512)
    observed_at_ms: int = Field(ge=0, le=INT64_MAX)
    positions: dict[str, str] = Field(default_factory=dict)
    open_order_ids: tuple[str, ...] = ()
    orders: tuple[SyntheticBrokerOrderProof, ...] = ()
    submit_calls: tuple[str, ...] = ()
    cancel_calls: tuple[str, ...] = ()
    lookup_calls: tuple[str, ...] = ()


class SyntheticEvidenceWorksheet(BaseModel):
    """The complete #1409 worksheet, including explicit empty identity sets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_scope: Literal[
        "DURABLE_CLERK_TRANSITION",
        "FEED_REGRESSION_HARNESS",
        "UI_CORRELATION_RECEIPT",
        "RECOVERY_CEREMONY",
    ]
    authority_participation: Literal["READ_WRITE", "CONTEXT_ONLY"]
    authority_generation: int = Field(ge=1)
    db_identity_token: str = Field(min_length=1, max_length=256)
    control_revision: int = Field(ge=0)
    command_ids: tuple[str, ...] = ()
    effect_operation_ids: tuple[str, ...] = ()
    order_refs: tuple[str, ...] = ()
    broker_order_ids: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = Field(min_length=1)
    source_event_at_ms: int | None = Field(default=None, ge=0, le=INT64_MAX)
    clerk_observed_at_ms: int | None = Field(default=None, ge=0, le=INT64_MAX)
    recorded_at_ms: int | None = Field(default=None, ge=0, le=INT64_MAX)
    expected_state: str = Field(min_length=1, max_length=1_024)
    observed_state: str = Field(min_length=1, max_length=1_024)
    operator_action: str = Field(min_length=1, max_length=640)
    warning_error_log_refs: tuple[str, ...] = Field(min_length=1)
    broker_before: SyntheticBrokerProof
    broker_after: SyntheticBrokerProof

    @model_validator(mode="after")
    def validate_temporal_order(self) -> SyntheticEvidenceWorksheet:
        clocks = (
            self.source_event_at_ms,
            self.clerk_observed_at_ms,
            self.recorded_at_ms,
        )
        if self.evidence_scope == "DURABLE_CLERK_TRANSITION" and (
            self.clerk_observed_at_ms is None or self.recorded_at_ms is None
        ):
            raise ValueError("a durable Clerk worksheet requires observation and record clocks")
        observed = tuple(value for value in clocks if value is not None)
        if observed != tuple(sorted(observed)):
            raise ValueError("worksheet clocks must order source <= Clerk observation <= durable record")
        return self


class SyntheticScenarioLimitation(BaseModel):
    """A typed boundary that prevents a scenario from claiming full rehearsal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=2_048)
    fault_kind: str | None = Field(default=None, max_length=128)


class SyntheticScenarioResult(BaseModel):
    """One seam-induced rehearsal result; never a live qualification result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: SyntheticScenarioId
    name: str = Field(min_length=1, max_length=160)
    status: SyntheticRehearsalStatus
    abort_class: SyntheticAbortClass
    abort_cause: SyntheticAbortCause = SyntheticAbortCause.NONE
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    worksheet: SyntheticEvidenceWorksheet
    limitations: tuple[SyntheticScenarioLimitation, ...] = ()
    fault_seam_required: bool = False
    fault_seam_exercised: bool = False
    failure_detail: str | None = Field(default=None, max_length=2_048)

    @model_validator(mode="after")
    def validate_status(self) -> SyntheticScenarioResult:
        if self.fault_seam_exercised and not self.fault_seam_required:
            raise ValueError("a scenario cannot exercise a seam it does not require")
        if (
            self.fault_seam_required
            and not self.fault_seam_exercised
            and not self.limitations
            and self.status != "ABORTED"
        ):
            raise ValueError("an unexercised required seam needs a typed limitation")
        if self.status == "REHEARSED_PENDING_LIVE_CONFIRMATION":
            if (
                self.abort_class != "NONE"
                or self.abort_cause != SyntheticAbortCause.NONE
                or self.failure_detail is not None
                or self.limitations
            ):
                raise ValueError("a rehearsed row cannot carry an abort or failure detail")
        elif self.status == "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION":
            if (
                self.abort_class != "NONE"
                or self.abort_cause != SyntheticAbortCause.NONE
                or self.failure_detail is not None
                or not self.limitations
            ):
                raise ValueError("a limited rehearsal row requires typed limitations only")
        elif self.status == "ABORTED":
            if (
                self.abort_class == "NONE"
                or self.abort_cause == SyntheticAbortCause.NONE
                or self.failure_detail is None
            ):
                raise ValueError("an aborted row requires a classified abort and failure detail")
            expected_cause = synthetic_abort_cause_for_scenario(self.scenario_id)
            if self.abort_cause not in {
                expected_cause,
                SyntheticAbortCause.INFRASTRUCTURE_OR_TOOLING_FAILURE,
            }:
                raise ValueError(
                    "scenario abort cause must match its predeclared invariant "
                    "or identify infrastructure/tooling failure"
                )
            expected_abort = synthetic_abort_class_for_cause(self.abort_cause)
            if self.abort_class != expected_abort:
                raise ValueError("scenario abort class must match feed, custody-finding, or infrastructure scope")
        return self


class SyntheticPolygonReplayEvidence(BaseModel):
    """Pinned fixture and exact 5-second-to-minute replay receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fixture_ref: str = Field(min_length=1, max_length=512)
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_bar_count: int = Field(ge=1)
    replayed_minute_count: int = Field(ge=1)
    synthesized_5s_count: int = Field(ge=1)
    emitted_minute_count: int = Field(ge=1)
    decision_count: int = Field(ge=1)
    decision_outcomes: tuple[Literal["HOLD", "ENTER", "EXIT"], ...] = Field(min_length=1)
    timestamp_contract: Literal["int64_ms_utc"] = "int64_ms_utc"
    aggregation_path: Literal["stream_minute_bars/aggregate_realtime_bar/IbkrMarketDataFeed"] = (
        "stream_minute_bars/aggregate_realtime_bar/IbkrMarketDataFeed"
    )
    decision_consumer_path: Literal["strategy_evaluations/DeploymentValidationDecisionKernel"] = (
        "strategy_evaluations/DeploymentValidationDecisionKernel"
    )

    @model_validator(mode="after")
    def validate_decision_count(self) -> SyntheticPolygonReplayEvidence:
        if self.decision_count != self.emitted_minute_count:
            raise ValueError("every emitted replay minute must reach the decision consumer")
        if len(self.decision_outcomes) != self.decision_count:
            raise ValueError("decision outcomes must match the measured decision count")
        return self


class SyntheticProtectedGenerationEvidence(BaseModel):
    """Proof that rehearsal storage was isolated before any authority opened."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifacts_root: str = Field(min_length=1, max_length=1_024)
    account_id: str = Field(min_length=1, max_length=64)
    authority_generation: int = Field(ge=1)
    db_identity_token: str = Field(min_length=1, max_length=256)
    filesystem_type: Literal["xfs"]
    wal_storage_guard_checked: Literal[True]
    production_account_id: str = Field(min_length=1, max_length=64)
    production_authority_root: str = Field(min_length=1, max_length=1_024)
    production_mount_access: Literal["READ_ONLY"]
    qualification_mount_access: Literal["READ_WRITE"]
    production_mount_id: int = Field(ge=1)
    qualification_mount_id: int = Field(ge=1)
    mount_identity_separated: Literal[True]
    production_generation_opened_read_write: Literal[False]
    diagnostic_read_boundary: Literal["ONE_SHOT_SERVICE_CONTAINER"]

    @model_validator(mode="after")
    def validate_mount_separation(self) -> SyntheticProtectedGenerationEvidence:
        if self.production_mount_id == self.qualification_mount_id:
            raise ValueError("production and qualification authority mounts must differ")
        return self


class SyntheticRecoveryEvidence(BaseModel):
    """Backup, restore, mirror rebuild, integrity, and hash-head receipts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_manifest_ref: str = Field(min_length=1, max_length=1_024)
    backup_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    restore_receipt_ref: str = Field(min_length=1, max_length=1_024)
    rebuild_receipt_ref: str = Field(min_length=1, max_length=1_024)
    integrity_check: Literal["ok"] = "ok"
    hash_head_before: str = Field(min_length=1, max_length=256)
    hash_head_after_restore: str = Field(min_length=1, max_length=256)
    hash_head_after_rebuild: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_heads(self) -> SyntheticRecoveryEvidence:
        if (
            len(
                {
                    self.hash_head_before,
                    self.hash_head_after_restore,
                    self.hash_head_after_rebuild,
                }
            )
            != 1
        ):
            raise ValueError("restore and rebuild must reproduce the exact authority hash head")
        return self


class SyntheticUiCorrelationRecord(BaseModel):
    """One browser event-to-evidence correlation with explicit action absence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    page_load_index: int = Field(ge=0, le=32)
    event_index: int = Field(ge=0, le=128)
    browser_epoch: str = Field(pattern=r"^browser-reload-[0-9]+$")
    revision: int = Field(ge=0)
    historical_snapshot_state: Literal["OFF_DUTY_STOPPED_FLAT_POST_RESTART"] = "OFF_DUTY_STOPPED_FLAT_POST_RESTART"
    presented_action_id: Literal["resume"] = "resume"
    click_target: Literal["BROKER_ACK_RAW_EVIDENCE"] = "BROKER_ACK_RAW_EVIDENCE"
    evidence_request_method: Literal["GET"] = "GET"
    evidence_request_path: str = Field(min_length=1, max_length=2_048)
    operation_reference: str = Field(min_length=1, max_length=1_024)
    proof_reference: str = Field(min_length=1, max_length=1_024)
    dispatched_lifecycle_action_id: None = None
    durable_lifecycle_receipt_id: None = None

    @model_validator(mode="after")
    def validate_evidence_request(self) -> SyntheticUiCorrelationRecord:
        if not self.evidence_request_path.startswith("/api/brokers/alpaca/"):
            raise ValueError("UI correlation must record the Alpaca V2 evidence path")
        if "/evidence?" not in self.evidence_request_path:
            raise ValueError("UI correlation must identify an evidence GET request")
        return self


class SyntheticUiCorrelationEvidence(BaseModel):
    """Measured #1413 browser-reload campaign and authenticated test receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    campaign_id: Literal["frontend-browser-network-correlation-1413"]
    test_ref: str = Field(min_length=1, max_length=1_024)
    command: tuple[str, ...] = Field(min_length=1, max_length=16)
    git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    test_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    campaign_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executed_at_ms: int = Field(ge=0, le=INT64_MAX)
    duration_ms: int = Field(
        ge=1,
        le=BOUNDED_UI_CAMPAIGN.timeout_seconds * 1_000,
    )
    stdout_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interaction_count: int = Field(ge=1)
    page_load_count: int = Field(ge=2)
    browser_reload_count: int = Field(ge=1)
    evidence_request_count: int = Field(ge=1)
    evidence_response_proof_reference_count: int = Field(ge=1)
    native_event_source_connection_count: int = Field(ge=1)
    observed_revision_count: int = Field(ge=1)
    revision_sequence: tuple[int, ...] = Field(min_length=1)
    historical_snapshot_state: Literal["OFF_DUTY_STOPPED_FLAT_POST_RESTART"] = "OFF_DUTY_STOPPED_FLAT_POST_RESTART"
    presented_action_id: Literal["resume"] = "resume"
    correlation_records: tuple[SyntheticUiCorrelationRecord, ...] = Field(
        min_length=1,
        max_length=4_096,
    )
    lifecycle_request_count: Literal[0] = 0
    lifecycle_action_ids: tuple[str, ...] = ()
    browser_reload_coverage: Literal["PLAYWRIGHT_NATIVE_EVENT_SOURCE_PAGE_RELOAD_CAMPAIGN"] = (
        "PLAYWRIGHT_NATIVE_EVENT_SOURCE_PAGE_RELOAD_CAMPAIGN"
    )
    durable_lifecycle_receipt_ids: tuple[str, ...] = Field(
        default=(),
        max_length=0,
    )
    disposition: Literal["HISTORICAL_TRIGGER_NOT_REPRODUCED_BROWSER_CAMPAIGN"] = (
        "HISTORICAL_TRIGGER_NOT_REPRODUCED_BROWSER_CAMPAIGN"
    )

    @model_validator(mode="after")
    def validate_zero_action_correlation(self) -> SyntheticUiCorrelationEvidence:
        if self.lifecycle_action_ids:
            raise ValueError("a read-only UI campaign cannot contain lifecycle action ids")
        if self.browser_reload_count != self.page_load_count - 1:
            raise ValueError("every page load after the first must be an actual browser reload")
        expected_observations = self.page_load_count * len(self.revision_sequence)
        measured_counts = {
            self.interaction_count,
            self.evidence_request_count,
            self.evidence_response_proof_reference_count,
            self.native_event_source_connection_count,
            self.observed_revision_count,
        }
        if measured_counts != {expected_observations}:
            raise ValueError(
                "every browser epoch and revision must correlate one SSE event, interaction, request, and proof reference"
            )
        if len(self.correlation_records) != expected_observations:
            raise ValueError("UI correlation ledger must contain every measured interaction")
        expected_coordinates = tuple(
            (page_load_index, event_index, revision)
            for page_load_index in range(self.page_load_count)
            for event_index, revision in enumerate(self.revision_sequence)
        )
        observed_coordinates = tuple(
            (record.page_load_index, record.event_index, record.revision) for record in self.correlation_records
        )
        if observed_coordinates != expected_coordinates:
            raise ValueError("UI correlation ledger must preserve browser/event/revision order")
        if any(
            record.browser_epoch != f"browser-reload-{record.page_load_index}" for record in self.correlation_records
        ):
            raise ValueError("UI correlation ledger browser epochs must match page loads")
        if any(record.operation_reference != record.proof_reference for record in self.correlation_records):
            raise ValueError("every UI evidence request must correlate one durable operation/proof reference")
        return self


class SyntheticCustodyRehearsalReport(BaseModel):
    """Content-addressed pre-live evidence that cannot represent a live pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    generated_at_ms: int = Field(ge=0, le=INT64_MAX)
    label: Literal["PRE_LIVE_REHEARSAL_SYNTHETIC"] = "PRE_LIVE_REHEARSAL_SYNTHETIC"
    live_environment_status: PaperQualificationStatus = "NOT_RUN"
    adr_0035_status: Literal["PROPOSED"] = "PROPOSED"
    overall_status: SyntheticRehearsalStatus
    abort_class: SyntheticAbortClass
    protected_generation: SyntheticProtectedGenerationEvidence
    polygon_replay: SyntheticPolygonReplayEvidence
    recovery: SyntheticRecoveryEvidence
    ui_correlation: SyntheticUiCorrelationEvidence | None
    scenarios: tuple[SyntheticScenarioResult, ...] = Field(min_length=13, max_length=13)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report(self) -> SyntheticCustodyRehearsalReport:
        scenario_ids = tuple(row.scenario_id for row in self.scenarios)
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("synthetic report scenario IDs must be unique")
        if set(scenario_ids) != set(SyntheticScenarioId):
            raise ValueError("synthetic report must contain every scenario")
        aborted = tuple(row for row in self.scenarios if row.status == "ABORTED")
        limited = any(row.status == "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION" for row in self.scenarios)
        expected_status = (
            "ABORTED"
            if aborted
            else "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION"
            if limited
            else "REHEARSED_PENDING_LIVE_CONFIRMATION"
        )
        if self.overall_status != expected_status:
            raise ValueError("overall status must match scenario outcomes")
        expected_abort = (
            "CUSTODY_ABORT"
            if any(row.abort_class == "CUSTODY_ABORT" for row in aborted)
            else "FEED_ABORT"
            if any(row.abort_class == "FEED_ABORT" for row in aborted)
            else "INFRA_ABORT"
            if aborted
            else "NONE"
        )
        if self.abort_class != expected_abort:
            raise ValueError("overall abort class must preserve custody-over-feed precedence")
        ui_row = next(
            row for row in self.scenarios if row.scenario_id == SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY
        )
        if ui_row.status != "ABORTED" and self.ui_correlation is None:
            raise ValueError("a completed UI scenario requires measured correlation evidence")
        payload = self.model_dump(mode="json", exclude={"report_sha256"})
        if self.report_sha256 != account_custody_qualification_payload_sha256(payload):
            raise ValueError("synthetic report SHA-256 does not match its semantic payload")
        return self


class SyntheticHarnessAbortReport(BaseModel):
    """Content-addressed fail-closed receipt when a core phase cannot complete."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    generated_at_ms: int = Field(ge=0, le=INT64_MAX)
    label: Literal["PRE_LIVE_REHEARSAL_SYNTHETIC_ABORT"] = "PRE_LIVE_REHEARSAL_SYNTHETIC_ABORT"
    live_environment_status: PaperQualificationStatus = "NOT_RUN"
    adr_0035_status: Literal["PROPOSED"] = "PROPOSED"
    overall_status: Literal["ABORTED"] = "ABORTED"
    abort_class: Literal["FEED_ABORT", "CUSTODY_ABORT", "INFRA_ABORT"]
    abort_cause: SyntheticAbortCause
    phase: Literal[
        "POLYGON_REPLAY",
        "PROTECTED_STORAGE_OR_RECOVERY_TOOLING",
        "RECOVERY_INTEGRITY",
        "INTEGRATED_CUSTODY_DRILLS",
    ]
    failure_type: str = Field(min_length=1, max_length=256)
    failure_detail: str = Field(min_length=1, max_length=2_048)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_hash(self) -> SyntheticHarnessAbortReport:
        expected_cause = (
            SyntheticAbortCause.FEED_REPLAY_FAILURE
            if self.phase == "POLYGON_REPLAY"
            else SyntheticAbortCause.RECOVERY_HASH_IDENTITY_FAILURE
            if self.phase == "RECOVERY_INTEGRITY"
            else SyntheticAbortCause.INFRASTRUCTURE_OR_TOOLING_FAILURE
        )
        if self.abort_cause != expected_cause:
            raise ValueError("core phase abort cause must match its typed phase")
        expected_abort = synthetic_abort_class_for_cause(self.abort_cause)
        if self.abort_class != expected_abort:
            raise ValueError("core phase abort class must match feed or infrastructure scope")
        payload = self.model_dump(mode="json", exclude={"report_sha256"})
        if self.report_sha256 != account_custody_qualification_payload_sha256(payload):
            raise ValueError("synthetic abort report SHA-256 does not match its semantic payload")
        return self
