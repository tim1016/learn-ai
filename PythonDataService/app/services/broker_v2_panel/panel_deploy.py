"""Deploying a bot from the panel, and the request preflight that gates it.

The deploy path is its own concern: it authors the closed paper-deployment
form, applies the form/configuration preflight to an incoming request, and
hands the resolved parameters to the bot runner. It reads account scope
through :mod:`panel_scope` and raises :mod:`panel_errors`, so it stays
independent of the panel's read projections.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import asyncpg

from app.broker.alpaca.active_binding import get_active_alpaca_binding
from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id
from app.broker.alpaca.clerk.active_authority import (
    custody_world_or_paper,
    primary_custody_world,
)
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError
from app.broker.alpaca.clerk.sqlite.runtime import StrategyRegistrationConflictError
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.data_lake.catalog_client import CatalogUnavailableError
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.research.golden_search import qualification_service
from app.research.golden_validation import service as golden_validation_service
from app.research.persistence.db import with_connection
from app.schemas.account_authority import world_admits_account_mode
from app.schemas.broker_bots import (
    AlpacaDeploySubmission,
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployStrategy,
    AlpacaPaperDeployView,
    AlpacaPaperSizingSelection,
    BotDeployPrefill,
)
from app.schemas.deployment_budget import (
    BudgetDeployCommandReceipt,
    DeploymentBudgetPreview,
    DeploySubmissionUncommitted,
)
from app.schemas.exit_terms import ExitTermsInput
from app.schemas.run_admission import RunAdmissionDecision
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_runner import BotRunnerError, BotTaskRegistry, get_bot_task_registry
from app.services.bot_runner import UnknownBotError as RunnerUnknownBotError
from app.services.broker_v2_panel import budget_deploy
from app.services.broker_v2_panel.bot_end_panel import resolve_deploy_end
from app.services.broker_v2_panel.deploy_submissions import (
    BotNameUnavailable,
    DeploySubmission,
    DeploySubmissionConflict,
    DeploySubmissionLedger,
)
from app.services.broker_v2_panel.panel_errors import (
    AccountMismatchError,
    PanelDataError,
    PanelRunnerError,
    PanelUnavailableError,
    UnknownBotError,
)
from app.services.broker_v2_panel.panel_scope import (
    clerk_status,
    resolve_account_snapshot,
    validate_account_scope,
)
from app.services.broker_v2_panel.paper_deploy_service import (
    GoldenDefaults,
    ResolvedDeployParams,
    broker_mode_for,
    build_alpaca_paper_deploy_receipt,
    build_alpaca_paper_deploy_view,
    resolve_deploy_strategy_params,
    strategy_gate_recovery,
)
from app.services.broker_v2_panel.strategy_catalog import GoldenValidationScope
from app.services.strategy_validation_manifest import (
    StrategyValidationManifestError,
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


#: A research-store read that failed, on either Golden read Deploy makes: the
#: form then offers the registry's validated point, as it did before either existed.
_RESEARCH_READ_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.PostgresError,
    asyncpg.InterfaceError,
    CatalogUnavailableError,
    KeyError,
    OSError,
    TimeoutError,
    TypeError,
    ValueError,
)


async def _ready_golden_defaults() -> GoldenDefaults:
    """Every (program, stock) Golden Search default that is READY for the running build (#2696).

    Unreadable research records degrade to no defaults, so Deploy offers the
    registry's validated point exactly as before; the failure is logged.
    """
    try:
        return await qualification_service.ready_defaults()
    except _RESEARCH_READ_ERRORS as exc:
        logger.warning(
            "Golden Search defaults unavailable; Deploy offers the registry's validated point",
            extra={"action": "golden_search_defaults_unavailable", "error": type(exc).__name__},
        )
        return {}


def _prefer_default_scopes(
    scopes_by_strategy: dict[str, tuple[GoldenValidationScope, ...]],
    golden_defaults: GoldenDefaults,
) -> dict[str, tuple[GoldenValidationScope, ...]]:
    """Let each stock's READY Golden Search default lead that stock's Golden scopes (#2696).

    Scopes arrive newest accepted review first, and the catalog offers the
    first representable one. The case a READY default qualification cites
    (its Golden Validation run) moves to the front of its own stock's
    scopes; every other order is kept, so a stock without a ready default
    still offers its newest accepted case.
    """
    preferred = {
        (strategy_key, symbol): judged.qualification.golden_run_id
        for (strategy_key, symbol), judged in golden_defaults.items()
        if judged.status == "ready"
    }
    ordered_by_strategy: dict[str, tuple[GoldenValidationScope, ...]] = {}
    for strategy_key, scopes in scopes_by_strategy.items():
        ordered = list(scopes)
        for (default_strategy, symbol), golden_run_id in preferred.items():
            if default_strategy != strategy_key:
                continue
            index = next(
                (i for i, scope in enumerate(ordered) if scope.golden_run_id == golden_run_id and scope.symbol == symbol),
                None,
            )
            if index is None:
                continue
            first = next(i for i, scope in enumerate(ordered) if scope.symbol == symbol)
            ordered.insert(first, ordered.pop(index))
        ordered_by_strategy[strategy_key] = tuple(ordered)
    return ordered_by_strategy


async def _current_golden_validation_scopes(
    symbol: str | None,
) -> dict[str, tuple[GoldenValidationScope, ...]]:
    """Return every current, exact Golden scope whose program version is runnable.

    The catalog applies its requested-symbol filter only after it knows which
    strategies have Golden evidence. Querying a symbol-filtered subset here
    would erase that fact and let a legacy strategy-wide validation fall back
    to broker deployment for a different ticker.
    """
    del symbol
    try:
        dossiers = await with_connection(
            golden_validation_service.list_latest_accepted_dossiers,
            symbol=None,
        )
    except (*_RESEARCH_READ_ERRORS, golden_validation_service.GoldenValidationError) as exc:
        logger.warning("Golden Validation catalog projection unavailable: %s", type(exc).__name__)
        return {}

    current: dict[str, list[GoldenValidationScope]] = {}
    for dossier in dossiers:
        review = dossier.latest_review
        strategy = dossier.validation_case.get("strategy")
        strategy_name = strategy.get("name") if isinstance(strategy, dict) else None
        recorded_version = strategy.get("program_version") if isinstance(strategy, dict) else None
        authorized_version = (
            review.authorized_program_version
            if review is not None and review.decision == "accept"
            else None
        )
        registration = _STRATEGY_REGISTRY.get(strategy_name) if isinstance(strategy_name, str) else None
        contract = registration.signal_program_contract if registration is not None else None
        case_symbol = dossier.validation_case.get("symbol")
        case_parameters = dossier.validation_case.get("parameters")
        if (
            review is None
            or review.decision != "accept"
            or dossier.review_is_current is not True
            or not isinstance(strategy_name, str)
            or contract is None
            or contract.program_version != (recorded_version or authorized_version)
            or not isinstance(case_symbol, str)
            or not isinstance(case_parameters, dict)
        ):
            continue
        current.setdefault(strategy_name, []).append(
            GoldenValidationScope(
                symbol=case_symbol.upper(),
                parameters=case_parameters,
                golden_run_id=dossier.golden_run.id,
            )
        )
    return {strategy_key: tuple(scopes) for strategy_key, scopes in current.items()}


async def get_alpaca_paper_deploy_view(
    broker: str,
    account_id: str,
    symbol: str | None = None,
    exit_terms: ExitTermsInput | None = None,
) -> AlpacaPaperDeployView:
    """Author the closed paper-deployment form and its current launch verdict.

    ``symbol`` scopes the channel-health verdict. Omitted, the view reports
    account-level channel presence and connectivity only; supplied, it
    evaluates that symbol's own market-data health, warm-up included
    (#1777, finding S6).
    """
    account = await resolve_account_snapshot(broker)
    if canonical_alpaca_account_id(account.account_id) != canonical_alpaca_account_id(account_id):
        raise AccountMismatchError(
            f"Account '{account_id}' is not the account for broker '{broker}'.",
            detail=f"The broker's account is '{account.account_id}'.",
        )
    installed_world = primary_custody_world()
    custody_world = custody_world_or_paper(installed_world)
    if not world_admits_account_mode(custody_world, account.account_mode):
        world_noun = broker_mode_for(custody_world)
        raise PanelUnavailableError(
            "Alpaca account deployment is refused.",
            detail=(
                f"No Alpaca authority is installed, so deployment assumes the paper world, which does "
                f"not admit an account Alpaca reports as {account.account_mode} (ADR 0059 D1)."
                if installed_world is None
                else f"The active {world_noun} authority does not admit an account Alpaca reports as "
                f"{account.account_mode} (ADR 0059 D1)."
            ),
            next_action=(
                "Reconnect with credentials for the account this authority custodies, or activate "
                "the authority for this account, then refresh."
            ),
        )
    _runner()
    clerk = await clerk_status(symbol=symbol)
    try:
        validation_entries = load_strategy_validation_entries(strategy_registry_seeds())
    except StrategyValidationManifestError as exc:
        raise PanelUnavailableError(
            "The strategy validation catalog could not be verified.",
            detail="Deploy remains closed until current validation evidence is readable and hash-valid.",
            next_action="Restore the validation manifest and evidence artifacts, then refresh.",
        ) from exc
    context = get_active_alpaca_binding()
    golden_defaults = await _ready_golden_defaults()
    return build_alpaca_paper_deploy_view(
        account,
        clerk,
        validation_entries,
        default_exit_terms=exit_terms or (None if context is None else context.default_exit_terms),
        symbol=symbol,
        custody_world=custody_world,
        golden_validation_scopes=_prefer_default_scopes(
            await _current_golden_validation_scopes(symbol), golden_defaults
        ),
        golden_defaults=golden_defaults,
    )


async def preview_alpaca_deployment_budget(
    broker: str, account_id: str, request: AlpacaPaperDeployRequest,
) -> DeploymentBudgetPreview:
    """The Deploy form's review: the money preview, with the same-symbol warning beside it.

    The warning is read here, not in ``preview_budget``: consent re-runs that
    preview at Deploy, and a warning is no part of what consent binds.
    """
    view = await get_alpaca_paper_deploy_view(broker, account_id, request.symbol, request.exit_terms)
    resolved = _require_alpaca_deploy_request(view, request)
    try:
        preview = budget_deploy.preview_budget(account_id, request, resolved_parameters=resolved.effective)
        same_symbol_note = await asyncio.to_thread(budget_deploy.same_symbol_note, account_id, request)
    except BudgetUnavailable as exc:
        raise budget_deploy.budget_error(exc) from exc
    return preview.model_copy(update={"same_symbol_note": same_symbol_note})


def _runner() -> BotTaskRegistry:
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
            next_action="Wait for the data plane to become healthy, then refresh.",
        )
    return registry


#: The Deploys this process is sending right now, by submission key, with the
#: bot each has named so far (``None`` until its claim). Each key is sent once
#: at a time: a concurrent resend is refused rather than allowed to name or
#: start a second bot, and the recovery read answers ``in_flight`` for exactly
#: these, named or not.
_SENDING: dict[str, DeploySubmission | None] = {}

#: A second copy of a Deploy while it is being sent, and a key resent with
#: other settings. Like every refusal not marked ``submission_settled``, each
#: leaves the key's first Deploy to its recovery read.
SUBMISSION_IN_FLIGHT = "deploy_submission_in_flight"
SUBMISSION_SETTINGS_CONFLICT = "deploy_submission_settings_conflict"
#: No valid name can be authored for the bot: a refusal, not an account change.
BOT_NAME_UNAVAILABLE = "deploy_bot_name_unavailable"


def _settled(error: PanelDataError) -> PanelDataError:
    """Mark a Deploy refusal as settling its key (``PanelDataError.submission_settled``)."""
    error.submission_settled = True
    return error


@contextmanager
def _refusals_settle_the_key(nothing_started: bool) -> Iterator[None]:
    """A refusal raised inside settles the key when nothing under it can have started."""
    try:
        yield
    except PanelDataError as exc:
        if nothing_started:
            _settled(exc)
        raise


@contextmanager
def _sending(submission_key: str) -> Iterator[None]:
    if submission_key in _SENDING:
        raise PanelRunnerError(
            "This Deploy is already being sent.",
            detail="A second copy of it was not started.",
            next_action="Check its status in a moment; checking never starts a second bot.",
            http_status=409,
            reason_code=SUBMISSION_IN_FLIGHT,
        )
    _SENDING[submission_key] = None
    try:
        yield
    finally:
        del _SENDING[submission_key]


def _submission_fingerprint(account_id: str, request: AlpacaDeploySubmission) -> str:
    """What one submission key binds: the settings, the account, and the end the request named.

    The end stays out of the settings' own ``fingerprint`` -- consent and the
    budget review never bind it, since the owner may change it while the bot
    runs -- but a key resent with another end is a different Deploy, refused
    like any other change of settings (#2607). The request's own ``end`` is
    bound, not the end it resolved to, so the default end (none named) binds
    nothing, and every key recorded before the end existed still matches.
    """
    account = canonical_alpaca_account_id(account_id)
    if request.end is None:
        return request.fingerprint(account=account)
    return request.fingerprint(account=account, end=request.end.model_dump_json())


def _settings_conflict(exc: DeploySubmissionConflict) -> PanelRunnerError:
    return PanelRunnerError(
        "This Deploy was already sent with other settings.",
        detail=f"{exc} Nothing new was set aside or started.",
        next_action="Check its status; checking never starts a second bot.",
        http_status=409,
        reason_code=SUBMISSION_SETTINGS_CONFLICT,
    )


def _unnamed(exc: BotNameUnavailable) -> PanelRunnerError:
    return PanelRunnerError(
        "This bot cannot be named.", detail=str(exc), next_action=exc.next_action, http_status=409,
        reason_code=BOT_NAME_UNAVAILABLE,
    )


def _earlier_claim(ledger: DeploySubmissionLedger, account_id: str, request: AlpacaDeploySubmission) -> DeploySubmission | None:
    """What this key was claimed as before, read only; other settings under it are refused."""
    try:
        return ledger.recorded(request.submission_key, request_fingerprint=_submission_fingerprint(account_id, request))
    except DeploySubmissionConflict as exc:
        raise _settings_conflict(exc) from exc


def _claim_bot_name(
    ledger: DeploySubmissionLedger,
    account_id: str,
    request: AlpacaDeploySubmission,
    *,
    renew: DeploySubmission | None,
) -> DeploySubmission:
    """Name this submission's bot at this instant (#2551), renewing a claim that never committed."""
    try:
        return ledger.claim(
            submission_key=request.submission_key,
            symbol=request.symbol,
            strategy_key=request.strategy_key,
            request_fingerprint=_submission_fingerprint(account_id, request),
            replaces_strategy_instance_id=request.replaces_strategy_instance_id,
            now_ms=now_ms_utc(),
            renew=renew,
        )
    except DeploySubmissionConflict as exc:
        raise _settings_conflict(exc) from exc
    except BotNameUnavailable as exc:
        # No name was published under the key, and the only claim it can hold
        # is one read as never committed: nothing under it started.
        raise _settled(_unnamed(exc)) from exc


async def _committed_receipt(account_id: str, claim: DeploySubmission) -> BudgetDeployCommandReceipt | None:
    try:
        return await budget_deploy.command_receipt(account_id, claim)
    except BudgetUnavailable as exc:
        raise budget_deploy.budget_error(exc) from exc


async def deploy_alpaca_paper_bot(
    broker: str,
    account_id: str,
    request: AlpacaDeploySubmission,
) -> AlpacaPaperDeployReceipt | BudgetDeployCommandReceipt:
    """Execute or recover one durable deployment through the runner seam.

    A key whose Deploy already committed returns that receipt exactly as
    recorded, and the same key with other settings is refused, before any
    check runs. The bot is named only after every check that can refuse the
    Deploy has passed, immediately before the commit: a Deploy refused and
    retried later is named from the retry's minute (the refused attempt's
    name stays burned).

    Only a refusal raised before the claim, while nothing under the key can
    have started, is marked ``submission_settled``: before that, the key's
    earlier Deploy is not yet read; after it, the bot has a name and the
    commit may have begun.
    """
    registry = _runner()
    ledger = DeploySubmissionLedger(registry.artifacts_root)
    with _sending(request.submission_key):
        earlier = _earlier_claim(ledger, account_id, request)
        if earlier is not None and request.budget is not None:
            existing = await _committed_receipt(account_id, earlier)
            if existing is not None:
                return existing
        # The key named no bot yet, or its bot was just read as never
        # committed. A budget-less Deploy has no commit to read, so its
        # earlier bot may have started.
        with _refusals_settle_the_key(earlier is None or request.budget is not None):
            view = await get_alpaca_paper_deploy_view(broker, account_id, request.symbol, request.exit_terms)
            resolved_params = _require_alpaca_deploy_request(view, request)
            end = resolve_deploy_end(request.end, dry_run=request.execution_mode == "dry_run").end
            try:
                consent = None if request.budget is None else budget_deploy.resolve_consent(account_id, request, resolved_parameters=resolved_params.effective)
            except BudgetUnavailable as exc:
                raise budget_deploy.budget_error(exc) from exc
        # A budget-less (pre-budget) Deploy has no commit to read, so its
        # key keeps the name it was first given and the runner's own
        # identity fences judge a resend.
        claim = _claim_bot_name(ledger, account_id, request, renew=earlier if consent is not None else None)
        _SENDING[request.submission_key] = claim
        sid = claim.strategy_instance_id
        try:
            started = await registry.deploy_with_admission(
                broker=broker,
                strategy_instance_id=sid,
                strategy_key=request.strategy_key,
                symbol=request.symbol,
                use_rth=True,
                mode="dry_run" if request.execution_mode == "dry_run" else "trade",
                quantity=request.sizing.quantity,
                carryover_policy=request.carryover_policy,
                evidence_override=request.evidence_override,
                strategy_params=resolved_params.effective,
                exit_terms=request.exit_terms.seal(),
                strategy_param_origins=resolved_params.origins,
                end=end,
                **({"budget_consent": consent} if consent is not None else {}),
            )
        except (BotRunnerError, BudgetUnavailable, DurableConflictError, StrategyRegistrationConflictError, AdmissionBlockedError) as exc:
            if consent is not None:
                existing = await _committed_receipt(account_id, claim)
                if existing is not None:
                    return existing
            if not isinstance(exc, BotRunnerError):
                detail = exc.decision.why if isinstance(exc, AdmissionBlockedError) else str(exc)
                raise budget_deploy.budget_error(BudgetUnavailable(detail)) from exc
            raise PanelRunnerError(
                str(exc),
                detail=exc.detail,
                next_action="Correct the deployment inputs or bot state, then submit a new command.",
                http_status=exc.http_status,
                operation_attempted=exc.admission_decision is None,
                admission_decision=exc.admission_decision,
                reason_code=exc.reason_code,
            ) from exc
        if consent is not None:
            receipt = await _committed_receipt(account_id, claim)
            if receipt is None:
                raise budget_deploy.budget_error(BudgetUnavailable("Deployment outcome is not yet readable. Recover this command before trying again."))
            return receipt
    return build_alpaca_paper_deploy_receipt(
        broker=broker,
        view=view,
        request=request,
        strategy_instance_id=sid,
        bot=started.bot,
        admission=started.admission,
        resolved_params=resolved_params,
    )


#: The recovery read's words for a key whose Deploy has not committed, and
#: for one still being sent before it has named its bot.
_UNCOMMITTED_COPY: dict[str, tuple[str, str, str]] = {
    "in_flight": (
        "{sid} is being deployed now",
        "Nothing is committed for it yet.",
        "Check again in a moment; checking never starts a second bot.",
    ),
    "unnamed": (
        "This Deploy is being sent now",
        "Its bot is not named yet, and nothing is committed for it.",
        "Check again in a moment; checking never starts a second bot.",
    ),
    "not_committed": (
        "{sid} was not deployed",
        "Its Deploy never committed, so nothing was set aside for it.",
        "Deploy again when ready; the bot is named from the minute you do.",
    ),
}


def _uncommitted(
    status: Literal["in_flight", "not_committed"], submission_key: str, claim: DeploySubmission | None,
) -> DeploySubmissionUncommitted:
    message, explanation, next_action = _UNCOMMITTED_COPY[status if claim is not None else "unnamed"]
    return DeploySubmissionUncommitted(
        status=status, submission_key=submission_key,
        strategy_instance_id=None if claim is None else claim.strategy_instance_id,
        claimed_at_ms=None if claim is None else claim.claimed_at_ms,
        message=message if claim is None else message.format(sid=claim.strategy_instance_id),
        explanation=explanation, next_action=next_action,
    )


async def deploy_submission_status(
    account_id: str, submission_key: str,
) -> BudgetDeployCommandReceipt | DeploySubmissionUncommitted | None:
    """The recovery read: what one Deploy submission did, by its key.

    A key this process is sending is ``in_flight`` before anything else is
    read -- from the moment its Deploy arrives, named or not, so no read ever
    tells a client that a Deploy still running started nothing. Otherwise the
    committed Deploy's receipt, or the name the key still holds after
    custody is read and never committed; ``None`` only for a key never
    claimed and not being sent. With
    no bot runner there is no ledger to read, so the read is refused (503)
    rather than answered.
    """
    ledger = DeploySubmissionLedger(_runner().artifacts_root)
    while submission_key not in _SENDING:
        claim = ledger.by_key(submission_key)
        if claim is None:
            return None
        receipt = await _committed_receipt(account_id, claim)
        if receipt is not None:
            return receipt
        # A resend of the key may have begun while custody was read -- or
        # begun, renamed the key's bot and finished. Only the claim the key
        # still holds is answered ``not_committed``; a renamed one is read again.
        if submission_key not in _SENDING and ledger.by_key(submission_key) == claim:
            return _uncommitted("not_committed", submission_key, claim)
    return _uncommitted("in_flight", submission_key, _SENDING[submission_key])


async def preview_alpaca_paper_start_admission(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> RunAdmissionDecision:
    """Project the same request-specific Start decision used by execution.

    The bot has no name yet, so the checks run against the name a Deploy
    claimed now would get: they describe the bot the Deploy would create.
    """
    view = await get_alpaca_paper_deploy_view(broker, account_id, request.symbol, request.exit_terms)
    resolved_params = _require_alpaca_deploy_request(view, request)
    # The end the Deploy would refuse is refused here too (#2607).
    resolve_deploy_end(request.end, dry_run=request.execution_mode == "dry_run")
    registry = _runner()
    try:
        sid = DeploySubmissionLedger(registry.artifacts_root).provisional_name(
            symbol=request.symbol, strategy_key=request.strategy_key, now_ms=now_ms_utc(),
        )
    except BotNameUnavailable as exc:
        raise _unnamed(exc) from exc
    try:
        consent = None if request.budget is None else budget_deploy.resolve_consent(account_id, request, resolved_parameters=resolved_params.effective)
        return await registry.preview_start_admission(
            broker=broker,
            strategy_instance_id=sid,
            strategy_key=request.strategy_key,
            symbol=request.symbol,
            use_rth=True,
            mode="dry_run" if request.execution_mode == "dry_run" else "trade",
            quantity=request.sizing.quantity,
            carryover_policy=request.carryover_policy,
            evidence_override=request.evidence_override,
            strategy_params=resolved_params.effective,
            exit_terms=request.exit_terms.seal(),
            strategy_param_origins=resolved_params.origins,
            **({"budget_consent": consent} if consent is not None else {}),
        )
    except BudgetUnavailable as exc:
        raise budget_deploy.budget_error(exc) from exc
    except BotRunnerError as exc:
        raise PanelRunnerError(
            str(exc),
            detail=exc.detail,
            next_action="Correct the deployment inputs or bot state, then refresh admission.",
            http_status=exc.http_status,
            admission_decision=exc.admission_decision,
            reason_code=exc.reason_code,
        ) from exc


async def deploy_prefill(broker: str, account_id: str, sid: str) -> BotDeployPrefill:
    """Deploy again: one earlier bot's sealed settings, never its money or consent.

    Strategy, symbol and sizing come from the bot's immutable runner binding;
    exit terms from the custody authority that sealed them. Parameters are
    only the ones the owner set (see ``_owner_parameters``), so the new
    Deploy's seal records the same origins the earlier one did.
    """
    await validate_account_scope(broker, account_id, sid)
    try:
        binding = _runner().binding_for_control(broker, sid)
    except RunnerUnknownBotError as exc:
        raise UnknownBotError(str(exc), detail=exc.detail, next_action="Choose a bot this account deployed.") from exc
    try:
        terms = await budget_deploy.sealed_exit_terms(account_id, sid)
    except BudgetUnavailable as exc:
        raise budget_deploy.budget_error(exc) from exc
    return BotDeployPrefill(
        source_strategy_instance_id=sid,
        strategy_key=binding.strategy_key,
        symbol=binding.symbol,
        sizing=AlpacaPaperSizingSelection(
            preset="safe_canary" if binding.quantity == 1 else "custom", quantity=binding.quantity,
        ),
        parameters=_owner_parameters(binding),
        exit_terms=(
            None if terms is None or terms.exit_allowance_bps is None
            else ExitTermsInput.model_validate(terms.model_dump(exclude={"provenance"}))
        ),
    )


def _owner_parameters(binding: BrokerBotBinding) -> dict[str, object]:
    """The parameters the owner chose for ``binding``, as a Deploy request states them.

    With a v2 seal these are exactly the ``deploy_override`` origins: a
    registered default re-sent would be recorded as an override the owner
    never made. Without a seal the origins are unknowable, so the public
    schema's values stand in. Never ``symbol`` (the request's own field) or
    a hidden parameter, which a Deploy refuses.
    """
    registration = _STRATEGY_REGISTRY.get(binding.strategy_key)
    hidden = {"symbol", *(() if registration is None else registration.hidden_params)}
    public = {name: value for name, value in (binding.strategy_params or {}).items() if name not in hidden}
    origins = binding.strategy_param_origins
    if origins is None:
        return public
    return {name: value for name, value in public.items() if origins.get(name) == "deploy_override"}


def _require_alpaca_deploy_request(
    view: AlpacaPaperDeployView,
    request: AlpacaPaperDeployRequest,
) -> ResolvedDeployParams:
    """Apply the shared form/configuration preflight before run admission.

    The requested strategy's own identity is checked before any mode-scoped
    gate: a request naming a missing strategy must see that specific reason,
    not a mode-eligibility headline. A mode this account's view does not
    offer is then refused outright — the view's own ``execution_modes`` is
    the authority on what an account can run, and only the modes it lists
    reach a tier gate at all (ADR 0059 D2). The remaining checks are
    mode-tiered (#1702) — Dry Run and Paper / Shadow ask for what each tier
    is worth, dispatched to ``_require_dry_run_deploy_request`` /
    ``_require_broker_deploy_request``.

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
    offered = {mode.mode for mode in view.execution_modes if mode.availability == "available"}
    if request.execution_mode not in offered:
        raise PanelRunnerError(
            "The requested execution mode is not available on this account.",
            detail=f"'{request.execution_mode}' is not offered by the {view.account_label} deploy view.",
            next_action="Choose an execution mode the deploy view lists as available.",
            http_status=409,
        )
    if request.execution_mode == "dry_run":
        _require_dry_run_deploy_request(view, strategy)
    else:
        _require_broker_deploy_request(view, strategy, request)
    try:
        resolved = resolve_deploy_strategy_params(request.strategy_key, request.symbol, request.parameters)
    except ValueError as exc:
        raise PanelRunnerError(
            "The submitted strategy parameters are invalid.",
            detail=str(exc),
            next_action="Correct the highlighted parameter(s) and resubmit.",
            http_status=400,
        ) from exc
    if request.execution_mode != "dry_run" and strategy.golden_validation_scope and (
        request.symbol != strategy.validation_case_symbol
        or resolved.effective != strategy.validation_case_parameters
    ):
        raise PanelRunnerError(
            "Broker deployment must use the reviewed Golden Validation configuration.",
            detail=(
                "The selected Golden Validation authorizes only "
                f"{strategy.validation_case_symbol} with its recorded parameter values."
            ),
            next_action="Restore the reviewed symbol and parameters, or complete a new Golden Validation.",
            http_status=409,
        )
    return resolved


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


def _require_broker_deploy_request(
    view: AlpacaPaperDeployView,
    strategy: AlpacaPaperDeployStrategy,
    request: AlpacaPaperDeployRequest,
) -> None:
    """Paper / Shadow asks for the human-validated flag and full Clerk custody proof.

    An evidence-only proof additionally requires the durable human override
    (acknowledgement + reason) on the request itself: the override rides the
    binding into Start admission, which is what turns an ``evidence_only``
    validation fact into ``VERIFIED`` (operator decision 2026-08-24,
    restoring the contract #1702/#1746 had re-pointed at Live). An override
    submitted for a fully accepted proof is still rejected outright — it
    would record a risk acceptance that no gate asked for.

    A shadow run holds shadow custody and needs the identical proof: the only
    difference between the two worlds is which single broker-contacting mode
    the account's own view admits.
    """
    if request.execution_mode not in strategy.admissible_modes:
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
            "This evidence-only strategy requires the durable evidence override for Alpaca deployment.",
            detail=(
                "Its behavioral evidence has not been reconciled to the reference implementation. "
                "Record the evidence override (acknowledgement + reason) to accept that risk."
            ),
            next_action="Record the evidence override and resubmit the deployment.",
            http_status=409,
        )
    if strategy.evidence_status != "evidence_only" and request.evidence_override is not None:
        raise PanelRunnerError(
            "An evidence override is not valid for this deployment.",
            detail=(
                "This strategy's validation proof is fully accepted; the evidence-only override "
                "applies only to strategies whose behavioral evidence is not accepted."
            ),
            next_action="Remove the override and submit the strategy normally.",
            http_status=409,
        )
    if request.carryover_policy == "ALLOW" and not view.carryover_available:
        raise PanelRunnerError(
            "Exposure carryover is globally disabled for Alpaca bots.",
            detail=view.carryover_explanation,
            next_action="Deploy with carryover disabled; per-program qualification is not available yet.",
            http_status=409,
        )
