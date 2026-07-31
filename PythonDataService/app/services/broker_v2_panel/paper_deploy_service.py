"""Pure authoring helpers for the closed Alpaca paper deployment workflow."""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.contract.models import BrokerAccountSnapshot
from app.schemas.broker_bots import (
    AlpacaPaperDeployEligibility,
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployStrategy,
    AlpacaPaperDeployView,
    AlpacaPaperSizingOption,
    BotStatusView,
)
from app.services.bot_runner import alpaca_v1_action_plan


def _eligibility(
    account: BrokerAccountSnapshot,
    clerk_status: ClerkStatus,
) -> AlpacaPaperDeployEligibility:
    if (
        account.account_status.upper() != "ACTIVE"
        or account.trading_blocked
        or account.account_blocked
    ):
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code="ALPACA_ACCOUNT_NOT_TRADABLE",
            headline="Deployment is blocked by the Alpaca paper account posture.",
            explanation=(
                f"Account status is {account.account_status}; "
                f"trading_blocked={account.trading_blocked}; "
                f"account_blocked={account.account_blocked}."
            ),
            next_action="Restore the paper account to ACTIVE and unblocked, then refresh.",
        )
    if clerk_status.hold.active:
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code=clerk_status.hold.reason_code or "CLERK_HOLD_ACTIVE",
            headline="Deployment is blocked by the Alpaca Clerk.",
            explanation=clerk_status.hold.reason
            or "The Clerk is holding new order-producing activity.",
            next_action="Resolve the Clerk hold from the Operator panel, then refresh this page.",
        )
    if clerk_status.outstanding_intents:
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code="UNRESOLVED_INTENTS",
            headline="Deployment is blocked while order outcomes are unresolved.",
            explanation=(
                f"The Clerk has {clerk_status.outstanding_intents} unresolved "
                "order intent(s). Starting another bot would widen uncertainty."
            ),
            next_action="Run reconciliation and resolve every uncertain intent before deploying.",
        )
    unhealthy = [
        health
        for health in clerk_status.channel_healths or ()
        if not health.healthy
    ]
    if unhealthy:
        streams = ", ".join(health.stream for health in unhealthy)
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code="CLERK_CHANNEL_UNHEALTHY",
            headline="Deployment is blocked until Clerk channels recover.",
            explanation=f"Unhealthy channel(s): {streams}.",
            next_action="Restore the unhealthy channel and refresh the deployment check.",
        )
    return AlpacaPaperDeployEligibility(
        eligible=True,
        reason_code="ALPACA_PAPER_DEPLOY_READY",
        headline="This Alpaca paper account is eligible for a Clerk-governed deployment.",
        explanation=(
            "The bot will start in order-producing mode. Every ENTER and EXIT "
            "is executed through the Alpaca Clerk; no observation-only mode is available."
        ),
        next_action="Choose the symbol and sizing, then deploy the bot.",
    )


def build_alpaca_paper_deploy_view(
    account: BrokerAccountSnapshot,
    clerk_status: ClerkStatus,
) -> AlpacaPaperDeployView:
    """Author the closed form choices and current launch verdict."""
    eligibility = _eligibility(account, clerk_status)
    return AlpacaPaperDeployView(
        broker="alpaca",
        account_id=account.account_id,
        account_mode="paper",
        account_label=f"Alpaca paper · {account.account_id}",
        eligibility=eligibility,
        strategies=(
            AlpacaPaperDeployStrategy(
                strategy_key="deployment_validation",
                label="Deployment Validation",
                explanation=(
                    "Validated consecutive-green strategy using the canonical "
                    "deployment-validation decision kernel."
                ),
            ),
        ),
        sizing_options=(
            AlpacaPaperSizingOption(
                preset="safe_canary",
                label="Safe canary · 1 share",
                explanation="Fixed one-share sizing for the first paper deployment.",
                min_quantity=1,
                max_quantity=1,
                default_quantity=1,
            ),
            AlpacaPaperSizingOption(
                preset="custom",
                label="Bounded custom shares",
                explanation="Whole-share paper sizing, bounded from 1 through 100 shares.",
                min_quantity=1,
                max_quantity=100,
                default_quantity=1,
            ),
        ),
        action_plan_explanation=(
            "The backend will author exactly one long stock ENTER leg and one "
            "matching close-leg EXIT for the selected symbol."
        ),
        allowed_actions=("deploy",) if eligibility.eligible else (),
    )


def build_alpaca_paper_deploy_receipt(
    *,
    broker: str,
    view: AlpacaPaperDeployView,
    request: AlpacaPaperDeployRequest,
    bot: BotStatusView,
) -> AlpacaPaperDeployReceipt:
    """Author the terminal receipt after the runner accepts the deployment."""
    return AlpacaPaperDeployReceipt(
        status="deployed",
        message=f"{request.strategy_instance_id} is on duty in Alpaca paper.",
        explanation=(
            "The deployment binding is durable and all strategy effects are "
            "owned by the Alpaca Clerk."
        ),
        next_action="Open the production bot panel and verify the first Clerk receipt.",
        panel_path=(
            f"/brokers/{broker}/accounts/{view.account_id}/bots/"
            f"{request.strategy_instance_id}"
        ),
        action_plan=alpaca_v1_action_plan(request.symbol),
        bot=bot,
    )
