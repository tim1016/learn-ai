"""Review and deploy an exact golden Options setup through the shared bot authority (ADR 0079)."""

from __future__ import annotations

import asyncio

from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.options_backtest.describe import describe_setup
from app.options_backtest.session_clock import full_session_clock
from app.options_bots.golden import get_ready_setup
from app.options_bots.schedule import OptionsSessionWindow, first_eligible_session
from app.schemas.action_plan import OptionsSessionActionPlan
from app.schemas.bot_end import BotEnd
from app.schemas.deployment_budget import BudgetDeployCommandReceipt
from app.schemas.golden_option_setups import GoldenOptionSetupView
from app.schemas.options_bot import OPTIONS_BOT_KEY, OptionsBotConfiguration
from app.schemas.options_deploy import OptionsDeploymentReview, OptionsDeployRequest, OptionsDeploySubmission
from app.services.bot_runner import BotRunnerError, BotTaskRegistry, get_bot_task_registry
from app.services.broker_v2_panel import budget_deploy, panel_scope
from app.services.broker_v2_panel.bot_end_panel import resolve_deploy_end
from app.services.broker_v2_panel.deploy_submissions import DeploySubmissionLedger
from app.utils.timestamps import now_ms_utc

_SENDING: dict[str, asyncio.Lock] = {}


def _configuration(golden: GoldenOptionSetupView) -> OptionsBotConfiguration:
    return OptionsBotConfiguration(
        golden_setup_id=golden.id, setup_sha256=golden.setup_sha256,
        rules_digest=golden.rules_digest, setup=golden.setup,
    )


def _plan(configuration: OptionsBotConfiguration) -> OptionsSessionActionPlan:
    return OptionsSessionActionPlan(
        kind="options_session", underlying=configuration.setup.underlying, setup_sha256=configuration.setup_sha256,
    )


def _runner() -> BotTaskRegistry:
    registry = get_bot_task_registry()
    if registry is None:
        raise BudgetUnavailable("The bot runner is unavailable on this account's Clerk.")
    return registry


def _deployment_end(request: OptionsDeployRequest, eligible: OptionsSessionWindow, *, now_ms: int) -> BotEnd | None:
    """Refuse an end that would stop the bot before it can enter, before funding or claiming."""
    end = resolve_deploy_end(
        request.end, dry_run=request.execution_mode == "dry_run", bot_kind="options", at_ms=now_ms,
    ).end
    refusal = eligible.end_refusal(None if end is None else end.end_at_ms, now_ms=now_ms)
    if refusal is not None:
        raise BudgetUnavailable(refusal)
    return end


async def review_options_bot(
    broker: str, account_id: str, request: OptionsDeployRequest,
) -> OptionsDeploymentReview:
    account_id = await panel_scope.validate_account(broker, account_id)
    golden = await get_ready_setup(request.golden_setup_id)
    configuration = _configuration(golden)
    if request.execution_mode != "dry_run":
        budget_deploy._primary(account_id).clerk.require_option_plan_taken(_plan(configuration))
    now = now_ms_utc()
    eligible = first_eligible_session(configuration.setup, at_ms=now)
    _deployment_end(request, eligible, now_ms=now)
    review = await asyncio.to_thread(
        budget_deploy.preview_options_session_budget, account_id, request,
        setup_sha256=golden.setup_sha256, rules_digest=golden.rules_digest,
    )
    allowed, explanation = False, review.detail
    registry = _runner()
    if review.state == "ready":
        consent = await asyncio.to_thread(
            budget_deploy.resolve_options_session_consent, account_id,
            request.model_copy(update={"budget": request.budget.model_copy(update={
                "review_token": review.review_token, "live_confirmation": review.confirmation_text,
            })}), setup_sha256=golden.setup_sha256, rules_digest=golden.rules_digest,
        )
        sid = DeploySubmissionLedger(registry.artifacts_root).provisional_name(
            symbol=golden.underlying, strategy_key=OPTIONS_BOT_KEY, now_ms=now,
        )
        admission = await registry.preview_start_admission(
            broker=broker, strategy_instance_id=sid, strategy_key=OPTIONS_BOT_KEY, symbol=golden.underlying,
            action_plan=_plan(configuration), options_bot=configuration, option_order_terms=request.order_terms,
            use_rth=True, mode="dry_run" if request.execution_mode == "dry_run" else "trade",
            quantity=1, carryover_policy="FORBID", exit_terms=request.exit_terms.seal(), budget_consent=consent,
        )
        allowed, explanation = admission.allowed, admission.explanation
    description = describe_setup(configuration.setup, full_session=full_session_clock(as_of_ms=now))
    rules = [*description.window[:4], *description.square_off]
    for leg in description.legs:
        rules.extend(leg.sentences)
    rules.extend((
        "Repeats on each eligible trading session; a new late deployment waits for the next session.",
        "Entry prices, partial fills and close outcomes come from the Clerk; historical fills were simulated.",
        "A different budget can change which entries are affordable compared with the selected backtest.",
    ))
    return OptionsDeploymentReview(
        golden=golden, budget=review, allowed=allowed, explanation=explanation, rules_summary=rules,
        first_eligible_session_open_ms=eligible.open_ms,
    )


