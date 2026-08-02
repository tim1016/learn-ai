"""Pure authoring helpers for the closed Alpaca paper deployment workflow."""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.contract.models import BrokerAccountSnapshot
from app.config import settings
from app.schemas.broker_bots import (
    AlpacaPaperDeployEligibility,
    AlpacaPaperDeployReadinessCheck,
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployStrategy,
    AlpacaPaperDeployView,
    AlpacaPaperExecutionMode,
    AlpacaPaperSizingOption,
    AlpacaPaperStrategyKey,
    BotStatusView,
)
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.strategy_validation import StrategyValidationEntry
from app.services.bot_runner import alpaca_v1_action_plan
from app.services.bot_trade_strategy import supported_alpaca_paper_strategy_keys
from app.services.broker_v2_panel.panel_projection_service import evaluate_channel_health
from app.services.strategy_validation_manifest import strategy_audit_copy_is_current
from app.utils.timestamps import now_ms_utc


def _entry_matches_accepted_snapshot(entry: StrategyValidationEntry) -> bool:
    """Require the deploy projection to use exactly the human-accepted proof."""
    event = entry.current_flag_event
    if event is None:
        return False
    snapshot = event.evidence_snapshot
    return (
        entry.validator_code_ref == snapshot.validator_code_ref
        and entry.validator_code_sha256 == snapshot.validator_code_sha256
        and entry.settings_file_ref == snapshot.settings_file_ref
        and entry.settings_file_sha256 == snapshot.settings_file_sha256
        and entry.qc_cloud_backtest_id == snapshot.qc_cloud_backtest_id
        and entry.audit_copy_ref == snapshot.audit_copy_ref
        and entry.audit_copy_sha256 == snapshot.audit_copy_sha256
        and entry.reconciliation_ref == snapshot.reconciliation_ref
        and entry.validation_case_symbol == snapshot.validation_case_symbol
        and entry.reconciliation_status == snapshot.reconciliation_status
        and entry.diagnostics == snapshot.diagnostics
    )


def _strategy_views(
    entries: list[StrategyValidationEntry],
) -> tuple[AlpacaPaperDeployStrategy, ...]:
    """Project only runtime-supported strategies with current accepted evidence."""
    supported_strategy_keys = supported_alpaca_paper_strategy_keys()
    strategies: list[AlpacaPaperDeployStrategy] = []
    for entry in entries:
        event = entry.current_flag_event
        snapshot = event.evidence_snapshot if event is not None else None
        diagnostics = snapshot.diagnostics if snapshot is not None else None
        if (
            entry.strategy_key not in supported_strategy_keys
            or entry.validation_state != "validated"
            or not entry.deployable
            or event is None
            or event.flag != "validated"
            or event.behavioral_equivalence.verdict != "accepted_for_deploy"
            or any(event.behavioral_equivalence.gating_divergence_counts.values())
            or not _entry_matches_accepted_snapshot(entry)
            or diagnostics is None
            or any(diagnostics.divergence_counts.values())
            or not snapshot.validation_case_symbol
            or not snapshot.qc_cloud_backtest_id
            or not snapshot.settings_file_ref
            or not snapshot.settings_file_sha256
            or not snapshot.audit_copy_ref
            or not snapshot.audit_copy_sha256
            or not snapshot.reconciliation_ref
            or not strategy_audit_copy_is_current(entry)
        ):
            continue
        strategies.append(
            AlpacaPaperDeployStrategy(
                strategy_key=AlpacaPaperStrategyKey(entry.strategy_key),
                label=entry.display_name,
                explanation=entry.description,
                validation_case_symbol=snapshot.validation_case_symbol,
            )
        )
    return tuple(strategies)


