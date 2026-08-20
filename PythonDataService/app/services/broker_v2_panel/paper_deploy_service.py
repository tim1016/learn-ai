"""Pure authoring helpers for the closed Alpaca paper deployment workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.contract.models import BrokerAccountSnapshot
from app.config import settings
from app.engine.strategy.registry import _STRATEGY_REGISTRY, hidden_params_present, public_params_schema
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
from app.schemas.strategy_validation import StrategyValidationEntry, StrategyValidationFlagEvent
from app.services.bot_runner import alpaca_v1_action_plan
from app.services.bot_trade_strategy import (
    alpaca_paper_strategy_default_symbol,
    supported_alpaca_paper_strategy_keys,
)
from app.services.broker_v2_panel.panel_projection_service import evaluate_channel_health
from app.services.strategy_validation_manifest import (
    strategy_audit_copy_is_current,
    strategy_settings_file_is_current,
)
from app.utils.timestamps import now_ms_utc


@dataclass(frozen=True)
class ResolvedDeployParams:
    """One strategy's fully-resolved deploy-time parameter set.

    ``effective`` is the complete parameter set bound to the immutable
    strategy instance — registered defaults merged with the request's
    overrides, `symbol` excluded (it is carried on the binding separately).
    Storing the full set, not a sparse diff, means a later change to a
    strategy's registered defaults can never silently alter an
    already-deployed instance's behavior on Resume.
    """

    effective: dict[str, Any]
    diverges_from_defaults: tuple[str, ...]


def resolve_deploy_strategy_params(
    strategy_key: AlpacaPaperStrategyKey,
    symbol: str,
    requested_parameters: dict[str, Any],
) -> ResolvedDeployParams:
    """Validate deploy-time tunables against the strategy's own param_schema.

    The same schema Engine Lab and Strategy Lab already validate against —
    there is no second, deploy-specific parameter contract. ``symbol`` is
    always deploy-authoritative, never a submittable tunable, regardless of
    whether the registration itself declares it in ``hidden_params``.

    Raises ``ValueError`` with a human-readable message for an unknown
    strategy, a hidden/live-only parameter, or a schema validation failure —
    callers translate this into their own typed error shape.
    """
    registration = _STRATEGY_REGISTRY.get(strategy_key.value)
    if registration is None:
        raise ValueError(f"Unknown strategy '{strategy_key.value}'.")
    hidden = hidden_params_present(registration, requested_parameters, extra_hidden=frozenset({"symbol"}))
    if hidden:
        raise ValueError(f"These parameters are not editable at deploy time: {', '.join(hidden)}.")
    try:
        validated = registration.param_schema.model_validate({**requested_parameters, "symbol": symbol})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise ValueError(f"Invalid strategy parameters: {problems}.") from exc
    defaults = registration.param_schema().model_dump(exclude={"symbol"})
    effective = validated.model_dump(exclude={"symbol"})
    diverges = tuple(sorted(name for name, value in effective.items() if value != defaults.get(name)))
    return ResolvedDeployParams(effective=effective, diverges_from_defaults=diverges)


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


def _accepted_proof_blocked_reason(
    entry: StrategyValidationEntry,
    event: StrategyValidationFlagEvent,
) -> str | None:
    """Name the first reason the proof recorded at acceptance no longer verifies, or None if it still holds."""
    snapshot = event.evidence_snapshot
    diagnostics = snapshot.diagnostics
    divergent_gating = sorted(
        category for category, count in event.behavioral_equivalence.gating_divergence_counts.items() if count
    )
    divergent_diagnostics = sorted(
        category for category, count in (diagnostics.divergence_counts if diagnostics is not None else {}).items() if count
    )
    missing_snapshot_fields = [
        field_name
        for field_name, value in (
            ("validation_case_symbol", snapshot.validation_case_symbol),
            ("qc_cloud_backtest_id", snapshot.qc_cloud_backtest_id),
            ("settings_file_ref", snapshot.settings_file_ref),
            ("settings_file_sha256", snapshot.settings_file_sha256),
            ("audit_copy_ref", snapshot.audit_copy_ref),
            ("audit_copy_sha256", snapshot.audit_copy_sha256),
            ("reconciliation_ref", snapshot.reconciliation_ref),
        )
        if not value
    ]
    checks: tuple[tuple[bool, str], ...] = (
        (
            event.behavioral_equivalence.verdict == "accepted_for_deploy",
            "The recorded behavioral-equivalence verdict is no longer accepted for deploy.",
        ),
        (
            strategy_settings_file_is_current(entry),
            f"The settings/deploy binding file at '{entry.settings_file_ref}' no longer matches its recorded hash.",
        ),
        (
            strategy_audit_copy_is_current(entry),
            f"The audit copy at '{entry.audit_copy_ref}' no longer matches its recorded hash.",
        ),
        (
            not divergent_gating,
            f"The accepted validation event now records a gating divergence in: {', '.join(divergent_gating)}.",
        ),
        (
            _entry_matches_accepted_snapshot(entry),
            "The current manifest entry no longer matches the proof snapshot recorded at acceptance; "
            "the manifest was edited after acceptance.",
        ),
        (
            diagnostics is not None,
            "The accepted proof is missing its diagnostics record.",
        ),
        (
            diagnostics is not None and not divergent_diagnostics,
            f"The accepted proof's diagnostics now record a divergence in: {', '.join(divergent_diagnostics)}.",
        ),
        (
            not missing_snapshot_fields,
            f"The accepted proof snapshot is missing required field(s): {', '.join(missing_snapshot_fields)}.",
        ),
        (
            entry.deployable,
            "The strategy validation manifest no longer marks this strategy as deployable.",
        ),
    )
    for passed, reason in checks:
        if not passed:
            return reason
    return None


def _deploy_params_schema(strategy_key: AlpacaPaperStrategyKey) -> dict[str, Any]:
    """This strategy's tunable JSON schema, `symbol` always hidden (#1701)."""
    registration = _STRATEGY_REGISTRY[strategy_key.value]
    return public_params_schema(registration, extra_hidden=frozenset({"symbol"}))


