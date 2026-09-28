"""UI migration journey for budget authority; never converts historical grants."""
from __future__ import annotations

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite.budget_authority import authority_review_token, commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.errors import BrokerError
from app.broker_configuration.errors import BrokerConfigurationError, RevisionConflict
from app.broker_configuration.service import BrokerConfigurationService
from app.schemas.deployment_budget import BudgetAuthorityApplyRequest, BudgetAuthorityState
from app.services.alpaca_live_graduation_gate import graduation_mutation_fence
from app.services.bot_runner import get_bot_task_registry
from app.services.lane_quiesce import stop_all_bots_on_lane


def _require_runtime(runtime: ActiveClerkRuntime | None) -> ActiveClerkRuntime:
    if runtime is None or runtime.sqlite_repository is None or not isinstance(runtime.clerk, SqliteAlpacaClerkFacade):
        raise BrokerConfigurationError("The account custody authority is unavailable.", next_step="Activate or restore the account under Broker connection in Settings, then retry.")
    return runtime


def read_budget_authority(runtime: ActiveClerkRuntime | None) -> BudgetAuthorityState:
    runtime = _require_runtime(runtime)
    repo = runtime.sqlite_repository
    assert repo is not None
    with repo.write_fence() as conn:
        version = repo.budget_authority_version()
        return BudgetAuthorityState(
            state="budget" if version == 2 else "legacy", account_id=repo.account_id,
            authorization_version=version, review_token=authority_review_token(repo),
            active_run_count=conn.execute("SELECT COUNT(*) FROM runs WHERE state='ACTIVE'").fetchone()[0],
            detail=("Fresh Deploys require dollar budgets and current consent. Historical grants cannot authorize trading." if version == 2 else
                    "Switching to budgets stops this account's bots and checks its orders against Alpaca. Positions, orders and fees stay visible and recoverable. Then deploy each bot afresh with its own dollar budget."),
        )


def _require_current_selection(service: BrokerConfigurationService, runtime: ActiveClerkRuntime) -> None:
    if get_active_clerk_runtime() is not runtime:
        raise RevisionConflict("The account authority changed during the upgrade.", next_step="Reload Settings and review the current account.")
    selection = service.selection()
    if selection.effective_account_id is None or runtime.account_id not in {selection.effective_account_id, f"shadow:{selection.effective_account_id}"}:
        raise RevisionConflict("The selected account changed during the upgrade.")


async def apply_budget_authority(service: BrokerConfigurationService, runtime: ActiveClerkRuntime | None, request: BudgetAuthorityApplyRequest) -> BudgetAuthorityState:
    runtime = _require_runtime(runtime)
    repo = runtime.sqlite_repository
    assert repo is not None and isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    async with graduation_mutation_fence():
        with service.selection_handover():
            _require_current_selection(service, runtime)
        reviewed = read_budget_authority(runtime)
        if reviewed.state == "budget":
            return reviewed
        if request.review_token != reviewed.review_token:
            raise RevisionConflict("The account or running deployments changed.", next_step="Reload and review the budget upgrade again.")
        registry = get_bot_task_registry()
        if registry is None:
            raise BrokerConfigurationError("The bot runner is unavailable.", next_step="Wait for recovery before switching to budgets.")
        actor = service.owner().owner_id
        receipt = await stop_all_bots_on_lane(registry, operator=actor, change_ref="budget-authority-cutover", clock=repo.clock)
        if not receipt.all_stopped:
            raise BrokerConfigurationError("Some bots could not be stopped.", next_step="Review their Stop receipts and resolve recovery, then retry the upgrade.")
        # Reconcile through the same cancellation/recovery authority. Unknown
        # obligations are retained and continue blocking new spending. Being
        # stopped is never described as proof of broker flatness.
        try:
            await runtime.clerk.reconcile_account(trigger="MANUAL")
        except BrokerError as exc:
            raise BrokerConfigurationError("Bots were stopped, but custody could not be reconciled.", next_step="Restore the account connection and retry the upgrade.") from exc
        async with runtime.clerk.intake:
            with service.selection_handover():
                _require_current_selection(service, runtime)
                try:
                    commit_budget_authority_cutover(repo, actor=actor, reviewed_token=request.review_token, stop_receipt=receipt.receipt_id)
                except BudgetUnavailable as exc:
                    raise BrokerConfigurationError(str(exc), next_step="Reload Settings and resolve the remaining Stop evidence before retrying.") from exc
                if runtime.envelope_sync is not None:
                    runtime.envelope_sync.refresh_arming()
                return read_budget_authority(runtime)