def _readiness_checks(
    account: BrokerAccountSnapshot,
    clerk_status: ClerkStatus,
    strategies: tuple[AlpacaPaperDeployStrategy, ...],
    *,
    now_ms: int,
) -> tuple[AlpacaPaperDeployReadinessCheck, ...]:
    account_ready = (
        account.account_mode == "paper"
        and account.account_status.upper() == "ACTIVE"
        and not account.trading_blocked
        and not account.account_blocked
    )
    freeze = clerk_status.freeze
    hold = clerk_status.hold
    channels = clerk_status.channel_healths or []
    channel_evaluation = evaluate_channel_health(clerk_status, now_ms)
    channel_ready = channel_evaluation.ready
    channel_summary = (
        ", ".join(
            f"{channel.stream.replace('_', ' ').title()} is {'healthy' if channel.healthy else 'unhealthy'}"
            for channel in channels
        )
        or "no channel observations"
    )
    return (
        AlpacaPaperDeployReadinessCheck(
            gate_id="strategy.validation_accepted",
            label="Accepted strategy evidence",
            ready=bool(strategies),
            scope="strategy",
            authority="Strategy validation current-state projection",
            headline=(
                "Validated strategies are accepted for deployment."
                if strategies
                else "No runtime-supported strategy has current accepted validation evidence."
            ),
            explanation=(
                "The latest human validation event is validated and its behavioral-equivalence verdict is accepted for deploy."
                if strategies
                else "The selector excludes missing, superseded, invalidated, evidence-only, and rejected validation events."
            ),
            evidence_summary=(
                "Current accepted evidence: "
                + ", ".join(strategy.label for strategy in strategies)
                + "."
                if strategies
                else "No accepted strategy validation event is available."
            ),
            evidence={
                "strategy_keys": ", ".join(strategy.strategy_key for strategy in strategies),
                "verdict": "accepted_for_deploy" if strategies else None,
            },
            recovery=(
                None
                if strategies
                else "Review and accept current behavioral-equivalence evidence in Strategy Validation."
            ),
        ),
        AlpacaPaperDeployReadinessCheck(
            gate_id="broker.account_posture",
            label="Paper account posture",
            ready=account_ready,
            scope="account",
            authority="Alpaca account snapshot",
            headline=(
                "The Alpaca paper account is active and tradable."
                if account_ready
                else "Deployment is blocked by the Alpaca paper account posture."
            ),
            explanation=(
                "The server resolved the selected paper account and found no broker trading block."
                if account_ready
                else (
                    f"Account mode is {account.account_mode}; status is {account.account_status}; "
                    f"trading_blocked={account.trading_blocked}; account_blocked={account.account_blocked}."
                )
            ),
            evidence_summary=(f"Alpaca paper account {account.account_id} reports status {account.account_status}."),
            evidence={
                "account_id": account.account_id,
                "mode": account.account_mode,
                "status": account.account_status,
                "trading_blocked": account.trading_blocked,
                "account_blocked": account.account_blocked,
            },
            recovery=None if account_ready else "Restore the paper account to ACTIVE and unblocked, then refresh.",
        ),
        AlpacaPaperDeployReadinessCheck(
            gate_id="clerk.custody_freeze",
            label="Clerk custody proof",
            ready=not freeze.active,
            scope="account",
            authority="Alpaca Clerk durable freeze",
            headline=(
                "The Clerk can currently prove account custody."
                if not freeze.active
                else "Deployment is blocked by a durable Alpaca account freeze."
            ),
            explanation=(
                "No account freeze is active."
                if not freeze.active
                else (freeze.explanation or "The Clerk cannot prove current account custody.")
            ),
            evidence_summary=(
                "No durable account freeze is active."
                if not freeze.active
                else f"Freeze category: {freeze.category or 'unclassified'}."
            ),
            evidence={"freeze_active": freeze.active, "category": freeze.category},
            recovery=(
                None
                if not freeze.active
                else (freeze.next_step or "Run Clerk recovery and reconciliation before deploying.")
            ),
        ),
        AlpacaPaperDeployReadinessCheck(
            gate_id="clerk.exposure_hold",
            label="Account hold",
            ready=not hold.active,
            scope="account",
            authority="Alpaca Clerk exposure hold",
            headline=(
                "No account exposure hold is active."
                if not hold.active
                else "Deployment is blocked by the Alpaca Clerk."
            ),
            explanation=(
                "The Clerk is accepting new order-producing activity."
                if not hold.active
                else (hold.reason or "The Clerk is holding new order-producing activity.")
            ),
            evidence_summary=(
                "No Clerk exposure hold is active." if not hold.active else "An active Clerk exposure hold is recorded."
            ),
            evidence={"hold_active": hold.active, "reason_code": hold.reason_code},
            recovery=(
                None if not hold.active else "Resolve the Clerk hold from the Operator panel, then refresh this page."
            ),
        ),
        AlpacaPaperDeployReadinessCheck(
            gate_id="clerk.intent_custody",
            label="Intent custody",
            ready=clerk_status.outstanding_intents == 0,
            scope="account",
            authority="Alpaca Clerk intent journal",
            headline=(
                "Every recorded order intent has a known outcome."
                if clerk_status.outstanding_intents == 0
                else "Deployment is blocked while order outcomes are unresolved."
            ),
            explanation=f"Outstanding intents: {clerk_status.outstanding_intents}.",
            evidence_summary=f"The Clerk reports {clerk_status.outstanding_intents} unresolved intent(s).",
            evidence={"outstanding_intents": clerk_status.outstanding_intents},
            recovery=(
                None
                if clerk_status.outstanding_intents == 0
                else "Run reconciliation and resolve every uncertain intent before deploying."
            ),
        ),
        AlpacaPaperDeployReadinessCheck(
            gate_id="clerk.channel_health",
            label="Clerk channels",
            ready=channel_ready,
            scope="broker",
            authority="Alpaca Clerk submission gate",
            headline=(
                "Market-data and execution channels are healthy."
                if channel_ready
                else "Deployment is blocked until Clerk channels are installed and healthy."
            ),
            explanation=f"Current channel observations: {channel_summary}.",
            evidence_summary=f"{len(channels)} channel observation(s): {channel_summary}.",
            evidence={
                "channel_count": len(channels),
                "channels": channel_summary,
                "missing_channels": ", ".join(channel_evaluation.missing) or "none",
                "stale_channels": ", ".join(channel_evaluation.stale) or "none",
                "unhealthy_channels": ", ".join(channel_evaluation.unhealthy) or "none",
            },
            recovery=None if channel_ready else "Restore both Clerk channels and refresh the deployment check.",
        ),
    )


