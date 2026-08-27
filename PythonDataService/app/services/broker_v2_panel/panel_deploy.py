"""Deploying a bot from the panel, and the request preflight that gates it.

The deploy path is its own concern: it authors the closed paper-deployment
form, applies the form/configuration preflight to an incoming request, and
hands the resolved parameters to the bot runner. It reads account scope
through :mod:`panel_scope` and raises :mod:`panel_errors`, so it stays
independent of the panel's read projections.
"""

from __future__ import annotations

import logging

from app.schemas.broker_bots import (
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployStrategy,
    AlpacaPaperDeployView,
)
from app.schemas.run_admission import RunAdmissionDecision
from app.services.bot_runner import BotRunnerError, get_bot_task_registry
from app.services.broker_v2_panel.panel_errors import (
    AccountMismatchError,
    PanelRunnerError,
    PanelUnavailableError,
)
from app.services.broker_v2_panel.panel_scope import (
    clerk_status,
    resolve_account_snapshot,
)
from app.services.broker_v2_panel.paper_deploy_service import (
    ResolvedDeployParams,
    build_alpaca_paper_deploy_receipt,
    build_alpaca_paper_deploy_view,
    resolve_deploy_strategy_params,
    strategy_gate_recovery,
)
from app.services.strategy_validation_manifest import (
    StrategyValidationManifestError,
    load_strategy_validation_entries,
    strategy_registry_seeds,
)

logger = logging.getLogger(__name__)


async def get_alpaca_paper_deploy_view(
    broker: str,
    account_id: str,
    symbol: str | None = None,
) -> AlpacaPaperDeployView:
    """Author the closed paper-deployment form and its current launch verdict.

    ``symbol`` scopes the channel-health verdict. Omitted, the view reports
    account-level channel presence and connectivity only; supplied, it
    evaluates that symbol's own market-data health, warm-up included
    (#1777, finding S6).
    """
    account = await resolve_account_snapshot(broker)
    if account.account_id != account_id:
        raise AccountMismatchError(
            f"Account '{account_id}' is not the account for broker '{broker}'.",
            detail=f"The broker's account is '{account.account_id}'.",
        )
    if account.account_mode != "paper":
        raise PanelUnavailableError(
            "Alpaca live-account deployment is refused.",
            detail="Phase 1 permits only the broker-authored paper account.",
            next_action="Reconnect with Alpaca paper credentials and refresh.",
        )
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
            next_action="Wait for the data plane to become healthy, then refresh.",
        )
    clerk = await clerk_status(symbol=symbol)
    try:
        validation_entries = load_strategy_validation_entries(strategy_registry_seeds())
    except StrategyValidationManifestError as exc:
        raise PanelUnavailableError(
            "The strategy validation catalog could not be verified.",
            detail="Deploy remains closed until current validation evidence is readable and hash-valid.",
            next_action="Restore the validation manifest and evidence artifacts, then refresh.",
        ) from exc
    return build_alpaca_paper_deploy_view(
        account, clerk, validation_entries, symbol=symbol
    )


