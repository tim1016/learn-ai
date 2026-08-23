from __future__ import annotations

from app.schemas.strategy_validation import (
    StrategyArtifactCheck,
    StrategyCategory,
    StrategyProofAction,
    StrategyProofDossier,
    StrategyProofStage,
    StrategyProofState,
    StrategyValidationEntry,
)

_QC_FILES_DOC = "https://www.quantconnect.com/docs/v2/cloud-platform/projects/files"
_QC_BACKTEST_DOC = "https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/getting-started"
_QC_RESULTS_DOC = "https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results"


def build_strategy_proof_dossier(
    entry: StrategyValidationEntry,
    *,
    strategy_category: StrategyCategory,
    has_signal_program: bool,
    reference_summary: str | None,
    audit_check: StrategyArtifactCheck | None,
    artifact_checks: list[StrategyArtifactCheck],
) -> StrategyProofDossier:
    proof_state = _proof_state(entry, artifact_checks)
    stages = [
        _program_contract_stage(has_signal_program),
        _reference_source_stage(
            entry,
            strategy_category=strategy_category,
            reference_summary=reference_summary,
            audit_check=audit_check,
        ),
        _reference_run_stage(entry, strategy_category),
        _reconciliation_stage(
            entry,
            strategy_category=strategy_category,
            has_signal_program=has_signal_program,
            reference_summary=reference_summary,
        ),
        _human_review_stage(entry),
        _current_proof_stage(proof_state, artifact_checks),
    ]
    applicable_stages = [stage for stage in stages if stage.state != "not_applicable"]
    blocker = next((stage for stage in applicable_stages if stage.state != "complete"), None)
    return StrategyProofDossier(
        state=proof_state,
        completed_stages=sum(stage.state == "complete" for stage in applicable_stages),
        total_stages=len(applicable_stages),
        blocking_stage_id=blocker.stage_id if blocker is not None else None,
        blocking_summary=(blocker.next_step or blocker.summary) if blocker is not None else None,
        stages=stages,
    )


def _program_contract_stage(has_signal_program: bool) -> StrategyProofStage:
    if has_signal_program:
        return StrategyProofStage(
            stage_id="program_contract",
            title="Signal Program contract",
            state="complete",
            authority="Strategy registry",
            summary=("The strategy has a versioned Signal Program contract and declared decision surface."),
        )
    return StrategyProofStage(
        stage_id="program_contract",
        title="Signal Program contract",
        state="missing",
        authority="Strategy registry",
        summary="This legacy strategy has not been promoted to a sealed Signal Program.",
        next_step=("Promote and qualify the Signal Program before preparing external validation evidence."),
    )


def _reference_source_stage(
    entry: StrategyValidationEntry,
    *,
    strategy_category: StrategyCategory,
    reference_summary: str | None,
    audit_check: StrategyArtifactCheck | None,
) -> StrategyProofStage:
    if strategy_category == "operational_validation_harness":
        if reference_summary:
            return StrategyProofStage(
                stage_id="reference_source",
                title="Internal replay reference",
                state="complete",
                authority="Signal Program numerical provenance",
                summary=reference_summary,
            )
        return StrategyProofStage(
            stage_id="reference_source",
            title="Internal replay reference",
            state="missing",
            authority="Signal Program numerical provenance",
            summary="The operational harness has no declared deterministic replay reference.",
            next_step="Declare the harness specification and deterministic replay corpus.",
        )

    evidence: list[StrategyArtifactCheck]
    if audit_check is None:
        state = "missing"
        summary = "No QuantConnect audit algorithm is registered for this production candidate."
        next_step = "Create and register an exact QuantConnect audit implementation."
        evidence = []
    elif audit_check.state == "current":
        state = "complete"
        summary = "The recorded QuantConnect audit algorithm matches the current candidate source."
        next_step = None
        evidence = [audit_check]
    elif audit_check.state == "stale":
        state = "stale"
        summary = "The QuantConnect audit algorithm changed after the recorded reference evidence."
        next_step = "Review and upload the current audit algorithm before running a fresh reference backtest."
        evidence = [audit_check]
    elif audit_check.state == "unreadable":
        state = "blocked"
        summary = "The recorded QuantConnect audit algorithm cannot be read."
        next_step = "Restore the audit algorithm at the recorded reference path."
        evidence = [audit_check]
    else:
        state = "missing"
        summary = "No QuantConnect audit algorithm is registered for this production candidate."
        next_step = "Create and register an exact QuantConnect audit implementation."
        evidence = [audit_check]
    return StrategyProofStage(
        stage_id="reference_source",
        title="QuantConnect reference algorithm",
        state=state,
        authority="Committed QuantConnect audit copy",
        summary=summary,
        next_step=next_step,
        actions=[
            StrategyProofAction(label="QuantConnect project files guide", href=_QC_FILES_DOC),
        ],
        evidence=evidence,
    )


def _reference_run_stage(
    entry: StrategyValidationEntry,
    strategy_category: StrategyCategory,
) -> StrategyProofStage:
    if strategy_category == "operational_validation_harness":
        return StrategyProofStage(
            stage_id="reference_run",
            title="QuantConnect reference run",
            state="not_applicable",
            authority="Strategy category policy",
            summary=("Operational validation harnesses do not enter the external-reference promotion track."),
        )
    if entry.qc_cloud_backtest_id:
        state = "complete"
        summary = f"QuantConnect backtest {entry.qc_cloud_backtest_id} pins the recorded reference run."
        next_step = None
    else:
        state = "missing"
        summary = "No QuantConnect Cloud backtest is attached to this strategy."
        next_step = "Run the registered audit algorithm in QuantConnect and attach the backtest ID."
    return StrategyProofStage(
        stage_id="reference_run",
        title="QuantConnect reference run",
        state=state,
        authority="QuantConnect Cloud",
        summary=summary,
        next_step=next_step,
        actions=[
            StrategyProofAction(
                label="How to run a backtest and find its ID",
                href=_QC_BACKTEST_DOC,
            ),
            StrategyProofAction(
                label="How to inspect and download results",
                href=_QC_RESULTS_DOC,
            ),
        ],
    )