def _eligibility(
    checks: tuple[AlpacaPaperDeployReadinessCheck, ...],
) -> AlpacaPaperDeployEligibility:
    blocked = next((check for check in checks if not check.ready), None)
    if blocked is not None:
        reason_codes = {
            "strategy.validation_accepted": "STRATEGY_NOT_ACCEPTED_FOR_DEPLOY",
            "broker.account_posture": "ALPACA_ACCOUNT_NOT_TRADABLE",
            "clerk.custody_freeze": str(blocked.evidence.get("category") or "ACCOUNT_STATE_UNPROVABLE"),
            "clerk.exposure_hold": str(blocked.evidence.get("reason_code") or "CLERK_HOLD_ACTIVE"),
            "clerk.intent_custody": "UNRESOLVED_INTENTS",
            "clerk.channel_health": "CLERK_CHANNEL_UNHEALTHY",
        }
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code=reason_codes[blocked.gate_id],
            headline=blocked.headline,
            explanation=blocked.explanation,
            next_action=blocked.recovery or "Refresh after the blocking condition is resolved.",
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
    validation_entries: list[StrategyValidationEntry],
) -> AlpacaPaperDeployView:
    """Author the closed form choices and current launch verdict."""
    evaluated_at_ms = now_ms_utc()
    strategies = _strategy_views(validation_entries)
    readiness_checks = _readiness_checks(
        account,
        clerk_status,
        strategies,
        now_ms=evaluated_at_ms,
    )
    eligibility = _eligibility(readiness_checks)
    return AlpacaPaperDeployView(
        broker="alpaca",
        account_id=account.account_id,
        account_mode="paper",
        account_label=f"Alpaca paper · {account.account_id}",
        evaluated_at_ms=evaluated_at_ms,
        eligibility=eligibility,
        readiness_checks=readiness_checks,
        execution_modes=(
            AlpacaPaperExecutionMode(
                mode="paper",
                label="Paper",
                availability="available",
                explanation="Orders route only to the selected Alpaca paper account through the Clerk.",
            ),
            AlpacaPaperExecutionMode(
                mode="live",
                label="Live",
                availability="planned",
                explanation="Live Alpaca execution is planned but is not connected to an admission or execution path.",
            ),
        ),
        strategies=strategies,
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
        carryover_available=settings.ALPACA_PAPER_CARRYOVER_ENABLED,
        carryover_label="Allow Clerk-proven exposure carryover on STOP",
        carryover_explanation=(
            "Default off. When enabled, STOP may preserve exactly attributed "
            "exposure only after a durable Clerk checkpoint; Resume still "
            "requires a fresh exact proof."
            if settings.ALPACA_PAPER_CARRYOVER_ENABLED
            else (
                "Account policy currently forbids carried exposure. STOP with "
                "exposure requires a Clerk flatten before Resume."
            )
        ),
        allowed_actions=("deploy",) if eligibility.eligible else (),
    )


def build_alpaca_paper_deploy_receipt(
    *,
    broker: str,
    view: AlpacaPaperDeployView,
    request: AlpacaPaperDeployRequest,
    bot: BotStatusView,
    admission: RunAdmissionDecision,
) -> AlpacaPaperDeployReceipt:
    """Author the terminal receipt after the runner accepts the deployment."""
    return AlpacaPaperDeployReceipt(
        status="deployed",
        receipt_id=(
            f"alpaca-paper-deploy:{view.account_id}:{request.strategy_instance_id}:{bot.binding_created_at_ms}"
        ),
        recorded_at_ms=bot.binding_created_at_ms,
        message=f"{request.strategy_instance_id} is on duty in Alpaca paper.",
        explanation=("The deployment binding is durable and all strategy effects are owned by the Alpaca Clerk."),
        next_action="Open the production bot panel and verify the first Clerk receipt.",
        panel_path=(f"/brokers/{broker}/accounts/{view.account_id}/bots/{request.strategy_instance_id}"),
        account_id=view.account_id,
        sizing=request.sizing,
        carryover_policy=request.carryover_policy,
        action_plan=alpaca_v1_action_plan(request.symbol),
        admission=admission,
        bot=bot,
    )