def _strategy_views(
    entries: list[StrategyValidationEntry],
) -> tuple[AlpacaPaperDeployStrategy, ...]:
    """Project every runtime-supported, human-validated strategy: accepted, overridable, or blocked.

    A human validated flag always guarantees a row. Re-derivation of the
    recorded proof may demote a row to ``blocked``; it never removes it.
    """
    supported_strategy_keys = supported_alpaca_paper_strategy_keys()
    strategies: list[AlpacaPaperDeployStrategy] = []
    for entry in entries:
        event = entry.current_flag_event
        if entry.strategy_key not in supported_strategy_keys:
            continue
        if entry.validation_state != "validated" or event is None or event.flag != "validated":
            continue
        snapshot = event.evidence_snapshot
        strategy_key = AlpacaPaperStrategyKey(entry.strategy_key)
        validation_case_symbol = snapshot.validation_case_symbol or alpaca_paper_strategy_default_symbol(
            strategy_key
        )
        if event.behavioral_equivalence.verdict == "evidence_only":
            strategies.append(
                AlpacaPaperDeployStrategy(
                    strategy_key=strategy_key,
                    label=entry.display_name,
                    explanation=entry.description,
                    validation_case_symbol=validation_case_symbol,
                    evidence_status="human_override_required",
                    selectable=True,
                    override_explanation=(
                        "A human marked this strategy validated, but its behavioral evidence is "
                        "evidence-only and is not accepted for deployment. Continuing can produce "
                        "decisions that have not been reconciled to the reference implementation."
                    ),
                    params_schema=_deploy_params_schema(strategy_key),
                )
            )
            continue
        blocked_reason = _accepted_proof_blocked_reason(entry, event)
        strategies.append(
            AlpacaPaperDeployStrategy(
                strategy_key=strategy_key,
                label=entry.display_name,
                explanation=entry.description,
                validation_case_symbol=validation_case_symbol,
                evidence_status=("accepted" if blocked_reason is None else "blocked"),
                selectable=blocked_reason is None,
                blocked_explanation=blocked_reason,
                params_schema=_deploy_params_schema(strategy_key),
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
    accepted_strategies = tuple(strategy for strategy in strategies if strategy.evidence_status == "accepted")
    override_strategies = tuple(
        strategy for strategy in strategies if strategy.evidence_status == "human_override_required"
    )
    blocked_strategies = tuple(strategy for strategy in strategies if strategy.evidence_status == "blocked")
    selectable_strategies = tuple(strategy for strategy in strategies if strategy.selectable)
    account_ready = (
        account.account_mode == "paper"
        and account.account_status.upper() == "ACTIVE"
        and not account.trading_blocked
        and not account.account_blocked
    )
    freeze = clerk_status.freeze
    hold = clerk_status.hold
    channels = clerk_status.channel_healths or []
    channel_evaluation = evaluate_channel_health(clerk_status.channel_healths, now_ms)
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
            label="Strategy evidence path",
            ready=bool(selectable_strategies),
            scope="strategy",
            authority="Strategy validation current-state projection",
            headline=(
                "At least one executable strategy is selectable for deployment."
                if selectable_strategies
                else "No runtime-supported strategy is currently selectable for deployment."
            ),
            explanation=(
                (
                    "Accepted strategies use current behavioral-equivalence evidence. Evidence-only "
                    "strategies require an explicit human risk acknowledgement and operator reason. "
                    "Blocked strategies are shown but not selectable until their proof is repaired."
                )
                if selectable_strategies
                else (
                    "The selector excludes missing, invalidated, rejected, and runtime-unsupported strategies. "
                    "Blocked rows are shown but do not count toward eligibility."
                )
            ),
            evidence_summary=(
                "Selectable strategies: " + ", ".join(strategy.label for strategy in selectable_strategies) + "."
                if selectable_strategies
                else "No selectable strategy validation event is available."
            ),
            evidence={
                "accepted_strategy_keys": ", ".join(strategy.strategy_key for strategy in accepted_strategies),
                "override_strategy_keys": ", ".join(strategy.strategy_key for strategy in override_strategies),
                "blocked_strategy_keys": ", ".join(strategy.strategy_key for strategy in blocked_strategies),
            },
            recovery=(
                None
                if selectable_strategies
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
            "The operator may choose Clerk-governed paper execution or a zero-broker-write Dry Run before launch."
        ),
        next_action="Complete the deployment ticket, review the summary, then deploy the bot.",
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
                mode="dry_run",
                label="Dry Run",
                availability="available",
                explanation=(
                    "Real market data and strategy decisions produce clearly simulated fills; "
                    "the runner never calls the Clerk's broker-effect boundary."
                ),
            ),
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
    resolved_params: ResolvedDeployParams,
) -> AlpacaPaperDeployReceipt:
    """Author the terminal receipt after the runner accepts the deployment."""
    return AlpacaPaperDeployReceipt(
        status="deployed",
        receipt_id=(
            f"alpaca-paper-deploy:{view.account_id}:{request.strategy_instance_id}:{bot.binding_created_at_ms}"
        ),
        recorded_at_ms=bot.binding_created_at_ms,
        message=(
            f"{request.strategy_instance_id} is on duty in Dry Run."
            if request.execution_mode == "dry_run"
            else f"{request.strategy_instance_id} is on duty in Alpaca paper."
        ),
        explanation=(
            "The immutable binding records a human override of evidence-only strategy proof. "
            "This launch is not numerical-equivalence evidence; all other admission gates remain in force."
            if request.evidence_override is not None
            else (
                "The immutable Dry Run binding consumes market data and records only simulated activity."
                if request.execution_mode == "dry_run"
                else "The deployment binding is durable and all strategy effects are owned by the Alpaca Clerk."
            )
        ),
        next_action=(
            "Open the bot panel, inspect every early decision, and stop the bot if behavior differs from expectation."
            if request.evidence_override is not None
            else (
                "Open the bot panel and verify clearly labelled simulated decisions and fills."
                if request.execution_mode == "dry_run"
                else "Open the production bot panel and verify the first Clerk receipt."
            )
        ),
        panel_path=(f"/brokers/{broker}/accounts/{view.account_id}/bots/{request.strategy_instance_id}"),
        account_id=view.account_id,
        execution_mode=request.execution_mode,
        sizing=request.sizing,
        carryover_policy=request.carryover_policy,
        evidence_override=request.evidence_override,
        action_plan=alpaca_v1_action_plan(request.symbol),
        admission=admission,
        bot=bot,
        parameters=resolved_params.effective,
        parameters_diverge_from_defaults=resolved_params.diverges_from_defaults,
    )