def _reconciliation_stage(
    entry: StrategyValidationEntry,
    *,
    strategy_category: StrategyCategory,
    has_signal_program: bool,
    reference_summary: str | None,
) -> StrategyProofStage:
    if strategy_category == "operational_validation_harness":
        if has_signal_program and reference_summary:
            return StrategyProofStage(
                stage_id="reconciliation",
                title="Harness qualification",
                state="complete",
                authority="Golden trace and deterministic replay contract",
                summary=(
                    "The harness declares an internal deterministic qualification path. "
                    "Its running build proof remains independently checked at Start."
                ),
            )
        return StrategyProofStage(
            stage_id="reconciliation",
            title="Harness qualification",
            state="missing",
            authority="Golden trace and deterministic replay contract",
            summary="No internal harness qualification path is declared.",
            next_step="Qualify the harness against its deterministic replay corpus.",
        )

    diagnostics = entry.diagnostics
    if (
        entry.reconciliation_status == "passed"
        and diagnostics is not None
        and diagnostics.verdict == "passed"
        and not any(diagnostics.divergence_counts.values())
    ):
        state = "complete"
        summary = "The registered behavioral reconciliation passed with no gating divergences."
        next_step = None
    elif diagnostics is not None:
        state = "blocked"
        summary = "The registered behavioral reconciliation does not satisfy the deployment gate."
        next_step = "Resolve the named divergences and record a fresh reconciliation report."
    else:
        state = "missing"
        summary = "No behavioral reconciliation receipt is registered."
        next_step = "Compare the Python strategy with the attached QuantConnect reference run."
    return StrategyProofStage(
        stage_id="reconciliation",
        title="Behavioral reconciliation",
        state=state,
        authority="Python reconciliation receipt",
        summary=summary,
        next_step=next_step,
        actions=[
            StrategyProofAction(label="QuantConnect results reference", href=_QC_RESULTS_DOC),
        ],
    )


def _human_review_stage(entry: StrategyValidationEntry) -> StrategyProofStage:
    event = entry.current_flag_event
    if event is None:
        return StrategyProofStage(
            stage_id="human_review",
            title="Human review",
            state="missing",
            authority="Append-only validation event ledger",
            summary="No human review has been recorded for the current strategy evidence.",
            next_step="Review the evidence, record an Accept or Reject decision, and provide a reason.",
        )
    if event.flag == "invalidated":
        return StrategyProofStage(
            stage_id="human_review",
            title="Human review",
            state="blocked",
            authority="Append-only validation event ledger",
            summary="The active human review rejects this strategy evidence.",
            next_step="Resolve the review reason before preparing and reviewing fresh evidence.",
        )
    return StrategyProofStage(
        stage_id="human_review",
        title="Human review",
        state="complete",
        authority="Append-only validation event ledger",
        summary="A human accepted an immutable snapshot of the recorded evidence.",
    )


def _current_proof_stage(
    proof_state: StrategyProofState,
    artifact_checks: list[StrategyArtifactCheck],
) -> StrategyProofStage:
    if proof_state == "current":
        state = "complete"
        summary = "The accepted evidence matches the current artifacts and deployment proof."
        next_step = None
    elif proof_state == "stale":
        state = "stale"
        stale_labels = ", ".join(check.label for check in artifact_checks if check.state == "stale")
        summary = f"The accepted proof no longer matches: {stale_labels}."
        next_step = "Prepare fresh evidence for the changed artifacts, then record a new human review."
    elif proof_state == "unreadable":
        state = "blocked"
        summary = "One or more proof artifacts cannot be read."
        next_step = "Restore the unreadable artifacts before rechecking the stored proof."
    elif proof_state == "rejected":
        state = "blocked"
        summary = "The active human review rejects this proof."
        next_step = "Resolve the rejection and submit fresh evidence for review."
    elif proof_state == "blocked":
        state = "blocked"
        summary = "The recorded evidence is present but does not satisfy the deployment gate."
        next_step = "Resolve the blocking reconciliation or evidence-only verdict."
    else:
        state = "missing"
        summary = "A current accepted proof has not been assembled."
        next_step = "Complete the preceding proof stages and record a human review."
    return StrategyProofStage(
        stage_id="current_proof",
        title="Current validation proof",
        state=state,
        authority="Start-time strategy validation admission",
        summary=summary,
        next_step=next_step,
        evidence=artifact_checks,
    )


def _proof_state(
    entry: StrategyValidationEntry,
    artifact_checks: list[StrategyArtifactCheck],
) -> StrategyProofState:
    if entry.deployable:
        return "current"
    event = entry.current_flag_event
    if event is not None and event.flag == "invalidated":
        return "rejected"
    if any(check.state == "unreadable" for check in artifact_checks):
        return "unreadable"
    if any(check.state == "stale" for check in artifact_checks):
        return "stale"
    if entry.diagnostics is not None or event is not None:
        return "blocked"
    return "missing"
