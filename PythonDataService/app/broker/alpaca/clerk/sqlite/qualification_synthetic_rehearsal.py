"""Synthetic custody rehearsal orchestration, result reduction, and reporting."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from app.broker.alpaca.clerk.sqlite import qualification_storage_recovery as _storage
from app.broker.alpaca.clerk.sqlite.qualification_polygon_replay import (
    run_polygon_replay,
)
from app.broker.alpaca.clerk.sqlite.qualification_storage_recovery import (
    ProtectedGenerationRun,
    QualificationStorageBoundary,
    SyntheticRecoveryCustodyError,
)
from app.schemas.account_custody_qualification import (
    account_custody_qualification_payload_sha256,
)
from app.schemas.account_custody_synthetic_qualification import (
    SyntheticBrokerOrderProof,
    SyntheticBrokerProof,
    SyntheticCustodyRehearsalReport,
    SyntheticEvidenceWorksheet,
    SyntheticPolygonReplayEvidence,
    SyntheticScenarioLimitation,
    SyntheticScenarioResult,
    SyntheticUiCorrelationEvidence,
    synthetic_abort_class_for_cause,
)
from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_REHEARSAL_SCENARIOS,
    SyntheticAbortCause,
    SyntheticScenarioCategory,
    SyntheticScenarioId,
)
from app.services.alpaca_sqlite_synthetic_drills import (
    SimulatedBrokerProof,
    SyntheticScenarioObservation,
    SyntheticScenarioStatus,
    run_synthetic_custody_drills,
)


class SyntheticTestFailureOrigin(StrEnum):
    """Whether a failed regression observed the invariant or failed to execute it."""

    OBSERVED_INVARIANT_FAILURE = "OBSERVED_INVARIANT_FAILURE"
    INFRASTRUCTURE_OR_TOOLING_FAILURE = "INFRASTRUCTURE_OR_TOOLING_FAILURE"


@dataclass(frozen=True)
class SyntheticTestOutcome:
    """Observed result for one scenario's selected regression set."""

    status: Literal["PASSED", "FAILED", "PENDING_CORE_VALIDATION"]
    evidence_refs: tuple[str, ...]
    log_refs: tuple[str, ...] = ("none",)
    failure_detail: str | None = None
    failure_origin: SyntheticTestFailureOrigin | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    ui_correlation: SyntheticUiCorrelationEvidence | None = None

    def __post_init__(self) -> None:
        if self.status == "FAILED" and self.failure_detail is None:
            raise ValueError("a failed synthetic outcome requires failure detail")
        if self.status != "FAILED" and self.failure_detail is not None:
            raise ValueError("only a failed synthetic outcome may carry failure detail")
        if self.status == "FAILED" and self.failure_origin is None:
            raise ValueError("a failed synthetic outcome requires a failure origin")
        if self.status != "FAILED" and self.failure_origin is not None:
            raise ValueError("only a failed synthetic outcome may carry a failure origin")
        if self.failure_origin is not None and not isinstance(self.failure_origin, SyntheticTestFailureOrigin):
            raise TypeError("synthetic outcome failure origin must be typed")
        if (self.started_at_ms is None) != (self.completed_at_ms is None):
            raise ValueError("synthetic outcome timings must be supplied together")
        if (
            self.started_at_ms is not None
            and self.completed_at_ms is not None
            and self.started_at_ms > self.completed_at_ms
        ):
            raise ValueError("synthetic outcome timings must be ordered")

    @property
    def passed(self) -> bool:
        """Whether the selected regression completed successfully."""

        return self.status == "PASSED"


class SyntheticRehearsalPhaseError(RuntimeError):
    """A classified core-phase failure that must emit an abort receipt."""

    def __init__(
        self,
        *,
        abort_cause: SyntheticAbortCause,
        phase: Literal[
            "POLYGON_REPLAY",
            "PROTECTED_STORAGE_OR_RECOVERY_TOOLING",
            "RECOVERY_INTEGRITY",
            "INTEGRATED_CUSTODY_DRILLS",
        ],
        cause: Exception,
    ) -> None:
        super().__init__(f"{phase}: {type(cause).__name__}: {cause}")
        self.abort_cause = abort_cause
        self.abort_class = synthetic_abort_class_for_cause(abort_cause)
        self.phase = phase
        self.cause = cause


