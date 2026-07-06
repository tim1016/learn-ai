"""Trader guidance authoring for the operator surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.engine.live.account_artifacts import AccountFreezeEvidence
from app.schemas.live_runs import (
    InvokeEndpointAction,
    NoPrimaryRemediationAction,
    OpenRunbookAction,
    OperatorGate,
    OperatorSurfaceAccountOwner,
    OperatorSurfaceAttentionGroup,
    OperatorSurfaceBroker,
    OperatorSurfaceDailyOrderCap,
    OperatorSurfaceEvidenceFact,
    OperatorSurfaceHostProcess,
    OperatorSurfaceNamedCondition,
    OperatorSurfaceProofLine,
    OperatorSurfaceReconciliation,
    OperatorSurfaceRuntimeFreshness,
    OperatorSurfaceSubmitReadiness,
    OperatorSurfaceTraderGuidance,
    OperatorSurfaceTradingSession,
    ReconciliationState,
    SubmitReadinessCode,
    TraderAttentionSeverity,
    TraderPrimaryRemediation,
    TraderSituationCode,
)
from app.services.account_truth_snapshot import AccountTruthAssessment
from app.services.resume_guard_state import ResumeGuardState

_READY_RECONCILIATION_STATES: frozenset[ReconciliationState] = frozenset({"CLEAN", "ADOPTED"})

_SUBMIT_READINESS_COPY: dict[SubmitReadinessCode, tuple[str, str]] = {
    "safe_to_submit": (
        "Safe to submit",
        "Broker safety, submit capability, AccountOwner generation, reconciliation, and runtime proofs are all satisfied.",
    ),
    "safe_to_monitor": (
        "Safe to monitor",
        "The cockpit can observe this bot, but order submission is not currently active or appropriate.",
    ),
    "blocked_before_submit": (
        "Blocked before submit",
        "A pre-submit gate would stop a new order before it reaches the broker.",
    ),
    "broker_state_unproven": (
        "Broker state unproven",
        "The backend cannot prove the broker/session/reconciliation evidence required for a safe submit.",
    ),
    "account_frozen": (
        "Account frozen",
        "Account-wide unresolved exposure is active; no sibling bot on this account may submit.",
    ),
    "waiting_for_owner_generation": (
        "Waiting for owner generation",
        "The AccountOwner generation or phase is not proven accepting, so single-writer submission is not proven.",
    ),
    "submit_outcome_uncertain": (
        "Submit outcome uncertain",
        "An intent may already have reached the broker; probe/reconcile before any retry.",
    ),
}

_TRADER_GUIDANCE_COPY: dict[TraderSituationCode, tuple[str, str, str, str]] = {
    "ready_to_submit": (
        "This bot is ready to submit paper orders.",
        "All backend submit-readiness proofs are currently satisfied.",
        "Submission gates are satisfied",
        "The surface is allowed to say safe to submit because the broker, submit lane, owner generation, and reconciliation proofs are all present.",
    ),
    "monitor_only": (
        "This bot is safe to monitor, not safe to submit right now.",
        "The current state is observable, but at least one non-critical condition means order submission should not be treated as active.",
        "Observation is okay; trading is not active",
        "Keep watching the bot, but do not interpret the Overview as a trade-permission signal.",
    ),
    "submission_blocked": (
        "A pre-submit gate is blocking this bot.",
        "The backend would stop a new order before it reaches the broker.",
        "Order submission is blocked before broker placement",
        "This is a controlled block, not proof that a broker order exists.",
    ),
    "broker_state_unproven": (
        "Broker state is not proven enough to submit.",
        "The backend cannot prove the broker/session/reconciliation facts needed before a submit.",
        "Do not treat stale or missing broker evidence as live truth",
        "Reconnect or reconcile until the broker evidence is fresh and explicit.",
    ),
    "account_frozen": (
        "This account has an active freeze.",
        "An account-wide unresolved-exposure artifact is present, so every sibling bot must treat submission as stopped.",
        "Account-wide stop sign",
        "Resolve the account exposure before any bot on this account submits.",
    ),
    "waiting_for_owner_generation": (
        "AccountOwner is not accepting submits yet.",
        "The current AccountOwner generation/phase is missing or not in the accepting phase.",
        "Single-writer proof is incomplete",
        "Wait for AccountOwner to reach accepting, or recover the owner lane before trading.",
    ),
    "submit_outcome_uncertain": (
        "A previous submit outcome is uncertain.",
        "An ACK_FAILED_UNCERTAIN or equivalent unresolved submit condition is active in the durable evidence.",
        "Do not blind-retry",
        "Probe or reconcile the broker before any retry so the bot cannot duplicate or orphan an order.",
    ),
    "attention_required": (
        "This bot needs operator attention.",
        "One or more backend-authored facts are not in their ready state.",
        "Review the independent facts below",
        "The summary does not replace the underlying process, broker, safety, and account facts.",
    ),
    "unknown": (
        "The bot state is not fully known.",
        "The backend is missing enough evidence that it cannot author a stronger trader summary.",
        "Unknown is not safe",
        "Treat missing evidence as a reason to inspect the raw artifacts or runbook.",
    ),
}


@dataclass(frozen=True)
class SubmitReadinessFinding:
    """One prioritized backend-authored reason affecting submit readiness."""

    readiness_code: SubmitReadinessCode
    reason_code: str
    attention_code: str
    attention_severity: TraderAttentionSeverity
    attention_headline: str
    attention_explanation: str
    operator_next_step: str
    remediation: TraderPrimaryRemediation


def author_submit_readiness(
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
) -> OperatorSurfaceSubmitReadiness:
    """Author submit readiness from a single prioritized finding list."""

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
    code: SubmitReadinessCode = findings[0].readiness_code if findings else "safe_to_submit"
    label, explanation = _SUBMIT_READINESS_COPY[code]
    return OperatorSurfaceSubmitReadiness(
        code=code,
        label=label,
        explanation=explanation,
        can_submit=code == "safe_to_submit",
        blocking_reason_codes=_unique_reason_codes(findings),
        template_id=f"operator_surface.submit_readiness.{code}",
        template_version=1,
    )


def author_trader_guidance(
    *,
    submit_readiness: OperatorSurfaceSubmitReadiness,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
    readiness_gates: list[OperatorGate],
    daily_order_cap: OperatorSurfaceDailyOrderCap,
    account_truth: AccountTruthAssessment,
) -> OperatorSurfaceTraderGuidance:
    """Author trader-readable guidance from the same prioritized findings."""

    findings = build_submit_readiness_findings(
        host_process=host_process,
        broker=broker,
        trading_session=trading_session,
        account_owner=account_owner,
        account_freeze=account_freeze,
        guard_state=guard_state,
        reconciliation=reconciliation,
        readiness_gates=readiness_gates,
        runtime_freshness=runtime_freshness,
        account_truth=account_truth,
    )
    situation_code = _situation_code_for_submit_readiness(submit_readiness.code, findings)
    headline, explanation, risk_headline, risk_explanation = _TRADER_GUIDANCE_COPY[situation_code]
    return OperatorSurfaceTraderGuidance(
        situation_code=situation_code,
        headline=headline,
        explanation=explanation,
        risk_headline=risk_headline,
        risk_explanation=risk_explanation,
        primary_remediation=_primary_remediation(submit_readiness.code, findings),
        additional_attention_groups=_attention_groups(findings),
        proof_lines=_proof_lines(
            submit_readiness=submit_readiness,
            broker=broker,
            account_owner=account_owner,
            reconciliation=reconciliation,
            runtime_freshness=runtime_freshness,
        ),
        advanced_evidence=_submit_readiness_evidence(
            host_process=host_process,
            broker=broker,
            trading_session=trading_session,
            account_owner=account_owner,
            account_freeze=account_freeze,
            guard_state=guard_state,
            reconciliation=reconciliation,
            readiness_gates=readiness_gates,
            daily_order_cap=daily_order_cap,
        ),
        template_id=f"operator_surface.trader_guidance.{situation_code}",
        template_version=1,
    )


def build_submit_readiness_findings(
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
) -> list[SubmitReadinessFinding]:
    findings: list[SubmitReadinessFinding] = []
    if account_freeze is not None:
        findings.append(
            _finding(
                "account_frozen",
                "ACCOUNT_FROZEN",
                "account_frozen",
                "critical",
                "Account freeze active",
                account_freeze.reason,
                "Open the freeze/runbook evidence and clear the account only after broker exposure is proven.",
                OpenRunbookAction(kind="open_runbook", slug="watchdog-halt"),
            )
        )
    if guard_state.uncertain_intent.state == "PRESENT":
        intents = ", ".join(guard_state.uncertain_intent.unresolved_intent_ids) or "unknown intent"
        findings.append(
            _finding(
                "submit_outcome_uncertain",
                "UNRESOLVED_UNCERTAIN_INTENT",
                "submit_outcome_uncertain",
                "critical",
                "Submit outcome uncertain",
                f"Unresolved intents: {intents}.",
                "Run runtime reconciliation before retrying any submit.",
                InvokeEndpointAction(kind="invoke_endpoint", endpoint="reconcile_instance"),
            )
        )
    if guard_state.uncertain_intent.state == "UNKNOWN":
        findings.append(
            _finding(
                "broker_state_unproven",
                "UNCERTAIN_INTENT_STATE_UNKNOWN",
                "uncertain_intent",
                "warning",
                "Uncertain intent state unknown",
                "The durable Intent WAL uncertain-state proof is unavailable.",
                "Run runtime reconciliation when a live process is bound, or inspect the intent WAL before retrying.",
                InvokeEndpointAction(kind="invoke_endpoint", endpoint="reconcile_instance"),
            )
        )
    if broker.safety_verdict != "PAPER_ONLY":
        findings.append(
            _finding(
                "broker_state_unproven",
                f"BROKER_SAFETY_{broker.safety_verdict}",
                "broker_safety",
                "critical",
                "Broker safety is not paper-only",
                f"Safety verdict is {broker.safety_verdict}.",
                "Inspect broker/account safety evidence before any trading action.",
                OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface"),
            )
        )
    if broker.connection != "CONNECTED":
        condition = broker.connection_condition
        connection_remediation: TraderPrimaryRemediation = (
            OpenRunbookAction(kind="open_runbook", slug="broker-reconnect")
            if host_process.state == "RUNNING"
            else NoPrimaryRemediationAction(kind="none", reason="WAITING_FOR_LIVE_RUNTIME")
        )
        findings.append(
            _finding(
                "broker_state_unproven",
                condition.code,
                "broker_connection",
                _attention_severity_from_condition(condition),
                condition.title,
                _broker_connection_detail(condition, host_process.state),
                (
                    condition.remediation
                    if host_process.state == "RUNNING"
                    and condition.remediation is not None
                    else "Start a bot process only after IBKR positions/executions are manually verified; broker proof cannot refresh while no runtime is bound."
                ),
                connection_remediation,
            )
        )
    if guard_state.submission_capability.state != "SATISFIED":
        findings.append(
            _finding(
                "blocked_before_submit",
                f"SUBMISSION_CAPABILITY_{guard_state.submission_capability.state}",
                "submission_capability",
                "warning",
                "Submission capability is blocked",
                f"Submission capability is {guard_state.submission_capability.state}.",
                "Inspect the submit guard evidence before attempting another order.",
                OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface"),
            )
        )
    if not _account_owner_ready(account_owner):
        phase = "unknown" if account_owner is None else account_owner.phase
        reason = (
            "ACCOUNT_OWNER_GENERATION_UNPROVEN"
            if account_owner is None or account_owner.generation is None or phase == "unknown"
            else f"ACCOUNT_OWNER_PHASE_{phase.upper()}"
        )
        findings.append(
            _finding(
                "waiting_for_owner_generation",
                reason,
                "account_owner",
                "warning",
                "AccountOwner not proven accepting",
                f"AccountOwner phase is {phase}.",
                "Wait for AccountOwner accepting/generation proof before expecting submit readiness to clear.",
                OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface"),
            )
        )
    if not _is_reconciliation_ready(reconciliation):
        state = "NOT_AVAILABLE" if reconciliation is None else reconciliation.state
        reconciliation_remediation: TraderPrimaryRemediation = (
            InvokeEndpointAction(kind="invoke_endpoint", endpoint="reconcile_instance")
            if host_process.state == "RUNNING"
            else NoPrimaryRemediationAction(kind="none", reason="WAITING_FOR_LIVE_RUNTIME")
        )
        findings.append(
            _finding(
                "broker_state_unproven",
                f"RECONCILIATION_{state}",
                "reconciliation",
                "warning",
                (
                    "Reconciliation is not fresh-clean"
                    if host_process.state == "RUNNING"
                    else "Runtime reconciliation is waiting for a live bot process"
                ),
                (
                    f"Reconciliation state is {state}."
                    if host_process.state == "RUNNING"
                    else f"Reconciliation state is {state}; runtime reconciliation cannot run until a bot process is bound."
                ),
                (
                    "Run reconciliation and wait for a clean or adopted receipt."
                    if host_process.state == "RUNNING"
                    else "Manually verify IBKR first; then start/redeploy the bot so runtime reconciliation can produce a receipt."
                ),
                reconciliation_remediation,
            )
        )
    findings.extend(_runtime_freshness_findings(host_process, runtime_freshness, trading_session))
    findings.extend(_account_truth_findings(account_truth))
    for gate in _hard_blocking_readiness_gates(readiness_gates):
        findings.append(
            _finding(
                "blocked_before_submit",
                f"READINESS_GATE_{gate.name}",
                f"readiness.{gate.name}",
                "warning",
                gate.name.replace("_", " ").capitalize(),
                gate.detail,
                gate.gate_result.operator_next_step or "Inspect this readiness gate before retrying.",
                OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface"),
            )
        )
    if host_process.state != "RUNNING":
        findings.append(
            _finding(
                "safe_to_monitor",
                f"HOST_PROCESS_{host_process.state}",
                "host_process",
                "info",
                "No live runtime is bound",
                f"Host process is {host_process.state}; live-only commands cannot execute until a bot process is started.",
                "Use this as context for the blocked broker/reconciliation proofs, not as a separate broker problem.",
                NoPrimaryRemediationAction(kind="none", reason="MONITOR_ONLY"),
            )
        )
    if trading_session.permits_strategy_activity is not True:
        findings.append(
            _finding(
                "safe_to_monitor",
                f"TRADING_SESSION_{trading_session.phase}",
                "trading_session",
                "info",
                "Trading session not accepting strategy activity",
                trading_session.phase,
                "Wait for the strategy session window before expecting submit readiness to clear.",
                NoPrimaryRemediationAction(kind="none", reason="MONITOR_ONLY"),
            )
        )
    return findings


def _finding(
    readiness_code: SubmitReadinessCode,
    reason_code: str,
    attention_code: str,
    attention_severity: TraderAttentionSeverity,
    attention_headline: str,
    attention_explanation: str,
    operator_next_step: str,
    remediation: TraderPrimaryRemediation,
) -> SubmitReadinessFinding:
    return SubmitReadinessFinding(
        readiness_code=readiness_code,
        reason_code=reason_code,
        attention_code=attention_code,
        attention_severity=attention_severity,
        attention_headline=attention_headline,
        attention_explanation=attention_explanation,
        operator_next_step=operator_next_step,
        remediation=remediation,
    )


def _fact(
    label: str,
    value: object,
    *,
    source: str | None = None,
    gate_id: str | None = None,
    ts_ms: int | None = None,
) -> OperatorSurfaceEvidenceFact:
    return OperatorSurfaceEvidenceFact(
        label=label,
        value=str(value),
        source=source,
        gate_id=gate_id,
        ts_ms=ts_ms,
        ts_ms_resolved=ts_ms is not None,
    )


def _hard_blocking_readiness_gates(readiness_gates: list[OperatorGate]) -> list[OperatorGate]:
    return [gate for gate in readiness_gates if gate.status != "pass" and gate.severity == "hard"]


def _is_reconciliation_ready(reconciliation: OperatorSurfaceReconciliation | None) -> bool:
    return reconciliation is not None and reconciliation.state in _READY_RECONCILIATION_STATES


def _account_owner_ready(account_owner: OperatorSurfaceAccountOwner | None) -> bool:
    return account_owner is not None and account_owner.generation is not None and account_owner.phase == "accepting"


def _account_truth_findings(assessment: AccountTruthAssessment) -> list[SubmitReadinessFinding]:
    if assessment.can_submit:
        return []

    severity: TraderAttentionSeverity = (
        "critical" if "ACCOUNT_TRUTH_NOT_PROVEN" in assessment.reason_codes else "warning"
    )
    return [
        _finding(
            "broker_state_unproven",
            reason_code,
            "account_truth",
            severity,
            assessment.headline,
            assessment.explanation,
            assessment.operator_next_step
            or "Open Account Monitor and refresh Account Truth before treating submit readiness as safe.",
            OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface"),
        )
        for reason_code in assessment.reason_codes
    ]


def _runtime_freshness_findings(
    host_process: OperatorSurfaceHostProcess,
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
    trading_session: OperatorSurfaceTradingSession,
) -> list[SubmitReadinessFinding]:
    if (
        host_process.state != "RUNNING"
        or runtime_freshness is None
        or trading_session.permits_strategy_activity is not True
    ):
        return []

    active_codes = set(runtime_freshness.stale_reason_codes)
    if "BAR_LOOP_FIRST_BAR_TIMEOUT" in active_codes:
        return [
            _finding(
                "blocked_before_submit",
                "MARKET_DATA_FIRST_BAR_TIMEOUT",
                "runtime_freshness",
                "critical",
                "IBKR market data is silent",
                (
                    "IBKR accepted the live bar subscription, but no first bar arrived. "
                    "A competing live session or missing paper market-data entitlement can "
                    "starve the paper API feed while broker/account calls still look healthy."
                ),
                (
                    "Log out of competing IBKR live/paper sessions, verify paper market-data "
                    "sharing or subscriptions, restart Gateway, and wait for a fresh signal bar."
                ),
                OpenRunbookAction(kind="open_runbook", slug="runtime-freshness"),
            )
        ]
    if "BAR_LOOP_SOURCE_MISSING" in active_codes:
        return [
            _finding(
                "blocked_before_submit",
                "MARKET_DATA_SOURCE_MISSING",
                "runtime_freshness",
                "critical",
                "IBKR market data has not started",
                (
                    "The live bar source is running, but no source bar has reached the engine. "
                    "New trading decisions are held until the signal feed proves fresh bars."
                ),
                "Check IBKR market-data availability and the configured live bar source before treating the bot as ready.",
                OpenRunbookAction(kind="open_runbook", slug="runtime-freshness"),
            )
        ]
    if active_codes & {"BAR_LOOP_HEARTBEAT_STALE", "BAR_LOOP_LATEST_BAR_STALE"}:
        return [
            _finding(
                "blocked_before_submit",
                "MARKET_DATA_STALE",
                "runtime_freshness",
                "warning",
                "Market data feed is stale",
                "The signal feed has stale runtime evidence. New trading decisions are held until fresh bars arrive.",
                "Wait for fresh bars or inspect the IBKR market-data session before treating the bot as ready.",
                OpenRunbookAction(kind="open_runbook", slug="runtime-freshness"),
            )
        ]
    return []


def _unique_reason_codes(findings: list[SubmitReadinessFinding]) -> list[str]:
    codes: list[str] = []
    for finding in findings:
        if finding.reason_code not in codes:
            codes.append(finding.reason_code)
    return codes


def _primary_remediation(
    code: SubmitReadinessCode,
    findings: list[SubmitReadinessFinding],
) -> TraderPrimaryRemediation:
    if code == "safe_to_submit":
        return NoPrimaryRemediationAction(kind="none", reason="READY")
    if code == "safe_to_monitor":
        return NoPrimaryRemediationAction(kind="none", reason="MONITOR_ONLY")
    if findings:
        return findings[0].remediation
    return OpenRunbookAction(kind="open_runbook", slug="broker-instance-operator-surface")


def _attention_groups(findings: list[SubmitReadinessFinding]) -> list[OperatorSurfaceAttentionGroup]:
    groups: list[OperatorSurfaceAttentionGroup] = []
    for finding in findings:
        if any(group.code == finding.attention_code for group in groups):
            continue
        groups.append(
            OperatorSurfaceAttentionGroup(
                code=finding.attention_code,
                severity=finding.attention_severity,
                headline=finding.attention_headline,
                explanation=finding.attention_explanation,
                operator_next_step=finding.operator_next_step,
                remediation=finding.remediation,
            )
        )
    return groups


def _proof_lines(
    *,
    submit_readiness: OperatorSurfaceSubmitReadiness,
    broker: OperatorSurfaceBroker,
    account_owner: OperatorSurfaceAccountOwner | None,
    reconciliation: OperatorSurfaceReconciliation | None,
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
) -> list[OperatorSurfaceProofLine]:
    return [
        _broker_proof_line(broker),
        _submit_readiness_proof_line(submit_readiness),
        _account_owner_proof_line(account_owner),
        _reconciliation_proof_line(reconciliation),
        _runtime_freshness_proof_line(runtime_freshness),
    ]


def _proof_line(
    proof_id: str,
    label: str,
    message: str,
    detail: str,
    tone: Literal["neutral", "ok", "attention"],
) -> OperatorSurfaceProofLine:
    return OperatorSurfaceProofLine(
        id=proof_id,
        label=label,
        message=message,
        detail=detail,
        tone=tone,
    )


def _broker_proof_line(broker: OperatorSurfaceBroker) -> OperatorSurfaceProofLine:
    if broker.safety_verdict == "PAPER_ONLY" and broker.connection == "CONNECTED":
        message = "Paper broker is connected."
        tone: Literal["neutral", "ok", "attention"] = "ok"
    elif broker.safety_verdict == "PAPER_ONLY" and broker.connection in {"DISCONNECTED", "DEGRADED"}:
        message = f"Paper broker is configured; {broker.connection_condition.title}."
        tone = "attention"
    elif broker.safety_verdict == "PAPER_ONLY":
        message = "Paper broker proof is available; broker connection remains unproven."
        tone = "attention"
    elif broker.connection == "CONNECTED":
        message = "Broker is connected, but paper-only proof is missing."
        tone = "attention"
    elif broker.connection in {"DISCONNECTED", "DEGRADED"}:
        message = broker.connection_condition.title
        tone = "attention"
    else:
        message = "Broker proof is not available yet."
        tone = "attention"
    return _proof_line(
        "broker-proof",
        "Broker",
        message,
        f"{_broker_safety_detail(broker.safety_verdict)} {_broker_connection_detail(broker.connection_condition)}",
        tone,
    )


def _submit_readiness_proof_line(readiness: OperatorSurfaceSubmitReadiness) -> OperatorSurfaceProofLine:
    detail = readiness.explanation
    if readiness.blocking_reason_codes:
        blocker_count = len(readiness.blocking_reason_codes)
        plural = "proofs" if blocker_count != 1 else "proof"
        detail = f"{detail} {blocker_count} blocking {plural} still need attention."
    return _proof_line(
        "submit-readiness",
        "Trade submit",
        readiness.label,
        detail,
        "ok" if readiness.can_submit else "attention",
    )


def _account_owner_proof_line(owner: OperatorSurfaceAccountOwner | None) -> OperatorSurfaceProofLine:
    if owner is None:
        return _proof_line(
            "account-owner",
            "Account owner",
            "Waiting for AccountOwner proof.",
            "No AccountOwner artifact is available for this bot.",
            "attention",
        )
    if owner.generation is None:
        message = "Waiting for AccountOwner generation."
    elif owner.phase == "accepting":
        message = f"Owner generation {owner.generation} is accepting commands."
    elif owner.phase == "reconnecting":
        message = f"Owner generation {owner.generation} is reconnecting."
    elif owner.phase == "draining":
        message = f"Owner generation {owner.generation} is draining open work."
    elif owner.phase == "frozen":
        message = f"Owner generation {owner.generation} is frozen."
    else:
        message = f"Owner generation {owner.generation} has unknown state."
    detail_parts = [
        f"Account {owner.account_id} owner phase is {_owner_phase_label(owner.phase)}.",
        f"Generation {owner.generation}." if owner.generation is not None else "Generation is not recorded.",
    ]
    return _proof_line(
        "account-owner",
        "Account owner",
        message,
        ". ".join(part for part in detail_parts if part is not None),
        "ok" if _account_owner_ready(owner) else "attention",
    )


def _reconciliation_proof_line(reconciliation: OperatorSurfaceReconciliation | None) -> OperatorSurfaceProofLine:
    if reconciliation is None or reconciliation.state == "NOT_AVAILABLE":
        return _proof_line(
            "reconciliation",
            "Reconciliation",
            "Waiting for reconciliation proof.",
            "No reconciliation claim has been produced for this run.",
            "attention",
        )
    if reconciliation.state == "IN_PROGRESS":
        message = "Reconciliation is running."
    elif reconciliation.state == "CLEAN":
        message = "Broker and engine agree."
    elif reconciliation.state == "ADOPTED":
        message = "Recovered prior intents into engine state."
    elif reconciliation.state == "STALE":
        message = "Reconciliation proof is stale."
    else:
        message = reconciliation.failure_reason or "Broker and engine do not agree."
    detail_parts = [
        _reconciliation_state_detail(reconciliation.state),
        f"Reason: {reconciliation.failure_reason}" if reconciliation.failure_reason else None,
        f"Adopted intents: {', '.join(reconciliation.adopted_intent_ids)}"
        if reconciliation.adopted_intent_ids
        else None,
    ]
    return _proof_line(
        "reconciliation",
        "Reconciliation",
        message,
        ". ".join(part for part in detail_parts if part is not None),
        "ok" if _is_reconciliation_ready(reconciliation) else "attention",
    )


def _runtime_freshness_proof_line(
    runtime_freshness: OperatorSurfaceRuntimeFreshness | None,
) -> OperatorSurfaceProofLine:
    if runtime_freshness is None:
        return _proof_line(
            "runtime-freshness",
            "Runtime",
            "No live runtime is bound yet.",
            "No child runtime is currently bound to this instance.",
            "attention",
        )
    notice = runtime_freshness.headline or (runtime_freshness.additional_reasons[0] if runtime_freshness.additional_reasons else None)
    if notice is not None:
        return _proof_line(
            "runtime-freshness",
            "Runtime",
            notice.message,
            notice.title,
            "neutral" if notice.tier == "info" else "attention",
        )
    return _proof_line(
        "runtime-freshness",
        "Runtime",
        "Runtime evidence is fresh.",
        "No active runtime-freshness notices.",
        "ok",
    )


def _broker_safety_detail(safety_verdict: str) -> str:
    if safety_verdict == "PAPER_ONLY":
        return "Paper-only account proof is present."
    if safety_verdict == "UNSAFE":
        return "Paper-only account proof is missing."
    return "Account safety proof is not recorded."


def _attention_severity_from_condition(
    condition: OperatorSurfaceNamedCondition,
) -> TraderAttentionSeverity:
    if condition.severity == "critical":
        return "critical"
    if condition.severity == "warning":
        return "warning"
    return "info"


def _broker_connection_detail(
    condition: OperatorSurfaceNamedCondition,
    host_state: str = "RUNNING",
) -> str:
    if host_state != "RUNNING" and condition.code == "BROKER_CONNECTION_UNKNOWN":
        return "Broker connection has not been proven because no live runtime is currently bound."
    return condition.summary


def _owner_phase_label(phase: str) -> str:
    if phase == "accepting":
        return "accepting commands"
    if phase == "reconnecting":
        return "reconnecting"
    if phase == "draining":
        return "draining open work"
    if phase == "frozen":
        return "frozen"
    return "unknown"


def _reconciliation_state_detail(state: ReconciliationState) -> str:
    if state == "CLEAN":
        return "Latest reconciliation claim is clean."
    if state == "ADOPTED":
        return "Latest reconciliation adopted prior broker work into engine state."
    if state == "IN_PROGRESS":
        return "Reconciliation is still running."
    if state == "STALE":
        return "Latest reconciliation proof is stale."
    if state == "NOT_AVAILABLE":
        return "No reconciliation claim has been produced for this run."
    return "Latest reconciliation claim needs operator attention."


def _situation_code_for_submit_readiness(
    code: SubmitReadinessCode,
    findings: list[SubmitReadinessFinding],
) -> TraderSituationCode:
    if code == "safe_to_submit":
        return "ready_to_submit"
    if code == "safe_to_monitor":
        return "monitor_only"
    if code == "blocked_before_submit":
        return "submission_blocked"
    if code in {
        "broker_state_unproven",
        "account_frozen",
        "waiting_for_owner_generation",
        "submit_outcome_uncertain",
    }:
        return code
    if findings:
        return "attention_required"
    return "unknown"


def _submit_readiness_evidence(
    *,
    host_process: OperatorSurfaceHostProcess,
    broker: OperatorSurfaceBroker,
    trading_session: OperatorSurfaceTradingSession,
    account_owner: OperatorSurfaceAccountOwner | None,
    account_freeze: AccountFreezeEvidence | None,
    guard_state: ResumeGuardState,
    reconciliation: OperatorSurfaceReconciliation | None,
    readiness_gates: list[OperatorGate],
    daily_order_cap: OperatorSurfaceDailyOrderCap,
) -> list[OperatorSurfaceEvidenceFact]:
    facts = [
        _fact("host_process.state", host_process.state, source="operator_surface"),
        _fact("broker.safety_verdict", broker.safety_verdict, source="operator_surface"),
        _fact("broker.connection", broker.connection, source="operator_surface"),
        _fact(
            "submission_capability.state",
            guard_state.submission_capability.state,
            source="resume_guard_state",
        ),
        _fact("uncertain_intent.state", guard_state.uncertain_intent.state, source="intent_wal"),
        _fact(
            "reconciliation.state",
            reconciliation.state if reconciliation is not None else "NOT_AVAILABLE",
            source="reconciliation_receipt",
            ts_ms=reconciliation.last_reconcile_ms if reconciliation is not None else None,
        ),
        _fact("trading_session.phase", trading_session.phase, source="operator_surface", ts_ms=trading_session.as_of_ms),
    ]
    if daily_order_cap.used is not None or daily_order_cap.limit is not None:
        facts.append(
            _fact(
                "daily_order_cap",
                f"{daily_order_cap.used if daily_order_cap.used is not None else 'unknown'}/"
                f"{daily_order_cap.limit if daily_order_cap.limit is not None else 'unknown'}",
                source="readiness",
            )
        )
    if account_owner is None:
        facts.append(_fact("account_owner.phase", "unknown", source="account_artifacts"))
    else:
        facts.append(
            _fact(
                "account_owner.phase",
                account_owner.phase,
                source=account_owner.source or "account_artifacts",
                ts_ms=account_owner.recorded_at_ms,
            )
        )
        facts.append(
            _fact(
                "account_owner.generation",
                account_owner.generation if account_owner.generation is not None else "unknown",
                source=account_owner.source or "account_artifacts",
                ts_ms=account_owner.recorded_at_ms,
            )
        )
    if account_freeze is not None:
        gate = account_freeze.to_gate_result()
        facts.append(
            _fact(
                "account_freeze",
                account_freeze.reason,
                source=account_freeze.source,
                gate_id=gate.gate_id,
                ts_ms=account_freeze.recorded_at_ms,
            )
        )
    for gate in _hard_blocking_readiness_gates(readiness_gates):
        facts.append(
            _fact(
                f"readiness.{gate.name}",
                gate.detail,
                source=gate.gate_result.source,
                gate_id=gate.gate_result.gate_id,
                ts_ms=gate.gate_result.evidence_at_ms,
            )
        )
    return facts


__all__ = ["author_submit_readiness", "author_trader_guidance", "build_submit_readiness_findings"]
