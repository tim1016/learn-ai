"""Review restoration of an existing Options budget, preserving lifetime results.

Formula: the restored free claim is ``sqlite/options_resume.resume_spending_snapshot``'s.
Reference: ADR 0079 (explicit Options recovery); ADR 0059 (budget consent).
Canonical implementation: ``app/broker/alpaca/clerk/sqlite/options_resume.py``.
Validated against: tests/broker/alpaca/clerk/test_options_resume.py.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.money import consent_cents, dollars
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.options_resume import resume_spending_snapshot
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker_configuration.runtime import get_broker_configuration_service
from app.schemas.deployment_budget import DeployBudgetConsent, DeploymentBudgetInput, DeploymentBudgetPreview
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.broker_v2_panel.bot_custody import bot_clerk_runtime


async def preview_options_resume_budget(
    binding: BrokerBotBinding, budget: DeploymentBudgetInput | None,
) -> DeploymentBudgetPreview:
    """Review original capital and the remaining free amount being reclaimed."""
    if binding.options_bot is None:
        raise BudgetUnavailable("Only an interrupted Options bot can use this recovery review.")
    async with bot_clerk_runtime("alpaca", binding.strategy_instance_id) as runtime:
        if runtime is None:
            raise BudgetUnavailable("The original Options custody authority is unavailable.")
        if runtime.account_authority_kind == "synthetic" and runtime.envelope_sync is not None:
            await runtime.envelope_sync.tick()
        return _preview_on_runtime(binding, budget, runtime)


def _preview_on_runtime(
    binding: BrokerBotBinding, budget: DeploymentBudgetInput | None, runtime: ActiveClerkRuntime,
) -> DeploymentBudgetPreview:
    repo = runtime.sqlite_repository
    if repo is None or repo.account_id != binding.sealed_account_id:
        raise BudgetUnavailable("The original Options account custody is unavailable.")
    with repo.write_fence():
        original = repo.deployment_budget(binding.strategy_instance_id)
        if original is None:
            raise BudgetUnavailable("The original Options deployment budget is unavailable.")
        if budget is None:
            policy = repo.account_risk_policy()
            return DeploymentBudgetPreview(
                state="ready", detail="Resume manages held positions. New entries and re-entry remain disabled.",
                world=original["world"], budget_usd=dollars(original["committed_cents"]),
                observed_at_ms=repo.clock(), risk_revision=0 if policy is None else policy.revision,
            )
        if consent_cents(budget.amount_usd) != original["committed_cents"]:
            raise BudgetUnavailable("Resume keeps the original cash budget and all lifetime results.")
        sync = runtime.envelope_sync
        try:
            spending = resume_spending_snapshot(repo, binding, envelope=None if sync is None else sync.envelope)
        except AdmissionBlockedError as exc:
            raise BudgetUnavailable(str(exc)) from exc
        if budget.risk_revision != spending.risk_revision:
            raise BudgetUnavailable("Risk limits changed. Review the current Resume budget again.")
        identity = {
            "kind": "options_resume", "account": repo.account_id, "bot": binding.strategy_instance_id,
            "configuration_hash": original["configuration_hash"], "original_cents": original["committed_cents"],
            "restored_free_cents": spending.restored_free_cents, "risk_revision": spending.risk_revision,
        }
        return DeploymentBudgetPreview(
            state="ready", detail=(
                f"Resume keeps the original ${dollars(original['committed_cents'])} budget and its lifetime results. "
                f"It reserves only the remaining ${dollars(spending.restored_free_cents)} free balance. "
                "Each future option entry still needs current approval, prices and Clerk admission."
            ),
            world=original["world"], budget_usd=dollars(original["committed_cents"]),
            unreserved_usd=dollars(spending.available_cents), observed_at_ms=spending.observed_at_ms,
            risk_revision=spending.risk_revision, review_token=canonical_sha256(identity),
            confirmation_text=(
                f"RESUME {repo.account_id} ${dollars(spending.restored_free_cents)} FREE"
                if original["world"] == "real_live" else None
            ),
        )


async def resolve_options_resume_consent(
    binding: BrokerBotBinding, budget: DeploymentBudgetInput | None,
) -> DeployBudgetConsent | None:
    """A reviewed original budget restores spending; an absent budget is manage-only."""
    if budget is None:
        return None
    runtime = get_clerk_runtime(binding.sealed_account_id or "")
    if runtime is None:
        raise BudgetUnavailable("The Options custody authority must be restored before Resume.")
    preview = _preview_on_runtime(binding, budget, runtime)
    if preview.state != "ready" or preview.review_token != budget.review_token:
        raise BudgetUnavailable("Review this Options bot's remaining free balance before restoring entry spending.")
    if preview.confirmation_text is not None and budget.live_confirmation != preview.confirmation_text:
        raise BudgetUnavailable("Enter the Live Resume confirmation displayed in the review.")
    return DeployBudgetConsent(
        committed_cents=consent_cents(budget.amount_usd), risk_revision=preview.risk_revision,
        actor=get_broker_configuration_service().owner().owner_id,
        request_fingerprint=preview.review_token, world=preview.world,
    )