def run_synthetic_custody_rehearsal(
    *,
    artifacts_root: Path,
    production_artifacts_root: Path,
    fixture_directory: Path,
    production_account_id: str,
    test_outcomes: dict[str, SyntheticTestOutcome],
    generated_at_ms: int | None = None,
    polygon_replay_runner: Callable[[Path], SyntheticPolygonReplayEvidence] | None = None,
    storage_boundary_resolver: Callable[[Path, Path, str], QualificationStorageBoundary] | None = None,
) -> SyntheticCustodyRehearsalReport:
    """Build the content-addressed #1415 pre-live report on isolated authority."""

    generated = generated_at_ms or time.time_ns() // 1_000_000
    try:
        polygon = (polygon_replay_runner or run_polygon_replay)(fixture_directory)
    except Exception as exc:
        raise SyntheticRehearsalPhaseError(
            abort_cause=SyntheticAbortCause.FEED_REPLAY_FAILURE,
            phase="POLYGON_REPLAY",
            cause=exc,
        ) from exc
    try:
        protected = _storage.run_protected_generation(
            artifacts_root=artifacts_root,
            production_artifacts_root=production_artifacts_root,
            production_account_id=production_account_id,
            generated_at_ms=generated,
            storage_boundary_resolver=storage_boundary_resolver,
        )
    except SyntheticRecoveryCustodyError as exc:
        raise SyntheticRehearsalPhaseError(
            abort_cause=SyntheticAbortCause.RECOVERY_HASH_IDENTITY_FAILURE,
            phase="RECOVERY_INTEGRITY",
            cause=exc,
        ) from exc
    except Exception as exc:
        raise SyntheticRehearsalPhaseError(
            abort_cause=SyntheticAbortCause.INFRASTRUCTURE_OR_TOOLING_FAILURE,
            phase="PROTECTED_STORAGE_OR_RECOVERY_TOOLING",
            cause=exc,
        ) from exc
    try:
        custody_drills = asyncio.run(run_synthetic_custody_drills(artifacts_root / "integrated-drills"))
    except Exception as exc:
        raise SyntheticRehearsalPhaseError(
            abort_cause=SyntheticAbortCause.INFRASTRUCTURE_OR_TOOLING_FAILURE,
            phase="INTEGRATED_CUSTODY_DRILLS",
            cause=exc,
        ) from exc
    custody_by_id = {observation.scenario_id: observation for observation in custody_drills.observations}
    results: list[SyntheticScenarioResult] = []
    ui_correlation: SyntheticUiCorrelationEvidence | None = None
    for scenario in SYNTHETIC_REHEARSAL_SCENARIOS:
        outcome = test_outcomes.get(scenario.scenario_id)
        if outcome is None:
            raise ValueError(f"missing test outcome for {scenario.scenario_id}")
        if outcome.status == "PENDING_CORE_VALIDATION":
            if scenario.category is not SyntheticScenarioCategory.RECOVERY:
                raise ValueError("only the recovery ceremony may await core validation")
            outcome = SyntheticTestOutcome(
                status="PASSED",
                evidence_refs=outcome.evidence_refs,
                log_refs=(
                    *outcome.log_refs,
                    protected.recovery.restore_receipt_ref,
                    protected.recovery.rebuild_receipt_ref,
                ),
            )
        observation = custody_by_id.get(scenario.scenario_id)
        limitations: tuple[SyntheticScenarioLimitation, ...] = ()
        failure_detail = outcome.failure_detail
        failure_origin = outcome.failure_origin
        if scenario.category is SyntheticScenarioCategory.CUSTODY:
            if observation is None:
                failure_detail = "integrated Clerk drill did not produce this scenario"
                failure_origin = SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE
            elif observation.status == SyntheticScenarioStatus.FAILED:
                failure_detail = observation.assertion
                failure_origin = SyntheticTestFailureOrigin.OBSERVED_INVARIANT_FAILURE
            else:
                limitations = tuple(
                    SyntheticScenarioLimitation(
                        code=limitation.code.value,
                        fault_kind=limitation.fault_kind,
                        detail=limitation.detail,
                    )
                    for limitation in observation.limitations
                )
                if observation.evidence.source_event_at_ms is None:
                    limitations = (
                        *limitations,
                        SyntheticScenarioLimitation(
                            code="SOURCE_EVENT_CLOCK_UNAVAILABLE",
                            detail=(
                                "The durable transition exposes Clerk-observation and "
                                "record clocks but has no source-event timestamp."
                            ),
                        ),
                    )
        elif scenario.category is SyntheticScenarioCategory.UI:
            ui_correlation = outcome.ui_correlation
            if outcome.passed and ui_correlation is None:
                failure_detail = "UI regression passed without an authenticated measured correlation receipt"
                failure_origin = SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE
        if scenario.category in {
            SyntheticScenarioCategory.FEED,
            SyntheticScenarioCategory.UI,
        }:
            limitations = (
                *limitations,
                SyntheticScenarioLimitation(
                    code="CLERK_OBSERVATION_CLOCK_NOT_APPLICABLE",
                    detail=(
                        "This feed/UI regression does not mutate or pass through the Clerk; "
                        "its Clerk-observation clock is therefore explicitly not applicable."
                    ),
                ),
            )
        passed = outcome.passed and failure_detail is None
        if passed:
            abort_cause = SyntheticAbortCause.NONE
        elif failure_origin == SyntheticTestFailureOrigin.INFRASTRUCTURE_OR_TOOLING_FAILURE:
            abort_cause = SyntheticAbortCause.INFRASTRUCTURE_OR_TOOLING_FAILURE
        else:
            abort_cause = scenario.abort_cause
        abort_class = synthetic_abort_class_for_cause(abort_cause)
        status = (
            "ABORTED"
            if not passed
            else "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION"
            if limitations
            else "REHEARSED_PENDING_LIVE_CONFIRMATION"
        )
        worksheet = (
            _integrated_custody_worksheet(observation)
            if observation is not None
            else _non_custody_worksheet(
                scenario_id=scenario.scenario_id,
                category=scenario.category,
                expected_state=scenario.expected_state,
                outcome=outcome,
                protected=protected,
                generated_at_ms=generated,
            )
        )
        evidence_refs = tuple(
            dict.fromkeys(
                (
                    *outcome.evidence_refs,
                    *((observation.evidence.durable_transition_ref,) if observation is not None else ()),
                )
            )
        )
        results.append(
            SyntheticScenarioResult(
                scenario_id=scenario.scenario_id,
                name=scenario.name,
                status=status,
                abort_class=abort_class,
                abort_cause=abort_cause,
                evidence_refs=evidence_refs,
                failure_detail=None if passed else (failure_detail or "scenario failed")[:2_048],
                worksheet=worksheet,
                limitations=limitations,
                fault_seam_required=(observation.fault_seam_required if observation is not None else False),
                fault_seam_exercised=(observation.fault_seam_exercised if observation is not None else False),
            )
        )
    aborted = tuple(result for result in results if result.status == "ABORTED")
    overall_abort = (
        "CUSTODY_ABORT"
        if any(result.abort_class == "CUSTODY_ABORT" for result in aborted)
        else "FEED_ABORT"
        if any(result.abort_class == "FEED_ABORT" for result in aborted)
        else "INFRA_ABORT"
        if aborted
        else "NONE"
    )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_ms": generated,
        "label": "PRE_LIVE_REHEARSAL_SYNTHETIC",
        "live_environment_status": "NOT_RUN",
        "adr_0035_status": "PROPOSED",
        "overall_status": (
            "ABORTED"
            if aborted
            else "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION"
            if any(result.limitations for result in results)
            else "REHEARSED_PENDING_LIVE_CONFIRMATION"
        ),
        "abort_class": overall_abort,
        "protected_generation": protected.evidence.model_dump(mode="json"),
        "polygon_replay": polygon.model_dump(mode="json"),
        "recovery": protected.recovery.model_dump(mode="json"),
        "ui_correlation": (ui_correlation.model_dump(mode="json") if ui_correlation is not None else None),
        "scenarios": [result.model_dump(mode="json") for result in results],
    }
    payload["report_sha256"] = account_custody_qualification_payload_sha256(payload)
    return SyntheticCustodyRehearsalReport.model_validate(payload)


