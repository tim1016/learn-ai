"""Account-scoped reconciliation receipts over Account Truth."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from pydantic import ValidationError

from app.broker.ibkr.account_truth_freshness import critical_source_freshness_blocks
from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountAuditedOverride,
    AccountFreezeEvidence,
    AccountRecoveryProof,
    account_artifacts_root,
    append_account_event,
    clear_account_freeze,
    read_account_events,
    read_account_freeze,
)
from app.engine.live.account_identity import InvalidAccountIdError, normalize_account_id
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    has_account_recovery_evidence_after,
    read_account_instance_registry,
)
from app.engine.live.exit_taxonomy import (
    CRASH_RETIRED_BINDING_SOURCES,
    ENDED_WITHOUT_STATUS_RETIRED_BINDING_SOURCES,
    LIVENESS_UNPROVEN_RETIRED_BINDING_SOURCES,
)
from app.schemas.account_reconciliation import (
    AccountAcceptExposureOverrideResponse,
    AccountClearFreezeResponse,
    AccountConditionOwner,
    AccountConditionRow,
    AccountExposureResolution,
    AccountFreezeBanner,
    AccountReconciliationEvidenceRef,
    AccountReconciliationReceipt,
    AccountTriageBotRef,
    AccountTriageGateRow,
    AccountTriageResponse,
)
from app.schemas.account_truth import AccountTruthResponse, AccountTruthSourceFreshness
from app.schemas.artifact_io import (
    atomic_write_pydantic_artifact,
    read_pydantic_artifact,
)
from app.schemas.live_runs import GateResult
from app.utils.timestamps import now_ms_utc

ACCOUNT_RECONCILIATION_RECEIPT_FILENAME = "account_reconciliation_receipt.json"
DEFAULT_ACCOUNT_RECONCILIATION_TTL_MS = 60_000
MAX_EVIDENCE_REF_DETAIL_LENGTH = 512
CLEARABLE_EXPOSURE_RESOLUTIONS = frozenset({"flat", "intended", "accepted_override"})


class AccountReconciliationService:
    """Persist and project account-level reconciliation receipts."""

    def __init__(self, *, artifacts_root: Path, ttl_ms: int = DEFAULT_ACCOUNT_RECONCILIATION_TTL_MS) -> None:
        self._artifacts_root = artifacts_root
        self._ttl_ms = ttl_ms

    def receipt_path(self, account_id: str) -> Path:
        return (
            account_artifacts_root(self._artifacts_root, normalize_account_id(account_id))
            / ACCOUNT_RECONCILIATION_RECEIPT_FILENAME
        )

    def read_latest_receipt(self, account_id: str) -> AccountReconciliationReceipt | None:
        return read_pydantic_artifact(self.receipt_path(account_id), AccountReconciliationReceipt)

    def write_receipt(
        self,
        *,
        requested_account_id: str,
        account_truth: AccountTruthResponse,
        now_ms: int | None = None,
    ) -> AccountReconciliationReceipt:
        generated_at_ms = now_ms_utc() if now_ms is None else now_ms
        receipt = self.compose_receipt(
            requested_account_id=requested_account_id,
            account_truth=account_truth,
            generated_at_ms=generated_at_ms,
        )
        atomic_write_pydantic_artifact(self.receipt_path(receipt.account_id), receipt)
        append_account_event(
            self._artifacts_root,
            receipt.account_id,
            {
                "event_type": "account_reconciliation_receipt_recorded",
                "receipt_id": receipt.receipt_id,
                "state": receipt.state,
                "account_truth_verdict": receipt.account_truth_verdict,
                "account_truth_severity": receipt.account_truth_severity,
                "final_gate_result": receipt.final_gate_result.model_dump(mode="json"),
                "recorded_at_ms": receipt.generated_at_ms,
                "expires_at_ms": receipt.expires_at_ms,
            },
        )
        return receipt

    def compose_receipt(
        self,
        *,
        requested_account_id: str,
        account_truth: AccountTruthResponse,
        generated_at_ms: int,
    ) -> AccountReconciliationReceipt:
        requested = normalize_account_id(requested_account_id)
        connected = _normalize_optional_account_id(account_truth.health.account_id)
        truth_account = _normalize_optional_account_id(account_truth.account_id)
        connected_matches = connected is not None and connected == requested
        truth_matches = truth_account is not None and truth_account == requested
        truth_fresh = account_truth.generated_at_ms <= generated_at_ms
        critical_source_blocks = critical_source_freshness_blocks(
            account_truth.source_freshness,
            checked_at_ms=generated_at_ms,
        )
        truth_clean = account_truth.final_verdict == "clean" and not critical_source_blocks
        exposure_resolution = _exposure_resolution(account_truth)
        exposure_resolved = exposure_resolution in CLEARABLE_EXPOSURE_RESOLUTIONS

        state = (
            "CLEAN"
            if connected_matches and truth_matches and truth_fresh and truth_clean and exposure_resolved
            else "NOT_PROVEN"
        )
        final_gate = _final_gate_result(
            requested_account_id=requested,
            connected_account_id=account_truth.health.account_id,
            account_truth=account_truth,
            generated_at_ms=generated_at_ms,
            state=state,
            connected_matches=connected_matches,
            truth_matches=truth_matches,
            truth_fresh=truth_fresh,
            critical_source_blocks=critical_source_blocks,
            exposure_resolution=exposure_resolution,
            exposure_resolved=exposure_resolved,
        )
        receipt_id = _receipt_id(
            requested_account_id=requested,
            account_truth=account_truth,
            generated_at_ms=generated_at_ms,
        )
        return AccountReconciliationReceipt(
            receipt_id=receipt_id,
            account_id=requested,
            requested_account_id=requested,
            connected_account_id=account_truth.health.account_id,
            state=state,
            account_truth_verdict=account_truth.final_verdict,
            account_truth_severity=account_truth.final_severity,
            final_gate_result=final_gate,
            exposure_resolution=exposure_resolution,
            account_truth=account_truth,
            evidence_refs=_evidence_refs(account_truth),
            generated_at_ms=generated_at_ms,
            account_truth_generated_at_ms=account_truth.generated_at_ms,
            expires_at_ms=generated_at_ms + self._ttl_ms,
            ttl_ms=self._ttl_ms,
        )

    def clear_freeze_from_latest_receipt(
        self,
        *,
        account_id: str,
        requested_by: str,
        receipt_id: str | None = None,
        reason: str | None = None,
        now_ms: int | None = None,
    ) -> AccountClearFreezeResponse:
        recorded_at_ms = now_ms_utc() if now_ms is None else now_ms
        canonical_account_id = normalize_account_id(account_id)
        freeze = read_account_freeze(self._artifacts_root, canonical_account_id)
        if freeze is None:
            raise AccountArtifactError(f"account freeze does not exist for {canonical_account_id!r}")
        receipt = self.read_latest_receipt(canonical_account_id)
        clear_blocker = _clear_freeze_blocker(
            account_id=canonical_account_id,
            receipt=receipt,
            freeze=freeze,
            now_ms=recorded_at_ms,
            receipt_id=receipt_id,
        )
        if clear_blocker is not None:
            raise AccountArtifactError(clear_blocker)

        recovery_id = _recovery_id(
            account_id=canonical_account_id,
            receipt=receipt,
            recorded_at_ms=recorded_at_ms,
        )
        broker_evidence: dict[str, object] = {
            "receipt_id": receipt.receipt_id,
            "account_truth_verdict": receipt.account_truth_verdict,
            "account_truth_severity": receipt.account_truth_severity,
            "account_truth_generated_at_ms": receipt.account_truth_generated_at_ms,
            "connected_account_id": receipt.connected_account_id,
            "exposure_resolution": receipt.exposure_resolution,
            "freeze_recorded_at_ms": freeze.recorded_at_ms,
            "evidence_refs": [ref.model_dump(mode="json") for ref in receipt.evidence_refs],
        }
        if reason:
            broker_evidence["reason"] = reason

        proof = AccountRecoveryProof(
            account_id=canonical_account_id,
            recovery_id=recovery_id,
            requested_action="reconcile",
            requested_by=requested_by,
            broker_evidence=broker_evidence,
            reconciliation_result="clean",
            final_gate_result=receipt.final_gate_result,
            recorded_at_ms=recorded_at_ms,
        )
        clear_account_freeze(self._artifacts_root, recovery_proof=proof)
        return AccountClearFreezeResponse(
            account_id=canonical_account_id,
            recovery_id=recovery_id,
            receipt_id=receipt.receipt_id,
            gate_result=receipt.final_gate_result,
            triage=self.triage(account_id=canonical_account_id, now_ms=recorded_at_ms),
        )

    def accept_exposure_override(
        self,
        *,
        account_id: str,
        requested_by: str,
        reason: str,
        strategy_instance_id: str | None = None,
        run_id: str | None = None,
        bot_order_namespace: str | None = None,
        now_ms: int | None = None,
    ) -> AccountAcceptExposureOverrideResponse:
        recorded_at_ms = now_ms_utc() if now_ms is None else now_ms
        canonical_account_id = normalize_account_id(account_id)
        freeze = read_account_freeze(self._artifacts_root, canonical_account_id)
        if freeze is None:
            raise AccountArtifactError(f"account freeze does not exist for {canonical_account_id!r}")
        if not _is_exposure_freeze(freeze):
            raise AccountArtifactError("audited exposure override can only clear an exposure freeze")

        latest_bindings = _latest_bindings(
            read_account_instance_registry(self._artifacts_root, canonical_account_id)
        )
        owner = _freeze_condition_owner(freeze, latest_bindings)
        override_strategy_instance_id = strategy_instance_id or owner.strategy_instance_id
        override_run_id = run_id or owner.run_id
        override_namespace = bot_order_namespace or _binding_namespace_for(
            latest_bindings,
            strategy_instance_id=override_strategy_instance_id,
            run_id=override_run_id,
        )
        receipt = self.read_latest_receipt(canonical_account_id)
        override_id = _exposure_override_id(
            account_id=canonical_account_id,
            recorded_at_ms=recorded_at_ms,
        )
        override = AccountAuditedOverride(
            account_id=canonical_account_id,
            override_id=override_id,
            approved_decision="continue",
            reason=reason,
            approved_by=requested_by,
            approved_at_ms=recorded_at_ms,
            valid_until_ms=recorded_at_ms + self._ttl_ms,
            prior_evidence={
                "freeze_reason": freeze.reason,
                "freeze_source": freeze.source,
                "freeze_recorded_at_ms": freeze.recorded_at_ms,
                "operator_next_step": freeze.operator_next_step,
                "receipt_id": receipt.receipt_id if receipt is not None else None,
                "receipt_state": receipt.state if receipt is not None else None,
                "exposure_resolution": (
                    receipt.exposure_resolution if receipt is not None else None
                ),
            },
            next_reconciliation_step=(
                "Run account reconciliation before allowing another start on this account."
            ),
            strategy_instance_id=override_strategy_instance_id,
            run_id=override_run_id,
            bot_order_namespace=override_namespace,
        )
        clear_account_freeze(
            self._artifacts_root,
            audited_override=override,
            now_ms=recorded_at_ms,
        )
        return AccountAcceptExposureOverrideResponse(
            account_id=canonical_account_id,
            override_id=override_id,
            triage=self.triage(account_id=canonical_account_id, now_ms=recorded_at_ms),
        )

    def triage(
        self,
        *,
        account_id: str,
        strategy_instance_id: str | None = None,
        now_ms: int | None = None,
    ) -> AccountTriageResponse:
        generated_at_ms = now_ms_utc() if now_ms is None else now_ms
        canonical_account_id = normalize_account_id(account_id)
        receipt = self.read_latest_receipt(canonical_account_id)
        bindings, registry_gap = _read_triage_registry(
            artifacts_root=self._artifacts_root,
            account_id=canonical_account_id,
            generated_at_ms=generated_at_ms,
        )
        account_events = read_account_events(self._artifacts_root, canonical_account_id)
        latest_bindings = _latest_bindings(bindings)
        active_bindings = [
            binding
            for binding in latest_bindings
            if binding.lifecycle_state in {"DEPLOYED", "ACTIVE"}
        ]
        freeze = read_account_freeze(self._artifacts_root, canonical_account_id)
        gate_rows: list[AccountTriageGateRow] = []

        reconciliation_gate = _reconciliation_triage_row(
            account_id=canonical_account_id,
            receipt=receipt,
            generated_at_ms=generated_at_ms,
            affected_bindings=active_bindings,
        )
        gate_rows.append(reconciliation_gate)
        if registry_gap is not None:
            gate_rows.append(registry_gap)
        if freeze is not None:
            gate_rows.append(
                AccountTriageGateRow(
                    gate_id="account.unresolved_exposure",
                    status="freeze",
                    scope="account",
                    severity="critical",
                    title="Account freeze active",
                    detail=freeze.reason,
                    operator_next_step=freeze.operator_next_step,
                    source=freeze.source,
                    evidence_at_ms=freeze.recorded_at_ms,
                    affected_strategy_instance_ids=[binding.strategy_instance_id for binding in active_bindings],
                    primary_remediation="open_account_monitor",
                )
            )
        conditions = _dedupe_conditions(
            [
                *_reconciliation_conditions(
                    account_id=canonical_account_id,
                    receipt=receipt,
                    generated_at_ms=generated_at_ms,
                ),
                *_terminal_binding_conditions(
                    latest_bindings,
                    account_events=account_events,
                ),
                *(
                    _freeze_conditions(
                        freeze,
                        active_bindings=active_bindings,
                        latest_bindings=latest_bindings,
                    )
                    if freeze is not None
                    else []
                ),
            ]
        )

        overall = _overall_gate(
            gate_rows,
            conditions,
            account_id=canonical_account_id,
            generated_at_ms=generated_at_ms,
        )
        return AccountTriageResponse(
            generated_at_ms=generated_at_ms,
            account_id=canonical_account_id,
            strategy_instance_id=strategy_instance_id,
            summary_headline="Account recovery gates passing"
            if overall.status == "pass"
            else "Account recovery needs attention",
            summary_detail=overall.operator_reason,
            overall_gate_result=overall,
            account_reconciliation_receipt=receipt,
            gate_rows=gate_rows,
            conditions=conditions,
            freeze_banner=_freeze_banner(freeze),
            clear_freeze_actionable=_clear_freeze_actionable(
                receipt=receipt,
                freeze=freeze,
                now_ms=generated_at_ms,
            ),
            affected_bots=[
                AccountTriageBotRef(
                    strategy_instance_id=binding.strategy_instance_id,
                    run_id=binding.run_id,
                    bot_order_namespace=binding.bot_order_namespace,
                    lifecycle_state=binding.lifecycle_state,
                )
                for binding in active_bindings
            ],
        )


def _normalize_optional_account_id(account_id: str | None) -> str | None:
    if account_id is None:
        return None
    try:
        return normalize_account_id(account_id)
    except InvalidAccountIdError:
        return None


def _receipt_id(
    *,
    requested_account_id: str,
    account_truth: AccountTruthResponse,
    generated_at_ms: int,
) -> str:
    digest = hashlib.sha256(
        f"{requested_account_id}:{account_truth.generated_at_ms}:{account_truth.final_verdict}:{generated_at_ms}".encode()
    ).hexdigest()[:16]
    return f"acct-recon-{requested_account_id}-{generated_at_ms}-{digest}"


def _recovery_id(
    *,
    account_id: str,
    receipt: AccountReconciliationReceipt,
    recorded_at_ms: int,
) -> str:
    digest = hashlib.sha256(f"{receipt.receipt_id}:{recorded_at_ms}".encode()).hexdigest()[:8]
    return f"acct-recovery-{account_id}-{recorded_at_ms}-{digest}-{uuid.uuid4().hex[:8]}"


def _exposure_override_id(*, account_id: str, recorded_at_ms: int) -> str:
    return f"acct-exposure-override-{account_id}-{recorded_at_ms}-{uuid.uuid4().hex[:8]}"


def _binding_namespace_for(
    bindings: list[AccountInstanceBinding],
    *,
    strategy_instance_id: str | None,
    run_id: str | None,
) -> str | None:
    for binding in bindings:
        if strategy_instance_id is not None and binding.strategy_instance_id != strategy_instance_id:
            continue
        if run_id is not None and binding.run_id != run_id:
            continue
        return binding.bot_order_namespace
    return None


def _final_gate_result(
    *,
    requested_account_id: str,
    connected_account_id: str | None,
    account_truth: AccountTruthResponse,
    generated_at_ms: int,
    state: str,
    connected_matches: bool,
    truth_matches: bool,
    truth_fresh: bool,
    critical_source_blocks: tuple[AccountTruthSourceFreshness, ...],
    exposure_resolution: AccountExposureResolution,
    exposure_resolved: bool,
) -> GateResult:
    if state == "CLEAN":
        return GateResult(
            gate_id="account.reconciliation",
            status="pass",
            source="account_reconciliation_receipt",
            operator_reason="Account Truth is clean and broker account scope is proven.",
            operator_next_step="ACCOUNT_CLEAN",
            evidence_at_ms=generated_at_ms,
        )
    if not connected_matches:
        reason = (
            f"Connected broker account {connected_account_id or 'unknown'} does not match "
            f"requested account {requested_account_id}."
        )
        next_step = "CONNECT_EXPECTED_BROKER_ACCOUNT"
    elif not truth_matches:
        reason = (
            f"Account Truth account {account_truth.account_id or 'unknown'} does not match "
            f"requested account {requested_account_id}."
        )
        next_step = "REFRESH_ACCOUNT_TRUTH"
    elif not truth_fresh:
        reason = "Account Truth was generated after the receipt clock and cannot be receipted safely."
        next_step = "REFRESH_ACCOUNT_TRUTH"
    elif critical_source_blocks:
        reason = critical_source_blocks[0].message
        next_step = "REFRESH_ACCOUNT_TRUTH"
    elif not exposure_resolved:
        reason = (
            "Account Truth is otherwise clean, but broker exposure is not flat. "
            f"exposure_resolution={exposure_resolution}."
        )
        next_step = "RESOLVE_EXPOSURE"
    else:
        reason = account_truth.status_detail
        next_step = "OPEN_ACCOUNT_MONITOR"
    return GateResult(
        gate_id="account.reconciliation",
        status="block",
        source="account_reconciliation_receipt",
        operator_reason=reason,
        operator_next_step=next_step,
        evidence_at_ms=generated_at_ms,
    )


def _exposure_resolution(account_truth: AccountTruthResponse) -> AccountExposureResolution:
    for row in account_truth.positions:
        if row.quantity != 0:
            return "unresolved"
    for row in account_truth.symbol_exposures:
        if row.quantity != 0:
            return "unresolved"
    return "flat"


def _evidence_refs(account_truth: AccountTruthResponse) -> list[AccountReconciliationEvidenceRef]:
    refs = [
        AccountReconciliationEvidenceRef(
            source="account_truth",
            ref=f"account_truth:{account_truth.generated_at_ms}",
            detail=_evidence_detail(account_truth.status_label),
        )
    ]
    for blocker in account_truth.blockers:
        refs.append(
            AccountReconciliationEvidenceRef(
                source="account_truth.blocker",
                ref=blocker.code,
                detail=_evidence_detail(blocker.title),
            )
        )
    for gap in account_truth.evidence_gaps:
        refs.append(
            AccountReconciliationEvidenceRef(
                source="account_truth.evidence_gap",
                ref=gap.source,
                detail=_evidence_detail(gap.message),
            )
        )
    return refs


def _evidence_detail(detail: str | None) -> str | None:
    if detail is None or len(detail) <= MAX_EVIDENCE_REF_DETAIL_LENGTH:
        return detail
    return f"{detail[: MAX_EVIDENCE_REF_DETAIL_LENGTH - 3]}..."


def _read_triage_registry(
    *,
    artifacts_root: Path,
    account_id: str,
    generated_at_ms: int,
) -> tuple[list[AccountInstanceBinding], AccountTriageGateRow | None]:
    try:
        return read_account_instance_registry(artifacts_root, account_id), None
    except (AccountArtifactError, OSError, json.JSONDecodeError, ValidationError) as exc:
        return [], AccountTriageGateRow(
            gate_id="account.instance_registry",
            status="unknown",
            scope="account",
            severity="warning",
            title="Account instance registry unavailable",
            detail=f"Account instance registry for {account_id} could not be read: {exc}",
            operator_next_step="REPAIR_ACCOUNT_INSTANCE_REGISTRY",
            source="instance_registry",
            evidence_at_ms=generated_at_ms,
            primary_remediation="open_account_monitor",
        )


def _latest_bindings(bindings: list[AccountInstanceBinding]) -> list[AccountInstanceBinding]:
    latest: dict[str, AccountInstanceBinding] = {}
    for binding in sorted(bindings, key=lambda b: b.recorded_at_ms):
        latest[binding.strategy_instance_id] = binding
    return sorted(latest.values(), key=lambda b: b.strategy_instance_id)


def _account_owner(account_id: str) -> AccountConditionOwner:
    return AccountConditionOwner(
        owner_type="account",
        owner_id=account_id,
        label=f"Account {account_id}",
    )


def _bot_owner(binding: AccountInstanceBinding) -> AccountConditionOwner:
    return AccountConditionOwner(
        owner_type="bot",
        owner_id=binding.strategy_instance_id,
        label=f"Bot {binding.strategy_instance_id}",
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        lifecycle_state=binding.lifecycle_state,
    )


def _reconciliation_conditions(
    *,
    account_id: str,
    receipt: AccountReconciliationReceipt | None,
    generated_at_ms: int,
) -> list[AccountConditionRow]:
    if receipt is None:
        return [
            AccountConditionRow(
                condition_type="evidence_stale",
                scope="account",
                owner=_account_owner(account_id),
                severity="warning",
                title="Account evidence not yet proven",
                detail=f"No account-level reconciliation receipt exists for {account_id}.",
                operator_next_step="RUN_ACCOUNT_RECONCILIATION",
                source="account_reconciliation_receipt",
                evidence_at_ms=generated_at_ms,
                cure_action="reconcile_now",
            )
        ]
    if receipt.expires_at_ms < generated_at_ms:
        return [
            AccountConditionRow(
                condition_type="evidence_stale",
                scope="account",
                owner=_account_owner(account_id),
                severity="warning",
                title="Account evidence stale",
                detail=f"Receipt {receipt.receipt_id} expired before this triage snapshot.",
                operator_next_step="RUN_ACCOUNT_RECONCILIATION",
                source="account_reconciliation_receipt",
                evidence_at_ms=receipt.generated_at_ms,
                evidence_refs=receipt.evidence_refs,
                cure_action="reconcile_now",
            )
        ]
    return []


def _terminal_binding_conditions(
    bindings: list[AccountInstanceBinding],
    *,
    account_events: list[dict],
) -> list[AccountConditionRow]:
    conditions: list[AccountConditionRow] = []
    for binding in bindings:
        if binding.lifecycle_state != "RETIRED":
            continue
        if binding.source not in (
            CRASH_RETIRED_BINDING_SOURCES
            | ENDED_WITHOUT_STATUS_RETIRED_BINDING_SOURCES
            | LIVENESS_UNPROVEN_RETIRED_BINDING_SOURCES
        ):
            continue
        if has_account_recovery_evidence_after(account_events, binding.recorded_at_ms):
            continue
        if binding.source in CRASH_RETIRED_BINDING_SOURCES:
            conditions.append(
                AccountConditionRow(
                    condition_type="crashed",
                    scope="bot",
                    owner=_bot_owner(binding),
                    severity="critical",
                    title="Bot crashed",
                    detail=(
                        f"{binding.strategy_instance_id} ended from a crash in run {binding.run_id}. "
                        "Retire & Replace preserves the history and creates a clean replacement."
                    ),
                    operator_next_step="RETIRE_REPLACE",
                    source=binding.source,
                    evidence_at_ms=binding.recorded_at_ms,
                    evidence_refs=_binding_evidence_refs(binding),
                    cure_action="retire_replace",
                )
            )
        elif binding.source in LIVENESS_UNPROVEN_RETIRED_BINDING_SOURCES:
            conditions.append(
                AccountConditionRow(
                    condition_type="liveness_unproven",
                    scope="bot",
                    owner=_bot_owner(binding),
                    severity="critical",
                    title="Bot liveness unproven after daemon boot",
                    detail=(
                        f"{binding.strategy_instance_id} was ACTIVE in run {binding.run_id}, "
                        "but the current host daemon does not own its process. The binding was "
                        "retired before it could be trusted as a live sibling."
                    ),
                    operator_next_step="RETIRE_REPLACE",
                    source=binding.source,
                    evidence_at_ms=binding.recorded_at_ms,
                    evidence_refs=_binding_evidence_refs(binding),
                    cure_action="retire_replace",
                )
            )
        elif binding.source in ENDED_WITHOUT_STATUS_RETIRED_BINDING_SOURCES:
            conditions.append(
                AccountConditionRow(
                    condition_type="ended_without_status",
                    scope="bot",
                    owner=_bot_owner(binding),
                    severity="critical",
                    title="Bot ended without status",
                    detail=(
                        f"{binding.strategy_instance_id} exited without a run-status receipt for "
                        f"run {binding.run_id}. Retire & Replace is required."
                    ),
                    operator_next_step="RETIRE_REPLACE",
                    source=binding.source,
                    evidence_at_ms=binding.recorded_at_ms,
                    evidence_refs=_binding_evidence_refs(binding),
                    cure_action="retire_replace",
                )
            )
    return conditions


def _binding_evidence_refs(binding: AccountInstanceBinding) -> list[AccountReconciliationEvidenceRef]:
    return [
        AccountReconciliationEvidenceRef(
            source="account_instance_registry",
            ref=f"{binding.strategy_instance_id}:{binding.run_id}",
            detail=binding.source,
        )
    ]


def _freeze_conditions(
    freeze: AccountFreezeEvidence,
    *,
    active_bindings: list[AccountInstanceBinding],
    latest_bindings: list[AccountInstanceBinding],
) -> list[AccountConditionRow]:
    condition_type = "exposure_freeze" if _is_exposure_freeze(freeze) else "account_freeze"
    cure_action = "resolve_exposure" if condition_type == "exposure_freeze" else "clear_freeze"
    return [
        AccountConditionRow(
            condition_type=condition_type,
            scope="account",
            owner=_freeze_condition_owner(freeze, latest_bindings),
            severity="critical",
            title="Account freeze active",
            detail=freeze.reason,
            operator_next_step=freeze.operator_next_step,
            source=freeze.source,
            evidence_at_ms=freeze.recorded_at_ms,
            affected_strategy_instance_ids=[
                binding.strategy_instance_id for binding in active_bindings
            ],
            cure_action=cure_action,
        )
    ]


def _freeze_condition_owner(
    freeze: AccountFreezeEvidence,
    latest_bindings: list[AccountInstanceBinding],
) -> AccountConditionOwner:
    if not _is_exposure_freeze(freeze):
        return _account_owner(freeze.account_id)
    candidates = [
        binding
        for binding in latest_bindings
        if binding.account_id == freeze.account_id
    ]
    if len(candidates) == 1:
        return _bot_owner(candidates[0])
    active_candidates = [
        binding
        for binding in candidates
        if binding.lifecycle_state in {"DEPLOYED", "ACTIVE"}
    ]
    if len(active_candidates) == 1:
        return _bot_owner(active_candidates[0])
    retired_after_freeze = [
        binding
        for binding in candidates
        if binding.lifecycle_state == "RETIRED"
        and binding.recorded_at_ms >= freeze.recorded_at_ms
    ]
    if len(retired_after_freeze) == 1:
        return _bot_owner(retired_after_freeze[0])
    return _account_owner(freeze.account_id)


def _freeze_banner(freeze: AccountFreezeEvidence | None) -> AccountFreezeBanner | None:
    if freeze is None:
        return None
    if _is_exposure_freeze(freeze):
        detail = "Resolve or audit broker exposure before starting another bot on this account."
    else:
        detail = "Run account reconciliation and clear the active account freeze before deploying."
    return AccountFreezeBanner(
        headline="Account sick bay is gating new starts.",
        detail=detail,
    )


def _dedupe_conditions(conditions: list[AccountConditionRow]) -> list[AccountConditionRow]:
    deduped: dict[tuple[str, str], AccountConditionRow] = {}
    for condition in conditions:
        key = (condition.condition_type, condition.owner.owner_id)
        current = deduped.get(key)
        if current is None or condition.evidence_at_ms > current.evidence_at_ms:
            deduped[key] = condition
    return sorted(deduped.values(), key=lambda row: (row.scope, row.condition_type, row.owner.owner_id))


def _is_exposure_freeze(freeze: AccountFreezeEvidence) -> bool:
    return freeze.freeze_kind == "exposure"


def _clear_freeze_blocker(
    *,
    receipt: AccountReconciliationReceipt | None,
    freeze: AccountFreezeEvidence | None,
    now_ms: int,
    account_id: str | None = None,
    receipt_id: str | None = None,
) -> str | None:
    if freeze is None:
        return (
            f"account freeze does not exist for {account_id!r}"
            if account_id is not None
            else "account freeze does not exist"
        )
    if receipt is None:
        return (
            f"account reconciliation receipt not found for {account_id!r}"
            if account_id is not None
            else "account reconciliation receipt not found"
        )
    if account_id is not None and receipt.account_id != account_id:
        return "latest account receipt belongs to a different account"
    if receipt.account_id != freeze.account_id:
        return "latest account receipt belongs to a different account"
    if receipt_id is not None and receipt.receipt_id != receipt_id:
        return "submitted receipt_id does not match the latest account receipt"
    if receipt.expires_at_ms < now_ms:
        return "account reconciliation receipt is stale"
    if receipt.generated_at_ms <= freeze.recorded_at_ms:
        return "account reconciliation receipt must be newer than the active freeze"
    if _is_exposure_freeze(freeze) and receipt.exposure_resolution not in CLEARABLE_EXPOSURE_RESOLUTIONS:
        return "exposure freeze cannot clear while exposure resolution is unresolved"
    if receipt.state != "CLEAN" or receipt.final_gate_result.status != "pass":
        return "account reconciliation receipt must be clean and passing"
    return None


def _clear_freeze_actionable(
    *,
    receipt: AccountReconciliationReceipt | None,
    freeze: AccountFreezeEvidence | None,
    now_ms: int,
) -> bool:
    return _clear_freeze_blocker(receipt=receipt, freeze=freeze, now_ms=now_ms) is None


def _reconciliation_triage_row(
    *,
    account_id: str,
    receipt: AccountReconciliationReceipt | None,
    generated_at_ms: int,
    affected_bindings: list[AccountInstanceBinding],
) -> AccountTriageGateRow:
    affected = [binding.strategy_instance_id for binding in affected_bindings]
    if receipt is None:
        return AccountTriageGateRow(
            gate_id="account.reconciliation",
            status="unknown",
            scope="reconciliation",
            severity="warning",
            title="Account reconciliation receipt missing",
            detail=f"No account-level reconciliation receipt exists for {account_id}.",
            operator_next_step="RUN_ACCOUNT_RECONCILIATION",
            source="account_reconciliation_receipt",
            evidence_at_ms=generated_at_ms,
            affected_strategy_instance_ids=affected,
            primary_remediation="refresh_account_truth",
        )
    if receipt.expires_at_ms < generated_at_ms:
        return AccountTriageGateRow(
            gate_id="account.reconciliation",
            status="unknown",
            scope="reconciliation",
            severity="warning",
            title="Account reconciliation receipt stale",
            detail=(
                f"Receipt {receipt.receipt_id} expired before this triage snapshot."
            ),
            operator_next_step="RUN_ACCOUNT_RECONCILIATION",
            source="account_reconciliation_receipt",
            evidence_at_ms=receipt.generated_at_ms,
            affected_strategy_instance_ids=affected,
            evidence_refs=receipt.evidence_refs,
            primary_remediation="refresh_account_truth",
        )
    return AccountTriageGateRow(
        gate_id="account.reconciliation",
        status=receipt.final_gate_result.status,
        scope="reconciliation",
        severity="ok" if receipt.final_gate_result.status == "pass" else "critical",
        title="Account reconciliation clean"
        if receipt.final_gate_result.status == "pass"
        else "Account reconciliation not proven",
        detail=receipt.final_gate_result.operator_reason,
        operator_next_step=receipt.final_gate_result.operator_next_step,
        source=receipt.final_gate_result.source,
        evidence_at_ms=receipt.final_gate_result.evidence_at_ms,
        affected_strategy_instance_ids=affected,
        evidence_refs=receipt.evidence_refs,
        primary_remediation=None
        if receipt.final_gate_result.status == "pass"
        else "open_reconciliation",
    )


def _overall_gate(
    gate_rows: list[AccountTriageGateRow],
    conditions: list[AccountConditionRow],
    *,
    account_id: str,
    generated_at_ms: int,
) -> GateResult:
    for status in ("freeze", "poison", "block", "unknown"):
        row = next((candidate for candidate in gate_rows if candidate.status == status), None)
        if row is not None:
            return GateResult(
                gate_id="account.triage",
                status=row.status,
                source=row.source,
                operator_reason=row.detail,
                operator_next_step=row.operator_next_step,
                evidence_at_ms=row.evidence_at_ms,
            )
    condition = next(
        (
            candidate
            for severity in ("critical", "warning")
            for candidate in conditions
            if candidate.severity == severity
        ),
        None,
    )
    if condition is not None:
        return GateResult(
            gate_id="account.triage",
            status="block" if condition.severity == "critical" else "unknown",
            source=condition.source,
            operator_reason=condition.detail,
            operator_next_step=condition.operator_next_step,
            evidence_at_ms=condition.evidence_at_ms,
        )
    return GateResult(
        gate_id="account.triage",
        status="pass",
        source="account_triage",
        operator_reason=f"Account {account_id} has no blocking account triage rows.",
        operator_next_step="ACCOUNT_TRIAGE_PASSING",
        evidence_at_ms=generated_at_ms,
    )
