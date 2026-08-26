"""Pure authoring helpers for the closed Alpaca paper deployment workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError

from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.contract.models import BrokerAccountSnapshot
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
    BotStatusView,
)
from app.schemas.run_admission import RunAdmissionDecision
from app.schemas.signal_program_seal import ParameterOrigin
from app.schemas.strategy_params_schema import StrategyParamsSchema
from app.schemas.strategy_validation import StrategyValidationEntry
from app.services.bot_runner import alpaca_v1_action_plan
from app.services.broker_v2_panel.channel_health import (
    ChannelHealthEvaluation,
    evaluate_channel_connectivity,
    evaluate_channel_health,
)
from app.services.broker_v2_panel.strategy_catalog import compose_strategy_catalog
from app.utils.timestamps import now_ms_utc


class PaperDeployInvariantError(RuntimeError):
    """A deploy-view computation reached a state its own gate should prevent."""


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
    origins: dict[str, ParameterOrigin]


def strategy_gate_recovery(
    strategies: tuple[AlpacaPaperDeployStrategy, ...],
) -> str | None:
    """Return the action that can actually clear the strategy gate.

    A runtime-backed blocked row needs its evidence repaired. A visible row
    with no runtime cannot be repaired in Strategy Validation; its runtime
    must be registered instead. Keeping this distinction here gives the
    readiness view and the request preflight one recovery authority.
    """
    if any(strategy.selectable for strategy in strategies):
        return None
    if strategies and not any("dry_run" in strategy.admissible_modes for strategy in strategies):
        return (
            "Choose a runtime-backed strategy, or have an engineer register this "
            "strategy's live-decision runtime."
        )
    if any(strategy.paper_access_state == "available" for strategy in strategies):
        return "Review and enable Paper access for a strategy below."
    if any("dry_run" in strategy.admissible_modes for strategy in strategies):
        return "Repair the named proof, or re-validate the strategy in Strategy Validation."
    if strategies:
        raise PaperDeployInvariantError("A non-empty strategy catalog has no classified recovery path.")
    return "Review and validate a strategy in Strategy Validation."


def resolve_deploy_strategy_params(
    strategy_key: str,
    symbol: str,
    requested_parameters: dict[str, Any],
    *,
    symbol_profile: dict[str, Any] | None = None,
) -> ResolvedDeployParams:
    """Validate deploy-time tunables against the strategy's own param_schema.

    The same schema Engine Lab and Strategy Lab already validate against —
    there is no second, deploy-specific parameter contract. ``symbol`` is
    always deploy-authoritative, never a submittable tunable, regardless of
    whether the registration itself declares it in ``hidden_params``.

    Resolution is the exact three-tier precedence PRD Sec 10.3 requires,
    applied once here at deploy time and then sealed: registered program
    defaults, then ``symbol_profile`` (a desk's per-``(strategy_key, symbol)``
    tuning, when the caller has one), then ``requested_parameters`` (the
    operator's explicit override). No caller currently supplies a profile —
    omitting it is a clean no-op that resolves straight to the registered
    default, not a fabricated dataset.

    Raises ``ValueError`` with a human-readable message for an unknown
    strategy, a hidden/live-only parameter, or a schema validation failure —
    callers translate this into their own typed error shape.
    """
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None:
        raise ValueError(f"Unknown strategy '{strategy_key}'.")
    hidden = hidden_params_present(registration, requested_parameters, extra_hidden=frozenset({"symbol"}))
    if hidden:
        raise ValueError(f"These parameters are not editable at deploy time: {', '.join(hidden)}.")
    profile = dict(symbol_profile) if symbol_profile else {}
    try:
        validated = registration.param_schema.model_validate({**profile, **requested_parameters, "symbol": symbol})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise ValueError(f"Invalid strategy parameters: {problems}.") from exc
    defaults = registration.param_schema().model_dump(exclude={"symbol"})
    effective = validated.model_dump(exclude={"symbol"})
    diverges = tuple(sorted(name for name, value in effective.items() if value != defaults.get(name)))
    origins: dict[str, ParameterOrigin] = {}
    for name in effective:
        if name in requested_parameters:
            origins[name] = "deploy_override"
        elif name in profile:
            origins[name] = "deployment_symbol"
        else:
            origins[name] = "registered_default"
    return ResolvedDeployParams(
        effective=effective,
        diverges_from_defaults=diverges,
        origins=origins,
    )


def _deploy_params_schema(strategy_key: str) -> StrategyParamsSchema:
    """This strategy's tunable JSON schema, `symbol` always hidden (#1701)."""
    registration = _STRATEGY_REGISTRY[strategy_key]
    schema = public_params_schema(registration, extra_hidden=frozenset({"symbol"}))
    return StrategyParamsSchema.model_validate(schema)


def _admissible_modes(*, selectable: bool, has_runtime: bool) -> tuple[Literal["dry_run", "paper"], ...]:
    """Derive the wire-facing mode set from the catalog's own launch facts (#1702, #1703)."""
    if selectable:
        return ("dry_run", "paper")
    if has_runtime:
        return ("dry_run",)
    return ()


def _strategy_views(
    entries: list[StrategyValidationEntry],
    *,
    account_id: str,
) -> tuple[AlpacaPaperDeployStrategy, ...]:
    """Project the composed strategy catalog into deploy-wire rows.

    Entity composition (the definition + validation-state facets, the
    runtime annotation, and the evidence-disposition derivation) is owned
    by ``strategy_catalog.compose_strategy_catalog``; this function adds
    only the deploy-specific wire shape — ``params_schema`` and the
    mode-explicit ``admissible_modes`` tuple derived from
    ``selectable`` / ``has_runtime``.
    """
    return tuple(
        AlpacaPaperDeployStrategy(
            strategy_key=entry.strategy_key,
            label=entry.label,
            explanation=entry.explanation,
            validation_case_symbol=entry.validation_case_symbol,
            evidence_status=entry.evidence_status,
            paper_access_state=entry.paper_access_state,
            selectable=entry.selectable,
            admissible_modes=_admissible_modes(selectable=entry.selectable, has_runtime=entry.has_runtime),
            override_explanation=entry.override_explanation,
            blocked_explanation=entry.blocked_explanation,
            params_schema=_deploy_params_schema(entry.strategy_key),
        )
        for entry in compose_strategy_catalog(entries, account_id=account_id)
    )


def _channel_evaluation(
    clerk_status: ClerkStatus,
    now_ms: int,
    *,
    symbol: str | None,
    required_streams: tuple[str, ...] | None = None,
) -> ChannelHealthEvaluation:
    """Pick the channel question this view's scope is entitled to ask.

    A symbol-scoped view judges *usability* for that symbol — warm-up
    included. The account-level view judges only presence and connectivity,
    because per-symbol warm-up is not an account-level fact and must not
    refuse every deploy on the account (#1777, finding S6).
    """
    kwargs = {} if required_streams is None else {"required_streams": required_streams}
    if symbol is None:
        return evaluate_channel_connectivity(clerk_status.channel_healths, now_ms, **kwargs)
    return evaluate_channel_health(clerk_status.channel_healths, now_ms, **kwargs)


def _readiness_checks(
    account: BrokerAccountSnapshot,
    clerk_status: ClerkStatus,
    strategies: tuple[AlpacaPaperDeployStrategy, ...],
    *,
    now_ms: int,
    symbol: str | None,
) -> tuple[AlpacaPaperDeployReadinessCheck, ...]:
    accepted_strategies = tuple(strategy for strategy in strategies if strategy.evidence_status == "accepted")
    override_strategies = tuple(
        strategy for strategy in strategies if strategy.evidence_status == "evidence_only"
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
    channel_evaluation = _channel_evaluation(clerk_status, now_ms, symbol=symbol)
    channel_ready = channel_evaluation.ready
    failing = channel_evaluation.failing
    channel_summary = (
        ", ".join(
            f"{channel.stream.replace('_', ' ').title()} is "
            # The sample's own reason names the symbol; surfacing it is what
            # makes a symbol-scoped refusal legible (#1777 decision 4).
            + (
                f"unhealthy ({channel.reason})"
                if channel.stream in failing and channel.reason
                else "unhealthy"
                if channel.stream in failing
                else "healthy"
            )
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
                else "No strategy is currently selectable for deployment."
            ),
            explanation=(
                (
                    "Accepted strategies use current behavioral-equivalence evidence. Evidence-only "
                    "strategies are Paper-selectable on the human-validated flag alone; their behavioral "
                    "verdict remains displayed but does not gate Paper. Blocked strategies are shown but "
                    "not selectable — either their proof no longer verifies, or they have no registered "
                    "live-decision runtime yet."
                )
                if selectable_strategies
                else (
                    "The selector excludes missing, invalidated, and rejected strategies. A validated "
                    "strategy always gets a row — blocked, with a reason, when its proof no longer "
                    "verifies or it has no registered runtime — but blocked rows never count toward "
                    "eligibility."
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
            recovery=strategy_gate_recovery(strategies),
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


def _dry_run_eligibility(
    strategies: tuple[AlpacaPaperDeployStrategy, ...],
    clerk_status: ClerkStatus,
    *,
    now_ms: int,
    symbol: str | None,
) -> AlpacaPaperDeployEligibility:
    """Author the Dry Run launch verdict — deliberately narrower than Paper's.

    Per the gate table (#1702), Dry Run requires only a registered runtime
    and a healthy market-data channel. It does not consider account posture,
    Clerk freeze, Clerk hold, outstanding intents, or the execution channel —
    none of those are "not applicable" to a mode that makes no broker
    contact and holds no custody.
    """
    market_data_ready = _channel_evaluation(
        clerk_status, now_ms, symbol=symbol, required_streams=("market_data",)
    ).ready
    # #1703: a visible catalog row no longer implies a registered runtime —
    # a validated-but-no-runtime row is now composed (not dropped), so
    # `bool(strategies)` alone can no longer stand in for "any runtime is
    # available". Check `admissible_modes` directly instead.
    any_runtime = any("dry_run" in strategy.admissible_modes for strategy in strategies)
    if not any_runtime:
        next_action = strategy_gate_recovery(strategies)
        if next_action is None:
            raise PaperDeployInvariantError(
                "No strategy is admissible for Dry Run, but strategy_gate_recovery reported no "
                "recovery hint; a selectable strategy always makes 'dry_run' admissible, so "
                "this should be unreachable."
            )
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code="STRATEGY_NOT_ACCEPTED_FOR_DEPLOY",
            headline="No runtime-backed strategy is currently available for Dry Run.",
            explanation=(
                "Every visible strategy is either unvalidated, or validated with no registered "
                "live-decision runtime yet."
            ),
            next_action=next_action,
        )
    if not market_data_ready:
        return AlpacaPaperDeployEligibility(
            eligible=False,
            reason_code="CLERK_CHANNEL_UNHEALTHY",
            headline="Deployment is blocked until the Clerk market-data channel is healthy.",
            explanation="Dry Run consumes real market data; it cannot simulate decisions without it.",
            next_action="Restore the market-data channel and refresh the deployment check.",
        )
    return AlpacaPaperDeployEligibility(
        eligible=True,
        reason_code="ALPACA_DRY_RUN_READY",
        headline="A zero-broker-write Dry Run is available.",
        explanation="A registered strategy and a healthy market-data channel are present; Dry Run holds no custody.",
        next_action="Complete the deployment ticket, review the summary, then start the Dry Run.",
    )


def build_alpaca_paper_deploy_view(
    account: BrokerAccountSnapshot,
    clerk_status: ClerkStatus,
    validation_entries: list[StrategyValidationEntry],
    *,
    symbol: str | None = None,
) -> AlpacaPaperDeployView:
    """Author the closed form choices and current launch verdict."""
    evaluated_at_ms = now_ms_utc()
    strategies = _strategy_views(validation_entries, account_id=account.account_id)
    readiness_checks = _readiness_checks(
        account,
        clerk_status,
        strategies,
        now_ms=evaluated_at_ms,
        symbol=symbol,
    )
    eligibility = _eligibility(readiness_checks)
    dry_run_eligibility = _dry_run_eligibility(
        strategies, clerk_status, now_ms=evaluated_at_ms, symbol=symbol
    )
    return AlpacaPaperDeployView(
        broker="alpaca",
        account_id=account.account_id,
        account_mode="paper",
        account_label=f"Alpaca paper · {account.account_id}",
        evaluated_at_ms=evaluated_at_ms,
        eligibility=eligibility,
        dry_run_eligibility=dry_run_eligibility,
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
        carryover_available=False,
        carryover_label="Exposure carryover is not yet qualified",
        carryover_explanation=(
            "Carryover is globally disabled until a separately reviewed per-program "
            "replay and restart-safety qualification is complete. STOP with exposure "
            "requires a Clerk-proven flatten before Resume."
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