def _integrated_custody_worksheet(
    observation: SyntheticScenarioObservation,
) -> SyntheticEvidenceWorksheet:
    """Map one observed Clerk transition without inventing identities or clocks."""

    evidence = observation.evidence

    def broker_proof(
        phase: str,
        proof: SimulatedBrokerProof,
    ) -> SyntheticBrokerProof:
        observed_at_ms = (
            evidence.recorded_at_ms
            if phase == "after"
            else evidence.source_event_at_ms or evidence.clerk_observed_at_ms
        )
        return SyntheticBrokerProof(
            proof_kind="SIMULATED_BROKER_SNAPSHOT",
            proof_reference=f"{evidence.durable_transition_ref}:broker-{phase}",
            observed_at_ms=observed_at_ms,
            positions={symbol: str(quantity) for symbol, quantity in proof.positions},
            open_order_ids=tuple(
                order.broker_order_id
                for order in proof.orders
                if order.status in {"accepted", "new", "partially_filled"}
            ),
            orders=tuple(
                SyntheticBrokerOrderProof.model_validate(order, from_attributes=True) for order in proof.orders
            ),
            submit_calls=proof.submit_calls,
            cancel_calls=proof.cancel_calls,
            lookup_calls=proof.lookup_calls,
        )

    receipt_ids = tuple(value for value in (evidence.receipt_id, evidence.durable_transition_ref) if value is not None)
    return SyntheticEvidenceWorksheet(
        evidence_scope="DURABLE_CLERK_TRANSITION",
        authority_participation="READ_WRITE",
        authority_generation=evidence.authority_generation,
        db_identity_token=evidence.db_identity_token,
        control_revision=evidence.revision_after,
        command_ids=evidence.command_ids,
        effect_operation_ids=evidence.effect_operation_ids,
        order_refs=evidence.order_refs,
        broker_order_ids=evidence.broker_order_ids,
        receipt_ids=receipt_ids,
        source_event_at_ms=evidence.source_event_at_ms,
        clerk_observed_at_ms=evidence.clerk_observed_at_ms,
        recorded_at_ms=evidence.recorded_at_ms,
        expected_state=evidence.expected,
        observed_state=evidence.observed,
        operator_action=evidence.action,
        warning_error_log_refs=evidence.log_refs,
        broker_before=broker_proof("before", evidence.broker_before),
        broker_after=broker_proof("after", evidence.broker_after),
    )


