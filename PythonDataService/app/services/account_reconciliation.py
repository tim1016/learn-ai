"""Account-scoped reconciliation receipts over Account Truth."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.engine.live.account_artifacts import (
    AccountInstanceBinding,
    account_artifacts_root,
    append_account_event,
    read_account_freeze,
    read_account_instance_registry,
)
from app.engine.live.account_identity import InvalidAccountIdError, normalize_account_id
from app.schemas.account_reconciliation import (
    AccountReconciliationEvidenceRef,
    AccountReconciliationReceipt,
    AccountTriageBotRef,
    AccountTriageGateRow,
    AccountTriageResponse,
)
from app.schemas.account_truth import AccountTruthResponse
from app.schemas.artifact_io import (
    atomic_write_pydantic_artifact,
    read_pydantic_artifact,
)
from app.schemas.live_runs import GateResult
from app.utils.timestamps import now_ms_utc

ACCOUNT_RECONCILIATION_RECEIPT_FILENAME = "account_reconciliation_receipt.json"
DEFAULT_ACCOUNT_RECONCILIATION_TTL_MS = 60_000


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
        truth_clean = account_truth.final_verdict == "clean"

        state = "CLEAN" if connected_matches and truth_matches and truth_fresh and truth_clean else "NOT_PROVEN"
        final_gate = _final_gate_result(
            requested_account_id=requested,
            connected_account_id=account_truth.health.account_id,
            account_truth=account_truth,
            generated_at_ms=generated_at_ms,
            state=state,
            connected_matches=connected_matches,
            truth_matches=truth_matches,
            truth_fresh=truth_fresh,
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
            account_truth=account_truth,
            evidence_refs=_evidence_refs(account_truth),
            generated_at_ms=generated_at_ms,
            account_truth_generated_at_ms=account_truth.generated_at_ms,
            expires_at_ms=generated_at_ms + self._ttl_ms,
            ttl_ms=self._ttl_ms,
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
        bindings = read_account_instance_registry(self._artifacts_root, canonical_account_id)
        active_bindings = [
            binding
            for binding in _latest_bindings(bindings)
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

        overall = _overall_gate(
            gate_rows,
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


def _evidence_refs(account_truth: AccountTruthResponse) -> list[AccountReconciliationEvidenceRef]:
    refs = [
        AccountReconciliationEvidenceRef(
            source="account_truth",
            ref=f"account_truth:{account_truth.generated_at_ms}",
            detail=account_truth.status_label,
        )
    ]
    for blocker in account_truth.blockers:
        refs.append(
            AccountReconciliationEvidenceRef(
                source="account_truth.blocker",
                ref=blocker.code,
                detail=blocker.title,
            )
        )
    for gap in account_truth.evidence_gaps:
        refs.append(
            AccountReconciliationEvidenceRef(
                source="account_truth.evidence_gap",
                ref=gap.source,
                detail=gap.message,
            )
        )
    return refs


def _latest_bindings(bindings: list[AccountInstanceBinding]) -> list[AccountInstanceBinding]:
    latest: dict[str, AccountInstanceBinding] = {}
    for binding in sorted(bindings, key=lambda b: b.recorded_at_ms):
        latest[binding.strategy_instance_id] = binding
    return sorted(latest.values(), key=lambda b: b.strategy_instance_id)


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
    return GateResult(
        gate_id="account.triage",
        status="pass",
        source="account_triage",
        operator_reason=f"Account {account_id} has no blocking account triage rows.",
        operator_next_step="ACCOUNT_TRIAGE_PASSING",
        evidence_at_ms=generated_at_ms,
    )