async def deploy_options_bot(
    broker: str, account_id: str, request: OptionsDeploySubmission,
) -> BudgetDeployCommandReceipt:
    account_id = await panel_scope.validate_account(broker, account_id)
    registry = _runner()
    lock = _SENDING.setdefault(request.submission_key, asyncio.Lock())
    async with lock:
        ledger = DeploySubmissionLedger(registry.artifacts_root)
        fingerprint = request.fingerprint(account=account_id, world=request.execution_mode)
        earlier = ledger.recorded(request.submission_key, request_fingerprint=fingerprint)
        if earlier is not None:
            if earlier.account_id is not None and earlier.account_id != canonical_alpaca_account_id(account_id):
                raise BudgetUnavailable("This deployment command belongs to another account.")
            committed = await budget_deploy.command_receipt(account_id, earlier)
            if committed is not None:
                return committed
            if await budget_deploy.historical_run_exists(account_id, earlier.strategy_instance_id):
                raise BudgetUnavailable("This deployment has a run but its receipt is not yet readable. Recover this command.")
        golden = await get_ready_setup(request.golden_setup_id)
        configuration = _configuration(golden)
        if request.execution_mode != "dry_run":
            budget_deploy._primary(account_id).clerk.require_option_plan_taken(_plan(configuration))
        now = now_ms_utc()
        eligible = first_eligible_session(configuration.setup, at_ms=now)
        end = _deployment_end(request, eligible, now_ms=now)
        consent = await asyncio.to_thread(
            budget_deploy.resolve_options_session_consent, account_id, request,
            setup_sha256=golden.setup_sha256, rules_digest=golden.rules_digest,
        )
        claim = ledger.claim(
            submission_key=request.submission_key, account_id=account_id, symbol=golden.underlying,
            strategy_key=OPTIONS_BOT_KEY, request_fingerprint=fingerprint,
            replaces_strategy_instance_id=None, now_ms=now_ms_utc(), renew=earlier,
        )
        try:
            await registry.deploy_with_admission(
                broker=broker, strategy_instance_id=claim.strategy_instance_id, strategy_key=OPTIONS_BOT_KEY,
                symbol=golden.underlying, action_plan=_plan(configuration), options_bot=configuration,
                option_order_terms=request.order_terms, use_rth=True,
                mode="dry_run" if request.execution_mode == "dry_run" else "trade",
                quantity=1, carryover_policy="FORBID", exit_terms=request.exit_terms.seal(),
                budget_consent=consent, end=end,
            )
        except (BotRunnerError, BudgetUnavailable):
            committed = await budget_deploy.command_receipt(account_id, claim)
            if committed is not None:
                return committed
            raise
        receipt = await budget_deploy.command_receipt(account_id, claim)
        if receipt is None:
            raise BudgetUnavailable("The Options deployment outcome is not yet readable. Recover this command.")
        return receipt
