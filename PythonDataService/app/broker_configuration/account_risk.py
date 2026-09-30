"""Configuration's immediate, explicitly reviewed account risk action."""
from __future__ import annotations

import asyncio
import logging

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.money import recorded_dollars
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, RiskRevisionConflict
from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness
from app.broker.contract.errors import BrokerError
from app.broker_configuration.errors import BrokerConfigurationError, RevisionConflict
from app.broker_configuration.service import BrokerConfigurationService
from app.schemas.broker_configuration import AccountRiskApplyRequest, AccountRiskClearRequest, AccountRiskStateResponse

logger = logging.getLogger(__name__)

_NO_LIMIT_HERE = "No daily loss limit is set for this account, so new entries are refused. Set one below and apply it."


def _require_runtime(runtime: ActiveClerkRuntime | None) -> ActiveClerkRuntime:
    if runtime is None or runtime.sqlite_repository is None or runtime.envelope_sync is None:
        raise BrokerConfigurationError(
            "This account's risk authority is unavailable.",
            next_step="Connect this account under Broker connection in Settings, then reload the daily loss limit.",
        )
    return runtime


def read_account_risk_state(
    service: BrokerConfigurationService, runtime: ActiveClerkRuntime | None,
) -> AccountRiskStateResponse:
    runtime = _require_runtime(runtime)
    repo, sync = runtime.sqlite_repository, runtime.envelope_sync
    selection = service.selection()
    with repo.write_fence():
        snapshot = sync.risk_snapshot()
        readiness = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
        policy, cause = snapshot.policy, snapshot.hold
        limits = policy if policy is not None else snapshot.legacy_values
        state = "held" if cause is not None else ("ready" if readiness.allowed else "unknown")
        detail = {
            "held": "A standing loss hold still blocks new entries. Applying looser limits does not clear it.",
            "ready": "These limits apply to new entries immediately. Existing bot exit terms stay fixed.",
            # Settings is where the missing limit is set, so its own read says
            # so in place rather than pointing the owner back at this page.
            "unknown": _NO_LIMIT_HERE if readiness.limit_missing else readiness.detail,
        }[state]
        return AccountRiskStateResponse(
            account_id=repo.account_id, risk_revision=0 if policy is None else policy.revision,
            selection_generation=selection.selection_generation,
            loss_fraction=None if limits is None else limits.loss_fraction,
            loss_usd=None if limits is None else limits.loss_usd,
            applied_at_ms=None if policy is None else policy.applied_at_ms,
            entry_state=state, detail=detail, limit_missing=readiness.limit_missing,
            hold_loss_limit_usd=None if cause is None else recorded_dollars(cause.loss_limit_usd),
            hold_session_start_ms=None if cause is None else cause.day_start_ms,
            hold_policy_revision=None if cause is None else cause.policy_revision,
        )


async def apply_account_risk_limits(
    service: BrokerConfigurationService, runtime: ActiveClerkRuntime | None, request: AccountRiskApplyRequest,
) -> AccountRiskStateResponse:
    runtime = _require_runtime(runtime)
    repo, sync = runtime.sqlite_repository, runtime.envelope_sync
    # Capture facts without holding a synchronous lock across network I/O.
    # A failed observation cannot be described as a successful clearance.
    try:
        await sync.observe()
    except BrokerError as error:
        sync.discard_observation()
        logger.warning("Account risk apply discarded a failed cash/risk observation.", exc_info=error)

    def commit() -> AccountRiskStateResponse:
        with service.selection_handover():
            selection = service.selection()
            if selection.selection_generation != request.expected_selection_generation:
                raise RevisionConflict("Account configuration changed since review.", next_step="Reload and review the limits again.")
            account_id = selection.effective_account_id
            if account_id is None or repo.account_id not in {account_id, f"shadow:{account_id}"}:
                raise RevisionConflict("The reviewed account is no longer the active custody account.")
            if selection.effective_profile_id is None or selection.effective_revision is None:
                raise RevisionConflict("No effective account profile is installed.")
            policy = AccountRiskPolicy(
                revision=request.expected_risk_revision + 1,
                loss_fraction=request.loss_fraction, loss_usd=request.loss_usd,
                profile_id=selection.effective_profile_id, profile_revision=selection.effective_revision,
                actor=service.owner().owner_id, applied_at_ms=repo.clock(),
            )
            try:
                sync.apply_risk_policy(policy, expected_revision=request.expected_risk_revision)
            except RiskRevisionConflict as exc:
                raise RevisionConflict(str(exc), next_step="Reload and review the limits again.") from exc
            return read_account_risk_state(service, runtime)

    return await asyncio.to_thread(commit)


async def clear_account_risk_hold(
    service: BrokerConfigurationService, runtime: ActiveClerkRuntime | None, request: AccountRiskClearRequest,
) -> AccountRiskStateResponse:
    from app.services.alpaca_live_envelope import clear_loss_hold

    runtime = _require_runtime(runtime)
    with service.selection_handover():
        state = read_account_risk_state(service, runtime)
        account_id = service.selection().effective_account_id
        if account_id is None or state.account_id not in {account_id, f"shadow:{account_id}"}:
            raise RevisionConflict("The reviewed account is no longer the active custody account.")
        if state.selection_generation != request.expected_selection_generation or state.risk_revision != request.expected_risk_revision:
            raise RevisionConflict("Account risk limits changed since review.", next_step="Reload and review before clearing the hold.")
        result = await clear_loss_hold(runtime, now_ms=runtime.sqlite_repository.clock())
        if result.outcome == "refused":
            raise BrokerConfigurationError(result.detail, next_step="Wait for fresh evidence of recovery, then clear again.")
        return read_account_risk_state(service, runtime)
