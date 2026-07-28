"""Broker-free composition of existing account safety authorities."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from app.broker.ibkr.account_truth_freshness import normalize_source_freshness
from app.engine.live.account_artifacts import account_artifact_file_path
from app.engine.live.account_clerk_journal import (
    fold_account_clerk_custody_statuses,
    read_account_clerk_durability_spine,
)
from app.engine.live.account_clerk_journal_models import (
    AccountClerkJournalCorruptError,
)
from app.engine.live.account_epoch import ACCOUNT_EPOCH_FILENAME, AccountEpochState
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.account_observation_lease import assess_account_observation_lease
from app.engine.live.account_safety import (
    AccountSafetyAuthority,
    AccountSafetyCorruptError,
    AccountSafetyState,
    AccountSafetyVerdict,
    account_safety_path,
)
from app.schemas.account_directory import AccountServiceStatusResponse
from app.schemas.account_reconciliation import AccountReconciliationEvidenceRef, AccountReconciliationReceipt
from app.schemas.account_safety_snapshot import (
    AccountSafetySnapshot,
    AccountSafetySnapshotCustody,
    AccountSafetySnapshotExposure,
    AccountSafetySnapshotSource,
    AccountSafetySnapshotVerdict,
    PresentedOperatorAction,
    PresentedOperatorActionPrecondition,
    PresentedOperatorActionTarget,
    issue_presented_operator_action_token,
    presented_operator_action_signing_available,
)
from app.schemas.account_truth import AccountTruthResponse
from app.schemas.artifact_io import read_pydantic_artifact
from app.schemas.operator_blocker import OperatorConfirmationCopy
from app.services.account_directory import AccountDirectoryService
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_snapshot import (
    AccountTruthSnapshot,
    AccountTruthUnavailable,
    get_account_truth_snapshot_provider,
)
from app.utils.timestamps import now_ms_utc

_CUSTODY_STAGES = (
    "A0_CUSTODY_ACCEPTED",
    "A1_BROKER_WRITE_STARTED",
    "A2_BROKER_KNOWN",
    "A3_ECONOMIC_TERMINAL",
)
_PRESENTED_ACTION_TTL_MS = 60_000
_VERDICT_REASONS = {
    "ACCOUNT_SAFETY_CLEAN": "Current critical evidence proves the managed account state.",
    "ACCOUNT_SAFETY_CONTAMINATED": "Exposure or order attribution is foreign or cannot be safely proven.",
    "ACCOUNT_SAFETY_SUSPENDED": "Retired-owner exposure remains under account custody until reconciliation completes.",
    "ACCOUNT_RECONCILIATION_REQUIRED": "The current account epoch is invalid and proof is being rebuilt.",
    "ACCOUNT_RECONCILIATION_NOT_PROVEN": "The latest reconciliation receipt does not prove a clean account state.",
    "ACCOUNT_TRUTH_NOT_PROVEN": "Cached Account Truth is fresh but does not prove a clean managed account state.",
    "CRITICAL_SAFETY_EVIDENCE_UNAVAILABLE": "Required account safety evidence is unavailable.",
    "CRITICAL_SAFETY_EVIDENCE_STALE": "Required account safety evidence is older than its safety threshold.",
    "CRITICAL_SAFETY_EPOCH_UNPROVEN": "Critical evidence does not belong to the current account epoch.",
    "CRITICAL_SAFETY_IDENTITY_MISMATCH": "Critical safety evidence belongs to a different account or Clerk generation.",
}


class AccountSafetySnapshotService:
    """Assemble current authority outputs without polling or persisting facts."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        directory: AccountDirectoryService,
        now_ms: Callable[[], int] = now_ms_utc,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._directory = directory
        self._now_ms = now_ms
        self._reconciliation = AccountReconciliationService(artifacts_root=artifacts_root)

    def snapshot(self, *, account_id: str) -> AccountSafetySnapshot:
        now_ms = self._now_ms()
        account_id = normalize_account_id(account_id)
        clerk = self._directory.service_status(account_id=account_id)
        posture = self._directory.effective_posture(account_id=account_id)
        custody, custody_read_failure = self._custody_summary(account_id, clerk)
        receipt = self._reconciliation.read_latest_receipt(account_id)
        truth_evidence = get_account_truth_snapshot_provider().get(account_id)
        safety, safety_source = self._safety(account_id, now_ms)
        epoch = read_pydantic_artifact(
            account_artifact_file_path(self._artifacts_root, account_id, ACCOUNT_EPOCH_FILENAME),
            AccountEpochState,
        )
        truth = truth_evidence.truth if isinstance(truth_evidence, AccountTruthSnapshot) else None
        truth_identity_matches = self._truth_identity_matches(truth, account_id)
        truth_sources = () if truth is None else tuple(
            AccountSafetySnapshotSource(
                source=f"account_truth.{freshness.source}",
                state=(
                    "FRESH"
                    if freshness.status == "fresh"
                    else "STALE"
                    if freshness.status == "stale"
                    else "UNAVAILABLE"
                ),
                critical=freshness.severity == "critical",
                epoch_relation=self._clock_epoch_relation(
                    freshness.fetched_at_ms,
                    account_id=account_id,
                    epoch=epoch,
                    identity_matches=truth_identity_matches,
                ),
                as_of_ms=freshness.fetched_at_ms,
                age_ms=freshness.age_ms,
                hard_ttl_ms=freshness.hard_ttl_ms,
                reason_code=(
                    freshness.reason_code
                    or f"ACCOUNT_TRUTH_{freshness.source.upper()}_{freshness.status.upper()}"
                ),
            )
            for freshness in normalize_source_freshness(
                truth.source_freshness,
                checked_at_ms=now_ms,
            )
        )
        sources = (
            self._truth_source(
                truth_evidence,
                account_id=account_id,
                epoch=epoch,
                now_ms=now_ms,
                identity_matches=truth_identity_matches,
            ),
            *truth_sources,
            self._reconciliation_source(receipt, account_id=account_id, epoch=epoch, now_ms=now_ms),
            self._safety_source(
                safety,
                safety_source,
                account_id=account_id,
                epoch=epoch,
            ),
            self._epoch_source(epoch, account_id=account_id, now_ms=now_ms),
            self._lease_source(account_id, epoch=epoch, now_ms=now_ms),
            self._clerk_source(
                clerk,
                account_id=account_id,
                epoch=epoch,
                now_ms=now_ms,
                custody_read_failure=custody_read_failure,
            ),
        )
        sources = tuple(
            self._with_evidence_refs(source, account_id=account_id, receipt=receipt)
            for source in sources
        )
        verdict, reason_code = self._verdict(
            safety.verdict if safety is not None else None,
            epoch,
            receipt,
            truth,
            sources,
        )
        evidence_refs = self._evidence_refs(sources)
        semantic = {
            "account_id": account_id,
            "posture": posture,
            "verdict": verdict,
            "reason_code": reason_code,
            "verdict_reason": self._verdict_reason(reason_code),
            "epoch": None if epoch is None else epoch.current_epoch.model_dump(mode="json"),
            "reconciliation_id": None if receipt is None else receipt.receipt_id,
            "reconciliation_state": None if receipt is None else receipt.state,
            "sources": [source.model_dump(exclude={"age_ms"}, mode="json") for source in sources],
            "custody": {
                "retired_owner_custody_count": 0 if safety is None or safety.suspension is None else len(safety.suspension.custody),
                "clerk_journal_last_seq": clerk.journal.last_seq,
                **custody,
            },
            "exposure": {
                "open_order_count": 0 if truth is None else len(truth.orders),
                "execution_count": 0 if truth is None else len(truth.executions),
                "position_count": 0 if truth is None else len(truth.positions),
                "unmanaged_or_unknown_count": self._unmanaged_or_unknown_exposure_count(truth),
            },
            "blockers": [blocker.model_dump(mode="json") for blocker in (truth.operator_blockers if truth else [])],
            "outage_diff": None if epoch is None else epoch.outage_diff,
            "evidence_refs": [ref.model_dump(mode="json") for ref in evidence_refs],
        }
        fingerprint = hashlib.sha256(json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        actions = self._presented_actions(
            account_id=account_id,
            snapshot_id=fingerprint,
            generated_at_ms=now_ms,
            verdict=verdict,
            reason_code=reason_code,
            evidence_refs=evidence_refs,
        )
        return AccountSafetySnapshot(
            snapshot_version=fingerprint,
            snapshot_id=fingerprint,
            generated_at_ms=now_ms,
            account_id=account_id,
            posture=posture,
            verdict=verdict,
            reason_code=reason_code,
            verdict_reason=self._verdict_reason(reason_code),
            account_epoch=None if epoch is None else epoch.current_epoch,
            reconciliation_id=None if receipt is None else receipt.receipt_id,
            reconciliation_state=None if receipt is None else receipt.state,
            sources=sources,
            custody=AccountSafetySnapshotCustody(
                retired_owner_custody_count=0 if safety is None or safety.suspension is None else len(safety.suspension.custody),
                clerk_journal_last_seq=clerk.journal.last_seq,
                **custody,
            ),
            exposure=AccountSafetySnapshotExposure(
                open_order_count=0 if truth is None else len(truth.orders),
                execution_count=0 if truth is None else len(truth.executions),
                position_count=0 if truth is None else len(truth.positions),
                unmanaged_or_unknown_count=self._unmanaged_or_unknown_exposure_count(truth),
            ),
            blockers=() if truth is None else tuple(truth.operator_blockers),
            outage_diff=None if epoch is None else epoch.outage_diff,
            evidence_refs=evidence_refs,
            actions=actions,
        )

    @staticmethod
    def _presented_actions(
        *,
        account_id: str,
        snapshot_id: str,
        generated_at_ms: int,
        verdict: AccountSafetySnapshotVerdict,
        reason_code: str,
        evidence_refs: tuple[AccountReconciliationEvidenceRef, ...],
    ) -> tuple[PresentedOperatorAction, ...]:
        """Present the one migrated, account-safe recovery action.

        The deterministic key is bound to the safety fingerprint, so a retry
        of this exact presentation cannot start a second reconciliation. A
        changed snapshot gets a new key and must pass fresh preconditions.
        """

        signing_available = presented_operator_action_signing_available()
        available = verdict == "RECONCILING" and signing_available
        confirmation = OperatorConfirmationCopy(
            title="Run account reconciliation",
            body="Request a fresh account reconciliation for this account.",
            consequence="The server will revalidate this snapshot before it refreshes account evidence.",
            confirm_label="Run account reconcile",
        )
        idempotency_key = hashlib.sha256(
            f"reconcile_now:{account_id}:{snapshot_id}".encode()
        ).hexdigest()
        issued_at_ms = generated_at_ms
        expires_at_ms = issued_at_ms + _PRESENTED_ACTION_TTL_MS
        target = PresentedOperatorActionTarget(account_id=account_id)
        return (
            PresentedOperatorAction(
                action_id="reconcile_now",
                target=target,
                snapshot_id=snapshot_id,
                snapshot_version=snapshot_id,
                evidence_refs=evidence_refs,
                effect_class="EVIDENCE_REFRESH",
                idempotency_key=idempotency_key,
                issued_at_ms=issued_at_ms,
                expires_at_ms=expires_at_ms,
                presentation_token=(
                    issue_presented_operator_action_token(
                        action_id="reconcile_now",
                        target=target,
                        snapshot_id=snapshot_id,
                        snapshot_version=snapshot_id,
                        idempotency_key=idempotency_key,
                        issued_at_ms=issued_at_ms,
                        expires_at_ms=expires_at_ms,
                    )
                    if signing_available
                    else None
                ),
                preconditions=(
                    PresentedOperatorActionPrecondition(
                        code="account_safety.verdict",
                        expected_value="RECONCILING",
                    ),
                    PresentedOperatorActionPrecondition(
                        code="account_safety.reason_code",
                        expected_value=reason_code,
                    ),
                ),
                confirmation=confirmation,
                availability="AVAILABLE" if available else "UNAVAILABLE",
                disposition="fix_here" if available else "wait",
                finished_copy="Reconciliation request accepted. Refresh account safety evidence for its observed result.",
            ),
        )

    def _safety(
        self,
        account_id: str,
        now_ms: int,
    ) -> tuple[AccountSafetyState | None, AccountSafetySnapshotSource]:
        if not account_safety_path(self._artifacts_root, account_id).is_file():
            return None, AccountSafetySnapshotSource(
                source="account_safety",
                state="UNAVAILABLE",
                reason_code="ACCOUNT_SAFETY_NOT_AVAILABLE",
            )
        try:
            state = AccountSafetyAuthority(
                artifacts_root=self._artifacts_root, account_id=account_id, now_ms=lambda: now_ms
            ).read()
        except (AccountSafetyCorruptError, OSError, ValueError):
            return None, AccountSafetySnapshotSource(
                source="account_safety",
                state="UNAVAILABLE",
                reason_code="ACCOUNT_SAFETY_UNAVAILABLE",
            )
        return state, AccountSafetySnapshotSource(
            source="account_safety", state="FRESH", as_of_ms=state.updated_at_ms,
            age_ms=max(0, now_ms - state.updated_at_ms), reason_code=f"ACCOUNT_SAFETY_{state.verdict.value}",
        )

    @staticmethod
    def _truth_source(
        evidence: AccountTruthSnapshot | AccountTruthUnavailable | None,
        *,
        account_id: str,
        epoch: AccountEpochState | None,
        now_ms: int,
        identity_matches: bool,
    ) -> AccountSafetySnapshotSource:
        if evidence is None:
            return AccountSafetySnapshotSource(
                source="account_truth",
                state="UNAVAILABLE",
                reason_code="ACCOUNT_TRUTH_NOT_AVAILABLE",
            )
        if isinstance(evidence, AccountTruthUnavailable):
            return AccountSafetySnapshotSource(
                source="account_truth",
                state="UNAVAILABLE",
                as_of_ms=evidence.attempted_at_ms,
                age_ms=evidence.age_ms(now_ms),
                hard_ttl_ms=evidence.hard_ttl_ms,
                reason_code="ACCOUNT_TRUTH_REFRESH_FAILED",
            )
        return AccountSafetySnapshotSource(
            source="account_truth",
            state="STALE" if evidence.is_stale(now_ms) else "FRESH",
            epoch_relation=AccountSafetySnapshotService._clock_epoch_relation(
                evidence.cached_at_ms,
                account_id=account_id,
                epoch=epoch,
                identity_matches=identity_matches,
            ),
            as_of_ms=evidence.cached_at_ms,
            age_ms=evidence.age_ms(now_ms),
            hard_ttl_ms=evidence.hard_ttl_ms,
            reason_code="ACCOUNT_TRUTH_STALE" if evidence.is_stale(now_ms) else "ACCOUNT_TRUTH_CURRENT",
        )

    @staticmethod
    def _reconciliation_source(
        receipt: AccountReconciliationReceipt | None,
        *,
        account_id: str,
        epoch: AccountEpochState | None,
        now_ms: int,
    ) -> AccountSafetySnapshotSource:
        if receipt is None:
            return AccountSafetySnapshotSource(
                source="reconciliation",
                state="UNAVAILABLE",
                reason_code="RECONCILIATION_NOT_AVAILABLE",
            )
        identity_matches = (
            receipt.account_id == account_id
            and receipt.requested_account_id == account_id
            and receipt.connected_account_id == account_id
        )
        return AccountSafetySnapshotSource(
            source="reconciliation",
            state="FRESH" if now_ms <= receipt.expires_at_ms else "STALE",
            epoch_relation=AccountSafetySnapshotService._clock_epoch_relation(
                receipt.generated_at_ms,
                account_id=account_id,
                epoch=epoch,
                identity_matches=identity_matches,
            ),
            as_of_ms=receipt.generated_at_ms,
            age_ms=max(0, now_ms - receipt.generated_at_ms),
            hard_ttl_ms=receipt.ttl_ms,
            reason_code="RECONCILIATION_CURRENT" if now_ms <= receipt.expires_at_ms else "RECONCILIATION_EXPIRED",
            reconciliation_id=receipt.receipt_id,
            reconciliation_state=receipt.state,
        )

    @staticmethod
    def _epoch_source(
        epoch: AccountEpochState | None,
        *,
        account_id: str,
        now_ms: int,
    ) -> AccountSafetySnapshotSource:
        if epoch is None:
            return AccountSafetySnapshotSource(
                source="account_epoch",
                state="UNAVAILABLE",
                reason_code="ACCOUNT_EPOCH_UNAVAILABLE",
            )
        return AccountSafetySnapshotSource(
            source="account_epoch",
            state="FRESH" if epoch.status == "CLEAN" else "STALE",
            epoch_relation="CURRENT" if epoch.account_id == account_id else "MISMATCH",
            as_of_ms=epoch.updated_at_ms,
            age_ms=max(0, now_ms - epoch.updated_at_ms),
            reason_code=(
                "ACCOUNT_EPOCH_CLEAN"
                if epoch.status == "CLEAN"
                else epoch.would_block_reason or "ACCOUNT_EPOCH_INVALID"
            ),
            epoch=epoch.current_epoch,
            reconciliation_id=epoch.reconciliation_id,
        )

    def _lease_source(
        self,
        account_id: str,
        *,
        epoch: AccountEpochState | None,
        now_ms: int,
    ) -> AccountSafetySnapshotSource:
        assessment = assess_account_observation_lease(self._artifacts_root, account_id, now_ms=now_ms)
        lease = assessment.lease
        identity_matches = lease is not None and lease.account_id == account_id
        generation_matches = epoch is None or (
            lease is not None and lease.clerk_generation == epoch.clerk_generation
        )
        return AccountSafetySnapshotSource(
            source="observation_lease",
            state=(
                "FRESH"
                if assessment.state == "VERIFIED"
                else "STALE"
                if lease is not None
                else "UNAVAILABLE"
            ),
            epoch_relation=(
                "UNATTESTED"
                if lease is None
                else "MISMATCH"
                if not identity_matches or not generation_matches
                else self._clock_epoch_relation(
                    lease.observed_at_ms,
                    account_id=account_id,
                    epoch=epoch,
                    identity_matches=True,
                )
            ),
            as_of_ms=None if lease is None else lease.observed_at_ms,
            age_ms=None if lease is None else max(0, now_ms - lease.observed_at_ms),
            hard_ttl_ms=(
                None
                if lease is None
                else max(1, lease.valid_until_ms - lease.observed_at_ms)
            ),
            reason_code=assessment.reason_code,
        )

    @staticmethod
    def _truth_identity_matches(truth: AccountTruthResponse | None, account_id: str) -> bool:
        return (
            truth is not None
            and truth.account_id == account_id
            and truth.health.account_id == account_id
        )

    @staticmethod
    def _unmanaged_or_unknown_exposure_count(truth: AccountTruthResponse | None) -> int:
        """Count current Account Truth facts that are not bot-owned.

        This is an authority projection, not an estimate. Current Bot custody
        requires both a Bot owner and an ACTIVE or DEPLOYED binding. Manual,
        mixed, foreign/unclaimed, retired, and unproven bindings are all
        outside current custody and remain visible as unmanaged or unknown.
        """
        if truth is None:
            return 0
        facts = (*truth.orders, *truth.executions, *truth.positions)
        return sum(
            fact.owner.owner_class != "bot"
            or fact.owner.owner_binding_state not in {"ACTIVE", "DEPLOYED"}
            for fact in facts
        )

    @staticmethod
    def _clock_epoch_relation(
        as_of_ms: int | None,
        *,
        account_id: str,
        epoch: AccountEpochState | None,
        identity_matches: bool,
    ) -> str:
        if not identity_matches:
            return "MISMATCH"
        if epoch is None or epoch.account_id != account_id or as_of_ms is None:
            return "UNATTESTED"
        return "CURRENT" if as_of_ms >= epoch.updated_at_ms else "PRE_EPOCH"

    @staticmethod
    def _safety_source(
        safety: AccountSafetyState | None,
        source: AccountSafetySnapshotSource,
        *,
        account_id: str,
        epoch: AccountEpochState | None,
    ) -> AccountSafetySnapshotSource:
        if safety is None:
            return source
        relation = AccountSafetySnapshotService._clock_epoch_relation(
            safety.updated_at_ms,
            account_id=account_id,
            epoch=epoch,
            identity_matches=safety.account_id == account_id,
        )
        return source.model_copy(update={"epoch_relation": relation})

    def _custody_summary(
        self,
        account_id: str,
        clerk: AccountServiceStatusResponse,
    ) -> tuple[dict[str, int], str | None]:
        counts = {stage: 0 for stage in _CUSTODY_STAGES}
        if clerk.journal.integrity != "healthy":
            return self._custody_count_fields(counts), None
        try:
            statuses = fold_account_clerk_custody_statuses(
                read_account_clerk_durability_spine(self._artifacts_root, account_id),
            )
        except (AccountClerkJournalCorruptError, OSError, ValueError):
            return self._custody_count_fields(counts), "ACCOUNT_CLERK_JOURNAL_UNAVAILABLE"
        for status in statuses:
            counts[status.custody_stage] += 1
        return self._custody_count_fields(counts), None

    @staticmethod
    def _custody_count_fields(counts: dict[str, int]) -> dict[str, int]:
        return {
            "a0_custody_accepted_count": counts["A0_CUSTODY_ACCEPTED"],
            "a1_broker_write_started_count": counts["A1_BROKER_WRITE_STARTED"],
            "a2_broker_known_count": counts["A2_BROKER_KNOWN"],
            "a3_economic_terminal_count": counts["A3_ECONOMIC_TERMINAL"],
        }

    @staticmethod
    def _with_evidence_refs(
        source: AccountSafetySnapshotSource,
        *,
        account_id: str,
        receipt: AccountReconciliationReceipt | None,
    ) -> AccountSafetySnapshotSource:
        if source.source == "reconciliation" and receipt is not None:
            refs = (
                AccountReconciliationEvidenceRef(
                    source="reconciliation_receipt",
                    ref=f"reconciliation:{receipt.receipt_id}",
                ),
                *tuple(receipt.evidence_refs),
            )
        elif source.source == "account_epoch":
            refs = (
                AccountReconciliationEvidenceRef(
                    source="account_epoch",
                    ref=f"account_epoch:{account_id}",
                    detail=source.reason_code,
                ),
            )
        elif source.source == "account_safety":
            refs = (
                AccountReconciliationEvidenceRef(
                    source="account_safety",
                    ref=f"account_safety:{account_id}",
                    detail=source.reason_code,
                ),
            )
        elif source.source == "observation_lease":
            refs = (
                AccountReconciliationEvidenceRef(
                    source="observation_lease",
                    ref=f"account_observation_lease:{account_id}",
                    detail=source.reason_code,
                ),
            )
        elif source.source == "clerk":
            refs = (
                AccountReconciliationEvidenceRef(
                    source="clerk_journal",
                    ref=f"clerk_journal:{account_id}",
                    detail=source.reason_code,
                ),
            )
        elif source.source == "account_truth":
            observed_at = source.as_of_ms if source.as_of_ms is not None else "unavailable"
            refs = (
                AccountReconciliationEvidenceRef(
                    source="account_truth_cache",
                    ref=f"account_truth_cache:{account_id}:{observed_at}",
                    detail=source.reason_code,
                ),
            )
        else:
            source_name = source.source.removeprefix("account_truth.")
            observed_at = source.as_of_ms if source.as_of_ms is not None else "unavailable"
            refs = (
                AccountReconciliationEvidenceRef(
                    source=f"account_truth_{source_name}",
                    ref=f"account_truth_source:{source_name}:{observed_at}",
                    detail=source.reason_code,
                ),
            )
        return source.model_copy(update={"evidence_refs": refs})

    @staticmethod
    def _evidence_refs(
        sources: tuple[AccountSafetySnapshotSource, ...],
    ) -> tuple[AccountReconciliationEvidenceRef, ...]:
        refs = {
            (ref.source, ref.ref, ref.detail): ref
            for source in sources
            for ref in source.evidence_refs
        }
        return tuple(
            refs[key]
            for key in sorted(refs, key=lambda key: (key[0], key[1], key[2] or ""))
        )

    @staticmethod
    def _verdict_reason(reason_code: str) -> str:
        return _VERDICT_REASONS.get(
            reason_code,
            "Account safety evidence requires operator review before new entry risk can proceed.",
        )

    @staticmethod
    def _clerk_source(
        clerk: AccountServiceStatusResponse,
        *,
        account_id: str,
        epoch: AccountEpochState | None,
        now_ms: int,
        custody_read_failure: str | None,
    ) -> AccountSafetySnapshotSource:
        lease = clerk.lease
        as_of_ms = clerk.generation_recorded_at_ms if lease is None else lease.renewed_at_ms
        hard_ttl_ms = (
            None
            if lease is None
            else max(1, lease.valid_until_ms - lease.renewed_at_ms)
        )
        identity_matches = clerk.account_id == account_id
        if custody_read_failure is not None:
            state = "UNAVAILABLE"
            reason_code = custody_read_failure
        elif clerk.journal.integrity == "corrupt":
            state = "UNAVAILABLE"
            reason_code = "ACCOUNT_CLERK_JOURNAL_CORRUPT"
        elif clerk.journal.integrity == "broker_evidence_only":
            state = "UNAVAILABLE"
            reason_code = "ACCOUNT_CLERK_BROKER_EVIDENCE_ONLY_HOLD"
        elif clerk.attachment != "ATTACHED" or lease is None:
            state = "UNAVAILABLE"
            reason_code = "ACCOUNT_CLERK_UNAVAILABLE"
        else:
            state = "FRESH"
            reason_code = "ACCOUNT_CLERK_ATTACHED"
        relation = (
            "MISMATCH"
            if not identity_matches
            else "UNATTESTED"
            if epoch is None or clerk.generation is None
            else "MISMATCH"
            if clerk.generation != epoch.clerk_generation
            else AccountSafetySnapshotService._clock_epoch_relation(
                as_of_ms,
                account_id=account_id,
                epoch=epoch,
                identity_matches=True,
            )
        )
        return AccountSafetySnapshotSource(
            source="clerk",
            state=state,
            epoch_relation=relation,
            as_of_ms=as_of_ms,
            age_ms=None if as_of_ms is None else max(0, now_ms - as_of_ms),
            hard_ttl_ms=hard_ttl_ms,
            reason_code=reason_code,
        )

    @staticmethod
    def _verdict(
        safety_verdict: AccountSafetyVerdict | None,
        epoch: AccountEpochState | None,
        receipt: AccountReconciliationReceipt | None,
        truth: AccountTruthResponse | None,
        sources: tuple[AccountSafetySnapshotSource, ...],
    ) -> tuple[AccountSafetySnapshotVerdict, str]:
        if safety_verdict is AccountSafetyVerdict.CONTAMINATED:
            return "CONTAMINATED", "ACCOUNT_SAFETY_CONTAMINATED"
        if safety_verdict is AccountSafetyVerdict.SUSPENDED:
            return "SUSPENDED", "ACCOUNT_SAFETY_SUSPENDED"
        if any(source.critical and source.epoch_relation == "MISMATCH" for source in sources):
            return "UNAVAILABLE", "CRITICAL_SAFETY_IDENTITY_MISMATCH"
        if epoch is None or any(source.critical and source.state == "UNAVAILABLE" for source in sources):
            return "UNAVAILABLE", "CRITICAL_SAFETY_EVIDENCE_UNAVAILABLE"
        if safety_verdict is AccountSafetyVerdict.RECONCILING or epoch.status != "CLEAN":
            return "RECONCILING", "ACCOUNT_RECONCILIATION_REQUIRED"
        if any(source.critical and source.epoch_relation != "CURRENT" for source in sources):
            return "RECONCILING", "CRITICAL_SAFETY_EPOCH_UNPROVEN"
        if receipt is None or receipt.state != "CLEAN":
            return "RECONCILING", "ACCOUNT_RECONCILIATION_NOT_PROVEN"
        if truth is None or truth.final_verdict != "clean":
            return "RECONCILING", "ACCOUNT_TRUTH_NOT_PROVEN"
        if any(source.critical and source.state == "STALE" for source in sources):
            return "STALE", "CRITICAL_SAFETY_EVIDENCE_STALE"
        return "CLEAN", "ACCOUNT_SAFETY_CLEAN"
