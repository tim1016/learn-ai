"""One fresh account-proof decision for interactive bot admission."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from app.broker.ibkr.client import BrokerError, IbkrClient
from app.engine.live import host_daemon_client
from app.engine.live.account_observation_lease import (
    account_observation_lease_gate_result,
    assess_account_observation_lease,
)
from app.engine.live.journal_recovery_state import (
    JournalRecoveryStateCorruptError,
    assess_journal_recovery_fence,
)
from app.schemas.live_runs import GateResult
from app.services.account_gate_promotion import (
    AccountGateAuthority,
    resolve_account_gate_authority,
    resolve_action_gate,
)
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import refresh_account_truth_now
from app.services.account_truth_snapshot import (
    account_truth_gate_result,
    get_account_truth_snapshot_provider,
)

logger = logging.getLogger(__name__)


class AccountStartGateError(RuntimeError):
    """A start boundary cannot prove its selected account gate."""

    def __init__(self, *, status_code: int, detail: dict[str, object]) -> None:
        super().__init__(str(detail.get("reason_code", "ACCOUNT_START_GATE_BLOCKED")))
        self.status_code = status_code
        self.detail = detail


async def ensure_account_start_gate(
    artifacts_root: Path,
    *,
    account_id: str,
    daemon_url: str,
    requested_authority: AccountGateAuthority,
    client: IbkrClient,
    now_ms: int,
    current_now_ms: Callable[[], int],
) -> GateResult:
    """Refresh Account Truth and enforce the proof selected for this Start.

    Promotion is reevaluated before and after any Clerk readiness action.  A
    new accepting Clerk generation invalidates its smoke evidence, so the same
    Start immediately returns to the proven Account Truth gate.  The current
    paired proof also cannot allow a first lease-weaker action.
    """

    try:
        recovery_fence = assess_journal_recovery_fence(artifacts_root, account_id)
    except JournalRecoveryStateCorruptError as exc:
        raise AccountStartGateError(
            status_code=409,
            detail={
                "reason_code": "CLERK_JOURNAL_RECOVERY_STATE_CORRUPT",
                "account_id": account_id,
                "gate_id": "account.clerk_journal_recovery",
                "operator_next_step": "ACCOUNT_DESK_JOURNAL_RECOVERY_REQUIRED",
            },
        ) from exc
    if recovery_fence.blocks_broker_writes:
        raise AccountStartGateError(
            status_code=409,
            detail={
                "reason_code": recovery_fence.reason_code,
                "account_id": account_id,
                "gate_id": "account.clerk_journal_recovery",
                "operator_next_step": "ACCOUNT_DESK_JOURNAL_RECOVERY_REQUIRED",
            },
        )

    initial_promotion = resolve_account_gate_authority(
        artifacts_root,
        account_id=account_id,
        requested_authority=requested_authority,
        now_ms=now_ms,
    )
    if initial_promotion.effective_authority == "observation_lease":
        initial_lease = assess_account_observation_lease(
            artifacts_root,
            account_id,
            now_ms=now_ms,
        )
        if initial_lease.state != "VERIFIED":
            await _ensure_clerk_ready(daemon_url, account_id)

    reconciliation = AccountReconciliationService(artifacts_root=artifacts_root)
    try:
        await refresh_account_truth_now(
            client,
            account_id=account_id,
            artifacts_root=artifacts_root,
            context="start account verification",
            account_truth_observer=reconciliation.observe_account_truth,
            account_truth_failure_observer=reconciliation.observe_account_truth_failure,
        )
    except BrokerError:
        logger.warning("start account verification refresh failed", extra={"account_id": account_id}, exc_info=True)

    decided_at_ms = current_now_ms()
    truth_gate = account_truth_gate_result(
        get_account_truth_snapshot_provider().get(account_id),
        now_ms=decided_at_ms,
    )
    promotion = resolve_account_gate_authority(
        artifacts_root,
        account_id=account_id,
        requested_authority=requested_authority,
        now_ms=decided_at_ms,
    )
    if promotion.effective_authority == "account_truth":
        _raise_if_blocked(truth_gate)
        return truth_gate

    lease_assessment = assess_account_observation_lease(
        artifacts_root,
        account_id,
        now_ms=decided_at_ms,
    )
    lease_gate = account_observation_lease_gate_result(lease_assessment)
    action = resolve_action_gate(
        promotion.effective_authority,
        account_truth_gate=truth_gate,
        observation_lease_gate=lease_gate,
    )
    if action.gate is None:
        raise RuntimeError("account action gate resolution omitted its selected gate")
    _raise_if_blocked(action.gate)
    return action.gate


async def _ensure_clerk_ready(daemon_url: str, account_id: str) -> None:
    try:
        await host_daemon_client.ensure_account_clerk(daemon_url, account_id)
    except host_daemon_client.HostDaemonOutcomeUnknownError as exc:
        raise AccountStartGateError(
            status_code=409,
            detail={
                "reason_code": "OUTCOME_UNKNOWN",
                "message": exc.detail
                or "The Clerk readiness request may have completed; refresh before retrying.",
            },
        ) from exc
    except host_daemon_client.HostDaemonError as exc:
        raise AccountStartGateError(status_code=exc.status_code, detail=exc.detail) from exc


def _raise_if_blocked(gate: GateResult) -> None:
    if gate.status == "pass":
        return
    raise AccountStartGateError(
        status_code=409,
        detail={
            "reason_code": gate.operator_reason,
            "message": "Account verification must pass before starting a bot.",
            "gate_result": gate.model_dump(mode="json"),
            "operator_next_step": gate.operator_next_step,
        },
    )


__all__ = ["AccountStartGateError", "ensure_account_start_gate"]