def _non_custody_worksheet(
    *,
    scenario_id: SyntheticScenarioId,
    category: SyntheticScenarioCategory,
    expected_state: str,
    outcome: SyntheticTestOutcome,
    protected: ProtectedGenerationRun,
    generated_at_ms: int,
) -> SyntheticEvidenceWorksheet:
    """Record actual harness/recovery receipts and explicit non-applicability."""

    started_at_ms = outcome.started_at_ms
    clerk_observed_at_ms: int | None = None
    completed_at_ms = outcome.completed_at_ms
    if category is SyntheticScenarioCategory.RECOVERY:
        scope = "RECOVERY_CEREMONY"
        participation = "READ_WRITE"
        command_ids = protected.command_ids
        receipt_ids = (*protected.receipt_ids, *outcome.evidence_refs)
        started_at_ms = protected.recovery_source_event_at_ms
        clerk_observed_at_ms = protected.recovery_clerk_observed_at_ms
        completed_at_ms = protected.recovery_recorded_at_ms
        observed_state = "Verified backup, restore, rebuild, integrity, and hash-head equality."
    else:
        scope = "UI_CORRELATION_RECEIPT" if category is SyntheticScenarioCategory.UI else "FEED_REGRESSION_HARNESS"
        participation = "CONTEXT_ONLY"
        command_ids = ()
        receipt_ids = outcome.evidence_refs
        observed_state = (
            "The bounded regression and its authenticated evidence completed."
            if outcome.passed
            else outcome.failure_detail or "The bounded regression failed."
        )
        observed_state = observed_state[:1_024]
    observed_at_ms = completed_at_ms or generated_at_ms
    broker_proof = {
        "proof_kind": "NOT_APPLICABLE",
        "proof_reference": f"{scenario_id}:no-broker-mutation",
        "observed_at_ms": observed_at_ms,
        "positions": {},
        "open_order_ids": (),
        "orders": (),
        "submit_calls": (),
        "cancel_calls": (),
        "lookup_calls": (),
    }
    return SyntheticEvidenceWorksheet(
        evidence_scope=scope,
        authority_participation=participation,
        authority_generation=protected.evidence.authority_generation,
        db_identity_token=protected.evidence.db_identity_token,
        control_revision=protected.control_revision,
        command_ids=command_ids,
        effect_operation_ids=(),
        order_refs=(),
        broker_order_ids=(),
        receipt_ids=receipt_ids,
        source_event_at_ms=started_at_ms,
        clerk_observed_at_ms=clerk_observed_at_ms,
        recorded_at_ms=completed_at_ms,
        expected_state=expected_state,
        observed_state=observed_state,
        operator_action=("Ran the bounded synthetic scenario without contacting a live broker."),
        warning_error_log_refs=outcome.log_refs,
        broker_before=broker_proof,
        broker_after=broker_proof,
    )