async def deploy_alpaca_paper_bot(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> AlpacaPaperDeployReceipt:
    """Execute the production paper deployment command through the runner seam."""
    view = await get_alpaca_paper_deploy_view(broker, account_id, request.symbol)
    resolved_params = _require_alpaca_deploy_request(view, request)
    registry = get_bot_task_registry()
    if registry is None:  # guarded by the view; retained for type narrowing
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        started = await registry.deploy_with_admission(
            broker=broker,
            strategy_instance_id=request.strategy_instance_id,
            strategy_key=request.strategy_key,
            symbol=request.symbol,
            use_rth=True,
            mode="dry_run" if request.execution_mode == "dry_run" else "trade",
            quantity=request.sizing.quantity,
            carryover_policy=request.carryover_policy,
            evidence_override=request.evidence_override,
            strategy_params=resolved_params.effective,
            strategy_param_origins=resolved_params.origins,
        )
    except BotRunnerError as exc:
        raise PanelRunnerError(
            str(exc),
            detail=exc.detail,
            next_action="Correct the deployment inputs or bot state, then submit a new command.",
            http_status=exc.http_status,
            operation_attempted=exc.admission_decision is None,
            admission_decision=exc.admission_decision,
        ) from exc
    return build_alpaca_paper_deploy_receipt(
        broker=broker,
        view=view,
        request=request,
        bot=started.bot,
        admission=started.admission,
        resolved_params=resolved_params,
    )


async def preview_alpaca_paper_start_admission(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> RunAdmissionDecision:
    """Project the same request-specific Start decision used by execution."""
    view = await get_alpaca_paper_deploy_view(broker, account_id, request.symbol)
    resolved_params = _require_alpaca_deploy_request(view, request)
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        return await registry.preview_start_admission(
            broker=broker,
            strategy_instance_id=request.strategy_instance_id,
            strategy_key=request.strategy_key,
            symbol=request.symbol,
            use_rth=True,
            mode="dry_run" if request.execution_mode == "dry_run" else "trade",
            quantity=request.sizing.quantity,
            carryover_policy=request.carryover_policy,
            evidence_override=request.evidence_override,
            strategy_params=resolved_params.effective,
            strategy_param_origins=resolved_params.origins,
        )
    except BotRunnerError as exc:
        raise PanelRunnerError(
            str(exc),
            detail=exc.detail,
            next_action="Correct the deployment inputs or bot state, then refresh admission.",
            http_status=exc.http_status,
            admission_decision=exc.admission_decision,
        ) from exc


def _require_alpaca_deploy_request(
    view: AlpacaPaperDeployView,
    request: AlpacaPaperDeployRequest,
) -> ResolvedDeployParams:
    """Apply the shared form/configuration preflight before run admission.

    The requested strategy's own identity is checked before any mode-scoped
    gate: a request naming a missing strategy must see that specific reason,
    not a mode-eligibility headline. The remaining checks are mode-tiered
    (#1702) — Dry Run and Paper ask for what each tier is worth, dispatched
    to ``_require_dry_run_deploy_request`` / ``_require_paper_deploy_request``.

    Returns the resolved strategy parameter set (registered defaults merged
    with the request's overrides) so callers can thread it into both the
    runner binding and the deploy receipt without re-validating.
    """
    strategy = next(
        (strategy for strategy in view.strategies if strategy.strategy_key == request.strategy_key),
        None,
    )
    if strategy is None:
        raise PanelRunnerError(
            "The selected strategy is not currently accepted for Alpaca deployment.",
            detail="Its latest validation evidence is missing, superseded, invalidated, or not accepted for deploy.",
            next_action="Review the strategy in Strategy Validation, then refresh this page.",
            http_status=409,
        )
    if request.execution_mode == "dry_run":
        _require_dry_run_deploy_request(view, strategy)
    else:
        _require_paper_deploy_request(view, strategy, request)
    try:
        return resolve_deploy_strategy_params(request.strategy_key, request.symbol, request.parameters)
    except ValueError as exc:
        raise PanelRunnerError(
            "The submitted strategy parameters are invalid.",
            detail=str(exc),
            next_action="Correct the highlighted parameter(s) and resubmit.",
            http_status=400,
        ) from exc


def _require_dry_run_deploy_request(
    view: AlpacaPaperDeployView,
    strategy: AlpacaPaperDeployStrategy,
) -> None:
    """Dry Run asks only for a registered runtime and a healthy market-data channel.

    Human validation, account posture, custody freeze, exposure hold, and
    intent custody are all "not applicable" to Dry Run per the gate table —
    it makes no broker contact and holds no custody, so none of the Paper
    checks below apply here.
    """
    if "dry_run" not in strategy.admissible_modes:
        # A validated strategy with no registered runtime (#1703) reaches
        # here with `admissible_modes == ()` — visible in the catalog, but
        # genuinely unable to run in any mode.
        raise PanelRunnerError(
            "The selected strategy is not currently available for Dry Run.",
            detail="This strategy has no registered live-decision runtime.",
            next_action="Choose a runtime-backed strategy.",
            http_status=409,
        )
    if not view.dry_run_eligibility.eligible:
        raise PanelRunnerError(
            view.dry_run_eligibility.headline,
            detail=view.dry_run_eligibility.explanation,
            next_action=view.dry_run_eligibility.next_action,
            http_status=409,
        )


def _require_paper_deploy_request(
    view: AlpacaPaperDeployView,
    strategy: AlpacaPaperDeployStrategy,
    request: AlpacaPaperDeployRequest,
) -> None:
    """Paper asks for the human-validated flag and full Clerk custody proof.

    An evidence-only proof additionally requires the durable human override
    (acknowledgement + reason) on the request itself: the override rides the
    binding into Start admission, which is what turns an ``evidence_only``
    validation fact into ``VERIFIED`` (operator decision 2026-08-24,
    restoring the contract #1702/#1746 had re-pointed at Live). An override
    submitted for a fully accepted proof is still rejected outright — it
    would record a risk acceptance that no gate asked for.
    """
    if "paper" not in strategy.admissible_modes:
        next_action = strategy_gate_recovery((strategy,))
        assert next_action is not None
        raise PanelRunnerError(
            "The selected strategy is not currently selectable for deployment.",
            detail=strategy.blocked_explanation or "This strategy's recorded proof no longer verifies.",
            next_action=next_action,
            http_status=409,
        )
    if not view.eligibility.eligible:
        raise PanelRunnerError(
            view.eligibility.headline,
            detail=view.eligibility.explanation,
            next_action=view.eligibility.next_action,
            http_status=409,
        )
    if strategy.evidence_status == "evidence_only" and request.evidence_override is None:
        raise PanelRunnerError(
            "This evidence-only strategy requires the durable evidence override for Paper deployment.",
            detail=(
                "Its behavioral evidence has not been reconciled to the reference implementation. "
                "Record the paper-mode evidence override (acknowledgement + reason) to accept that risk."
            ),
            next_action="Record the evidence override and resubmit the deployment.",
            http_status=409,
        )
    if strategy.evidence_status != "evidence_only" and request.evidence_override is not None:
        raise PanelRunnerError(
            "An evidence override is not valid for Paper deployment.",
            detail=(
                "This strategy's validation proof is fully accepted; the evidence-only override "
                "applies only to strategies whose behavioral evidence is not accepted."
            ),
            next_action="Remove the override and submit the strategy normally.",
            http_status=409,
        )
    if request.carryover_policy == "ALLOW" and not view.carryover_available:
        raise PanelRunnerError(
            "Exposure carryover is globally disabled for Alpaca paper bots.",
            detail=view.carryover_explanation,
            next_action="Deploy with carryover disabled; per-program qualification is not available yet.",
            http_status=409,
        )
