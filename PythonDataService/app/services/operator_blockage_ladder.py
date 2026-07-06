"""Backend-authored blockage ladder for the operator surface."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.schemas.live_runs import (
    OperatorGate,
    OperatorSurfaceAccountOwner,
    OperatorSurfaceBlockageLadder,
    OperatorSurfaceBlockageStage,
    OperatorSurfaceBlockageStageId,
    OperatorSurfaceBlockageState,
    OperatorSurfaceBroker,
    OperatorSurfaceConditionSeverity,
    OperatorSurfaceControlPlane,
    OperatorSurfaceHostProcess,
    OperatorSurfaceReconciliation,
    OperatorSurfaceRuntimeFreshness,
    OperatorSurfaceTradingSession,
)
from app.services.account_truth_snapshot import AccountTruthAssessment
from app.services.operator_trader_guidance import (
    IBKR_CLIENT_ID_IN_USE,
    SubmitReadinessFinding,
    build_submit_readiness_findings,
)
from app.services.resume_guard_state import ResumeGuardState


@dataclass(frozen=True)
class _StageDraft:
    id: OperatorSurfaceBlockageStageId
    label: str
    severity: OperatorSurfaceConditionSeverity
    title: str
    summary: str
    next_step: str | None = None
    reason_codes: tuple[str, ...] = ()


_STAGE_ORDER: tuple[OperatorSurfaceBlockageStageId, ...] = (
    "control_plane",
    "host_process",
    "broker",
    "account_safety",
    "account_owner",
    "reconciliation",
    "preflight",
    "trading_session",
    "runtime_freshness",
)


def author_blockage_ladder(
    *,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
    readiness_gates: list[OperatorGate],
    account_truth: AccountTruthAssessment,
    control_plane: OperatorSurfaceControlPlane | None,
) -> OperatorSurfaceBlockageLadder:
    """Compose the lifecycle/current-blockage overview from canonical facts."""

    findings = build_submit_readiness_findings(
        host_process=host_process,
        broker=broker,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=guard_state,
        reconciliation=reconciliation,
        runtime_freshness=runtime_freshness,
        readiness_gates=readiness_gates,
        account_truth=account_truth,
    )
    stages = [
        _control_plane_stage(control_plane),
        _host_process_stage(host_process),
        _stage_from_findings(
            "broker",
            "Broker proof",
            findings,
            {"broker_safety", "broker_connection", "submission_capability"},
            ok_title="Broker proof is clear",
            ok_summary="Broker safety, connection, and submit capability have no active blockage findings.",
        ),
        _stage_from_findings(
            "account_safety",
            "Account safety",
            findings,
            {"account_truth", "account_frozen"},
            ok_title="Account safety is clear",
            ok_summary="Account Truth and account-freeze checks have no active blockage findings.",
        ),
        _stage_from_findings(
            "account_owner",
            "Account owner",
            findings,
            {"account_owner"},
            ok_title="Account owner is accepting",
            ok_summary="AccountOwner generation and phase are proven accepting.",
        ),
        _stage_from_findings(
            "reconciliation",
            "Reconciliation",
            findings,
            {"reconciliation", "submit_outcome_uncertain", "uncertain_intent"},
            ok_title="Reconciliation proof is clear",
            ok_summary="Broker/engine reconciliation has no active blockage findings.",
        ),
        _preflight_stage(findings),
        _stage_from_findings(
            "trading_session",
            "Trading session",
            findings,
            {"trading_session"},
            ok_title="Trading session permits activity",
            ok_summary="The current session window permits this strategy to act.",
        ),
        _runtime_freshness_stage(runtime_freshness),
    ]
    current_id = _current_stage_id(stages)
    if current_id is None:
        headline = "Lifecycle is clear"
        summary = "No active blockage rung is currently limiting this bot."
    else:
        current = next(stage for stage in stages if stage.id == current_id)
        headline = current.title
        summary = current.summary
    return OperatorSurfaceBlockageLadder(
        headline=headline,
        summary=summary,
        current_stage_id=current_id,
        stages=[
            OperatorSurfaceBlockageStage(
                id=stage.id,
                label=stage.label,
                state=_state_for_severity(stage.severity),
                severity=stage.severity,
                current=stage.id == current_id,
                title=stage.title,
                summary=stage.summary,
                next_step=stage.next_step,
                reason_codes=list(stage.reason_codes),
            )
            for stage in stages
        ],
    )


def _control_plane_stage(
    control_plane: OperatorSurfaceControlPlane | None,
) -> _StageDraft:
    if control_plane is None:
        return _StageDraft(
            "control_plane",
            "Control plane",
            "neutral",
            "Daemon control plane is not configured",
            "No live-runner daemon URL is configured for this data plane.",
        )
    if control_plane.state == "CONNECTED":
        return _StageDraft(
            "control_plane",
            "Control plane",
            "ok",
            "Daemon control plane connected",
            "The data plane can reach the host live-runner daemon.",
        )
    return _StageDraft(
        "control_plane",
        "Control plane",
        "warning",
        "Daemon control plane needs attention",
        control_plane.notice or "The data plane cannot currently prove healthy daemon control-plane connectivity.",
        reason_codes=(f"DAEMON_{control_plane.state}",),
    )


def _host_process_stage(host_process: OperatorSurfaceHostProcess) -> _StageDraft:
    if host_process.state == "RUNNING":
        return _StageDraft(
            "host_process",
            "Host process",
            "ok",
            "Bot process is running",
            "The host daemon reports this bot process is running.",
        )
    if host_process.last_exit_error_code == IBKR_CLIENT_ID_IN_USE:
        return _StageDraft(
            "host_process",
            "Host process",
            "critical",
            "IBKR client ID is already in use",
            host_process.last_exit_error_message
            or "IBKR Gateway rejected this bot because the requested client ID is already in use.",
            "Stop the sibling session using that client ID, expand the live-runner client-id pool, or restart IB Gateway if the slot is stale.",
            reason_codes=(IBKR_CLIENT_ID_IN_USE,),
        )
    return _StageDraft(
        "host_process",
        "Host process",
        "warning",
        "Bot process is not running",
        host_process.notice or f"Host process state is {host_process.state}.",
        reason_codes=(f"HOST_PROCESS_{host_process.state}",),
    )


def _preflight_stage(findings: list[SubmitReadinessFinding]) -> _StageDraft:
    return _stage_from_findings(
        "preflight",
        "Pre-flight gates",
        findings,
        {finding.attention_code for finding in findings if finding.attention_code.startswith("readiness.")},
        ok_title="Pre-flight gates are clear",
        ok_summary="No hard readiness gate is currently blocking pre-submit flow.",
    )


def _runtime_freshness_stage(
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
) -> _StageDraft:
    if runtime_freshness is None:
        return _StageDraft(
            "runtime_freshness",
            "Runtime evidence",
            "warning",
            "Runtime evidence is not bound",
            "No child runtime freshness envelope is currently bound to this instance.",
            reason_codes=("RUNTIME_FRESHNESS_MISSING",),
        )
    if runtime_freshness.headline is not None:
        notice = runtime_freshness.headline
        return _StageDraft(
            "runtime_freshness",
            "Runtime evidence",
            _condition_severity(notice.tier),
            notice.title,
            notice.message,
            reason_codes=tuple(runtime_freshness.stale_reason_codes),
        )
    if runtime_freshness.additional_reasons:
        notice = runtime_freshness.additional_reasons[0]
        return _StageDraft(
            "runtime_freshness",
            "Runtime evidence",
            _condition_severity(notice.tier),
            notice.title,
            notice.message,
            reason_codes=tuple(runtime_freshness.stale_reason_codes),
        )
    return _StageDraft(
        "runtime_freshness",
        "Runtime evidence",
        "ok",
        "Runtime evidence is fresh",
        "No active runtime-freshness notice is present.",
    )


def _stage_from_findings(
    stage_id: OperatorSurfaceBlockageStageId,
    label: str,
    findings: list[SubmitReadinessFinding],
    attention_codes: set[str],
    *,
    ok_title: str,
    ok_summary: str,
) -> _StageDraft:
    matches = [finding for finding in findings if finding.attention_code in attention_codes]
    if not matches:
        return _StageDraft(stage_id, label, "ok", ok_title, ok_summary)
    strongest = _strongest_finding(matches)
    return _StageDraft(
        stage_id,
        label,
        _condition_severity(strongest.attention_severity),
        strongest.attention_headline,
        strongest.attention_explanation,
        strongest.operator_next_step,
        tuple(_unique_reason_codes(matches)),
    )


def _strongest_finding(findings: Iterable[SubmitReadinessFinding]) -> SubmitReadinessFinding:
    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(findings, key=lambda finding: order[finding.attention_severity])[0]


def _condition_severity(severity: str) -> OperatorSurfaceConditionSeverity:
    if severity == "critical":
        return "critical"
    if severity == "warning":
        return "warning"
    return "info"


def _unique_reason_codes(findings: Iterable[SubmitReadinessFinding]) -> list[str]:
    codes: list[str] = []
    for finding in findings:
        if finding.reason_code not in codes:
            codes.append(finding.reason_code)
    return codes


def _current_stage_id(stages: list[_StageDraft]) -> OperatorSurfaceBlockageStageId | None:
    for severity in ("critical", "warning", "info"):
        for stage in stages:
            if stage.severity == severity:
                return stage.id
    return None


def _state_for_severity(
    severity: OperatorSurfaceConditionSeverity,
) -> OperatorSurfaceBlockageState:
    if severity == "ok":
        return "clear"
    if severity == "critical":
        return "danger"
    if severity == "warning":
        return "warning"
    if severity == "info":
        return "info"
    return "unknown"