def synthetic_rehearsal_markdown(report: SyntheticCustodyRehearsalReport) -> str:
    """Render the audit section without changing any required-live status."""

    protected = report.protected_generation
    replay = report.polygon_replay
    recovery = report.recovery
    ui = report.ui_correlation
    lines = [
        "## Pre-live rehearsal (synthetic)",
        "",
        "> **Synthetic evidence only.** No live scenario was run or checked off. "
        "ADR 0035 remains **Proposed**. Every successful row is rehearsed — pending one live confirmation.",
        "",
        f"- Report SHA-256: `{report.report_sha256}`",
        f"- Overall: `{report.overall_status}`; abort classification: `{report.abort_class}`",
        f"- Protected authority: `{protected.account_id}` / generation `{protected.authority_generation}` / "
        f"DB identity `{protected.db_identity_token}`",
        f"- Storage guard: checked on `{protected.filesystem_type}`; production account "
        f"`{protected.production_account_id}` opened read-write: `false`; production mount "
        f"`{protected.production_mount_id}` is read-only and qualification mount "
        f"`{protected.qualification_mount_id}` is separately read-write",
        f"- Polygon fixture: `{replay.fixture_sha256}` ({replay.fixture_bar_count} minute rows); "
        f"{replay.synthesized_5s_count} deterministic 5-second prints drove "
        f"{replay.emitted_minute_count} exact closed minutes through `{replay.aggregation_path}`; "
        f"all `{replay.decision_count}` reached `{replay.decision_consumer_path}`",
        f"- Recovery: backup `{recovery.backup_sha256}`; integrity `{recovery.integrity_check}`; "
        "backup/restore/rebuild hash heads match",
        (
            f"- #1413 browser campaign: `{ui.interaction_count}` interactions across "
            f"`{ui.page_load_count}` page loads / `{ui.browser_reload_count}` actual reloads, "
            f"`{ui.native_event_source_connection_count}` native EventSource observations, "
            f"`{len(ui.correlation_records)}` row-level event/request/proof correlations, "
            f"historical state `{ui.historical_snapshot_state}` with presented action "
            f"`{ui.presented_action_id}`, and `{ui.lifecycle_request_count}` lifecycle requests; "
            "browser-page reload coverage: "
            f"`{ui.browser_reload_coverage}`"
            if ui is not None
            else "- #1413 browser campaign: `ABORTED_NO_MEASURED_RECEIPT`"
        ),
        "",
        "| Synthetic scenario | Result | Fault seam | Abort class | Abort cause | Limitations | Evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report.scenarios:
        result = {
            "REHEARSED_PENDING_LIVE_CONFIRMATION": ("rehearsed — pending one live confirmation"),
            "REHEARSED_WITH_LIMITATIONS_PENDING_LIVE_CONFIRMATION": (
                "rehearsed with limitations — pending one live confirmation"
            ),
            "ABORTED": "ABORTED",
        }[row.status]
        limitations = "<br>".join(f"`{item.code}`: {item.detail}" for item in row.limitations) or "none"
        refs = "<br>".join(f"`{ref}`" for ref in row.evidence_refs)
        seam = (
            "exercised"
            if row.fault_seam_exercised
            else "required but not exercised"
            if row.fault_seam_required
            else "not required"
        )
        lines.append(
            f"| {row.name} | {result} | {seam} | `{row.abort_class}` | `{row.abort_cause}` | {limitations} | {refs} |"
        )
    lines.extend(
        [
            "",
            "The required-live scenario table above remains `PENDING`. The content-addressed JSON "
            "report preserves the full #1409 field set (authority identity/revision, operation "
            "identities, three clock fields, broker before/after proof, expected/observed state, "
            "operator action, receipts, and logs). Any unavailable or inapplicable clock remains "
            "null and carries a typed clock limitation; no such row is presented as complete "
            "three-clock evidence.",
            "",
        ]
    )
    return "\n".join(lines)
