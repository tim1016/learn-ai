"""Read-only discovery and Account-service projection for the Account desk."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.account_artifacts import (
    AccountArtifactError,
    AccountClerkGeneration,
    AccountClerkLease,
    list_account_artifact_ids,
    read_account_clerk_generation,
    read_account_clerk_lease,
)
from app.engine.live.account_binding_ledger import account_binding_ledger_read_enabled
from app.engine.live.account_clerk_journal import inspect_account_clerk_journal
from app.engine.live.account_clerk_journal_models import AccountClerkJournalCorruptError
from app.engine.live.account_identity import normalize_account_id
from app.engine.live.account_observation_lease import (
    account_observation_lease_gate_result,
    assess_account_observation_lease,
)
from app.engine.live.account_registry import (
    ACTIVE_INSTANCE_BINDING_STATES,
    AccountInstanceBinding,
    account_binding_ledger_parity,
    index_account_instance_bindings,
    pending_account_binding_retirements,
    read_account_instance_registry,
)
from app.engine.live.account_session_policy import (
    assess_account_live_session,
    read_account_session_policy,
)
from app.schemas.account_directory import (
    AccountEffectivePosture,
    AccountRosterRow,
    AccountRosterVerdictSummary,
    AccountServiceAttachmentState,
    AccountServiceBinding,
    AccountServiceGateAuthority,
    AccountServiceJournalWatermark,
    AccountServiceLease,
    AccountServiceOperatingState,
    AccountServiceSessionPolicy,
    AccountServiceStatusResponse,
    AccountServiceSummary,
    AccountsRosterResponse,
)
from app.schemas.live_runs import GateResult
from app.services.account_gate_promotion import (
    AccountGateAuthority,
    effective_account_gate_authority_for_current_evidence,
    resolve_account_gate_authority,
)
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_snapshot import (
    account_truth_gate_result,
    get_account_truth_snapshot_provider,
)
from app.utils.timestamps import now_ms_utc


class AccountDirectoryError(ValueError):
    """The account roster cannot be safely projected from durable evidence."""


class UnknownAccountError(AccountDirectoryError):
    """The requested account is neither configured nor durably known."""


@dataclass(frozen=True)
class CurrentBrokerAccount:
    """Server-known current IBKR account; absent while no account is connected."""

    account_id: str
    is_paper: bool


class AccountDirectoryService:
    """Compose account-keyed read models without mutating broker or artifacts."""

    def __init__(
        self,
        *,
        artifacts_root: Path,
        current_account: CurrentBrokerAccount | None,
        requested_account_gate_authority: AccountGateAuthority = "account_truth",
        now_ms: Callable[[], int] = now_ms_utc,
    ) -> None:
        self._artifacts_root = artifacts_root
        self._current_account = current_account
        self._requested_account_gate_authority = requested_account_gate_authority
        self._now_ms = now_ms
        self._reconciliation = AccountReconciliationService(artifacts_root=artifacts_root)

    def roster(self) -> AccountsRosterResponse:
        """List configured and durably-known accounts in stable account-id order."""

        known_accounts = self._known_accounts()
        rows = [
            self._roster_row(account_id, bindings)
            for account_id, bindings in known_accounts.items()
        ]
        return AccountsRosterResponse(rows=rows)

    def service_status(self, *, account_id: str) -> AccountServiceStatusResponse:
        """Return full Account service evidence for exactly one known account."""

        canonical_account_id = normalize_account_id(account_id)
        known_accounts = self._known_accounts()
        if canonical_account_id not in known_accounts:
            raise UnknownAccountError(f"unknown account: {canonical_account_id}")
        return self._service_status_for_known_account(
            canonical_account_id,
            bindings=known_accounts[canonical_account_id],
        )

    def _service_status_for_known_account(
        self,
        account_id: str,
        *,
        bindings: list[AccountInstanceBinding] | None = None,
    ) -> AccountServiceStatusResponse:
        """Project service artifacts after the account-key membership boundary has passed."""

        try:
            generation = read_account_clerk_generation(self._artifacts_root, account_id)
            lease = read_account_clerk_lease(self._artifacts_root, account_id)
            journal = inspect_account_clerk_journal(self._artifacts_root, account_id)
            ledger_parity = account_binding_ledger_parity(
                self._artifacts_root,
                account_id=account_id,
            )
        except (
            AccountArtifactError,
            AccountClerkJournalCorruptError,
            OSError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise AccountDirectoryError(str(exc)) from exc

        try:
            now_ms = self._now_ms()
            attachment = _attachment_state(generation, lease, now_ms=now_ms)
            gate_authority = resolve_account_gate_authority(
                self._artifacts_root,
                account_id=account_id,
                requested_authority=self._requested_account_gate_authority,
                now_ms=now_ms,
            )
            action_authority, action_gate = _effective_account_action_gate(
                self._artifacts_root,
                account_id=account_id,
                promoted_authority=gate_authority.effective_authority,
                now_ms=now_ms,
            )
            session_policy = read_account_session_policy(self._artifacts_root, account_id)
            session_assessment = assess_account_live_session(
                self._artifacts_root,
                account_id=account_id,
                now_ms=now_ms,
            )
            pending_retirement_proposals = len(
                pending_account_binding_retirements(
                    self._artifacts_root,
                    account_id=account_id,
                )
            )
        except (
            AccountArtifactError,
            OSError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise AccountDirectoryError(str(exc)) from exc
        ledger_parity_issue_count = sum(
            len(instances)
            for instances in (
                ledger_parity.legacy_only_instances,
                ledger_parity.ledger_only_instances,
                ledger_parity.mismatched_instances,
            )
        )
        ledger_parity_state = "clean" if ledger_parity.is_clean else "dirty"
        ledger_read_authority = (
            "clerk_ledger"
            if account_binding_ledger_read_enabled() and ledger_parity.is_clean
            else "legacy_registry"
        )
        operating_state, headline, detail = self._operating_copy(
            account_id,
            attachment,
            bindings=bindings,
            pending_retirement_proposals=pending_retirement_proposals,
            ledger_parity_issue_count=ledger_parity_issue_count,
            gate_authority_state=gate_authority.state,
        )
        return AccountServiceStatusResponse(
            account_id=account_id,
            attachment=attachment,
            phase=None if generation is None else generation.phase,
            generation=None if generation is None else generation.generation,
            generation_recorded_at_ms=None if generation is None else generation.recorded_at_ms,
            source=None if generation is None else generation.source,
            binding=AccountServiceBinding(
                state=attachment,
                generation=None if generation is None else generation.generation,
                lease_generation=None if lease is None else lease.generation,
                pending_retirement_proposals=pending_retirement_proposals,
                ledger_read_authority=ledger_read_authority,
                ledger_parity=ledger_parity_state,
                ledger_parity_issue_count=ledger_parity_issue_count,
            ),
            gate_authority=AccountServiceGateAuthority(
                requested_authority=gate_authority.requested_authority,
                effective_authority=gate_authority.effective_authority,
                promotion_state=gate_authority.state,
                reason_code=gate_authority.reason_code,
                disposition=gate_authority.disposition,
                action_authority=action_authority,
                action_gate=action_gate,
                observed_session_dates=(
                    [] if gate_authority.parity is None else list(gate_authority.parity.observed_session_dates)
                ),
                lease_weaker_comparison_count=(
                    0
                    if gate_authority.parity is None
                    else len(gate_authority.parity.lease_weaker_comparisons)
                ),
                restart_smoke_recorded_at_ms=(
                    None if gate_authority.restart_smoke is None else gate_authority.restart_smoke.recorded_at_ms
                ),
            ),
            session_policy=AccountServiceSessionPolicy(
                allow_outside_live_session=session_policy.allow_outside_live_session,
                gate_result=session_assessment.to_gate_result(),
            ),
            lease=None if lease is None else AccountServiceLease(
                status=lease.status,
                generation=lease.generation,
                started_at_ms=lease.started_at_ms,
                renewed_at_ms=lease.renewed_at_ms,
                valid_until_ms=lease.valid_until_ms,
            ),
            journal=AccountServiceJournalWatermark(
                last_seq=None if not journal else journal[-1].seq,
                last_write_ms=None if not journal else journal[-1].recorded_at_ms,
            ),
            operating_state=operating_state,
            headline=headline,
            detail=detail,
        )

    def _operating_copy(
        self,
        account_id: str,
        attachment: AccountServiceAttachmentState,
        *,
        bindings: list[AccountInstanceBinding] | None = None,
        pending_retirement_proposals: int = 0,
        ledger_parity_issue_count: int = 0,
        gate_authority_state: str = "SAFE_DEFAULT",
    ) -> tuple[AccountServiceOperatingState, str, str]:
        if ledger_parity_issue_count:
            suffix = "difference" if ledger_parity_issue_count == 1 else "differences"
            return (
                "ATTENTION",
                "Binding ledger parity needs attention",
                f"{ledger_parity_issue_count} binding ledger {suffix} require operator repair; bot admission and Clerk intake remain fail-closed.",
            )
        if pending_retirement_proposals:
            suffix = "proposal is" if pending_retirement_proposals == 1 else "proposals are"
            return (
                "ATTENTION",
                "Binding retirement reconciliation pending",
                f"{pending_retirement_proposals} daemon liveness {suffix} waiting for the Account Clerk to reconcile before bot admission can resume.",
            )
        if attachment != "ATTACHED":
            return (
                "ATTENTION",
                "Account service needs attention",
                "Account verification cannot stay current until the account service is attached.",
            )
        if gate_authority_state in {
            "WAITING_FOR_SHADOW_PARITY",
            "WAITING_FOR_CLERK_RESTART_SMOKE",
        }:
            return (
                "ATTENTION",
                "Account gate promotion pending",
                "The proven Account Truth gate remains active until the Clerk proof promotion evidence is complete.",
            )
        if bindings is None:
            try:
                bindings = read_account_instance_registry(self._artifacts_root, account_id)
            except (AccountArtifactError, OSError, ValueError) as exc:
                raise AccountDirectoryError(str(exc)) from exc
        latest_bindings = index_account_instance_bindings(
            bindings,
            account_id=account_id,
        ).latest_by_instance.values()
        active_count = sum(
            binding.lifecycle_state in ACTIVE_INSTANCE_BINDING_STATES
            for binding in latest_bindings
        )
        if active_count == 0:
            return (
                "STANDBY",
                "Ready — no bots on duty",
                "Account verification continues in the background and the service is ready for a bot to attach.",
            )
        suffix = "bot is" if active_count == 1 else "bots are"
        return (
            "READY",
            f"Ready — {active_count} {suffix} on duty",
            "The account service is attached and continuously verifying this account.",
        )

    def _known_accounts(self) -> dict[str, list[AccountInstanceBinding]]:
        try:
            known_accounts: dict[str, list[AccountInstanceBinding]] = {}
            for directory_account_id in list_account_artifact_ids(self._artifacts_root):
                bindings = read_account_instance_registry(self._artifacts_root, directory_account_id)
                if bindings:
                    binding_account_ids = {
                        normalize_account_id(binding.account_id)
                        for binding in bindings
                    }
                    if binding_account_ids != {directory_account_id}:
                        raise AccountDirectoryError(
                            "account binding identity does not match its durable directory: "
                            f"{directory_account_id}"
                        )
                known_accounts[directory_account_id] = bindings
        except (AccountArtifactError, OSError, ValueError) as exc:
            raise AccountDirectoryError(str(exc)) from exc
        if self._current_account is not None:
            current_account_id = normalize_account_id(self._current_account.account_id)
            known_accounts.setdefault(current_account_id, [])
        return dict(sorted(known_accounts.items()))

    def _roster_row(
        self,
        account_id: str,
        bindings: list[AccountInstanceBinding],
    ) -> AccountRosterRow:
        try:
            service = self._service_status_for_known_account(account_id, bindings=bindings)
            triage = self._reconciliation.triage(account_id=account_id, now_ms=self._now_ms())
        except (AccountArtifactError, OSError, ValidationError, ValueError) as exc:
            raise AccountDirectoryError(str(exc)) from exc
        return AccountRosterRow(
            account_id=account_id,
            effective_posture=self._effective_posture(account_id),
            service=AccountServiceSummary(
                attachment=service.attachment,
                phase=service.phase,
                generation=service.generation,
                operating_state=service.operating_state,
                headline=service.headline,
            ),
            latest_verdict_summary=AccountRosterVerdictSummary(
                state=triage.verdict.state,
                headline=triage.verdict.headline,
                generated_at_ms=triage.generated_at_ms,
            ),
            last_verified_at_ms=triage.account_observation.observed_at_ms,
        )

    def _effective_posture(self, account_id: str) -> AccountEffectivePosture:
        if self._current_account is None or self._current_account.account_id != account_id:
            return "UNKNOWN"
        return "PAPER_EXECUTION" if self._current_account.is_paper else "UNSAFE"


def _effective_account_action_gate(
    artifacts_root: Path,
    *,
    account_id: str,
    promoted_authority: AccountGateAuthority,
    now_ms: int,
) -> tuple[AccountGateAuthority, GateResult]:
    """Project the proof that would enforce a normal action right now."""

    truth_gate = account_truth_gate_result(
        get_account_truth_snapshot_provider().get(account_id),
        now_ms=now_ms,
    )
    if promoted_authority == "account_truth":
        return "account_truth", truth_gate
    lease_gate = account_observation_lease_gate_result(
        assess_account_observation_lease(artifacts_root, account_id, now_ms=now_ms)
    )
    action_authority = effective_account_gate_authority_for_current_evidence(
        promoted_authority,
        account_truth_gate_status=truth_gate.status,
        observation_lease_gate_status=lease_gate.status,
    )
    return action_authority, lease_gate if action_authority == "observation_lease" else truth_gate


def _attachment_state(
    generation: AccountClerkGeneration | None,
    lease: AccountClerkLease | None,
    *,
    now_ms: int,
) -> AccountServiceAttachmentState:
    if generation is None:
        return "UNATTACHED"
    if lease is None:
        return "FENCED"
    return "ATTACHED" if (
        generation.generation == lease.generation
        and lease.status == "RUNNING"
        and lease.valid_until_ms > now_ms
    ) else "FENCED"
