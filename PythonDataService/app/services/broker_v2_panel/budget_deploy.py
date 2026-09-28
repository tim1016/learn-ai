"""The existing Deploy form's money review and durable command recovery.

This service composes the custody calculation; it owns no cash ledger, FIFO,
fee calculator or strategy-evidence gate. All account/world choices come from
installed authorities. Browser amounts are consent, never cash observations.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id, synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.account_money import (
    AccountMoney,
    BarParts,
    BarSegment,
    MoneyBarUnavailable,
    MoneyConservationError,
    bot_parts,
    money_bar,
)
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime, get_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.budgets import budget_entry_decision, entry_requirement
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.money import (
    MoneyInputError,
    cents_required,
    cents_spendable,
    consent_cents,
    display_cents,
    dollars,
    money_context,
    normalize_money,
)
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.clerk.sqlite.day_pnl import observed_day_pnl
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness
from app.broker.alpaca.clerk.sqlite.uncertainty import admit_new_exposure
from app.broker.alpaca.regulatory_fees import RateNotPinnedError
from app.broker_configuration.runtime import get_broker_configuration_service
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.account_authority import AuthorityKind
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import (
    AccountMoneyView,
    BudgetDeployCommandReceipt,
    DeployBudgetConsent,
    DeploymentBudgetPreview,
    DeploymentBudgetShortcut,
    DeploymentBudgetView,
    MoneyParts,
    MoneySegment,
)
from app.services.bot_runner import UnknownBotError, get_bot_task_registry
from app.services.broker_v2_panel.panel_errors import PanelRunnerError
from app.services.market_liveness import prepared_top_of_book

logger = logging.getLogger(__name__)


def display_dollars(amount: Decimal) -> str:
    """Display only; this rounded value never feeds custody admission."""
    return dollars(display_cents(amount))


def _primary(account_id: str) -> ActiveClerkRuntime:
    runtime = get_active_clerk_runtime()
    custody_id = None if runtime is None else runtime.selected_account_id
    if runtime is None or custody_id is None or canonical_alpaca_account_id(custody_id.removeprefix("shadow:")) != canonical_alpaca_account_id(account_id):
        raise BudgetUnavailable("This account's custody authority is unavailable. Activate it in Configuration.")
    if runtime.sqlite_repository is None:
        raise BudgetUnavailable("Wait for this account's custody recovery to finish.")
    return runtime


def _request_world(runtime: ActiveClerkRuntime, request: AlpacaPaperDeployRequest) -> tuple[AuthorityKind, str]:
    if request.execution_mode == "dry_run":
        return "synthetic", synthetic_account_id_for_strategy(request.strategy_instance_id)
    world = runtime.account_authority_kind
    offered = {"real_paper": "paper", "real_live": "live", "shadow": "shadow"}
    if world not in offered or offered[world] != request.execution_mode:
        raise BudgetUnavailable("The account's execution world changed. Refresh Deploy and review the current world.")
    return world, runtime.selected_account_id


def request_fingerprint(request: AlpacaPaperDeployRequest, *, custody_account_id: str, world: AuthorityKind) -> str:
    payload = request.model_dump(mode="json")
    if payload["budget"] is not None:
        payload["budget"].pop("review_token", None)
        # The separately checked typed phrase proves the final click. It is
        # not part of the draft token, which is available before typing.
        payload["budget"].pop("live_confirmation", None)
    return canonical_sha256({"account": custody_account_id, "world": world, "request": payload})


def preview_budget(account_id: str, request: AlpacaPaperDeployRequest, *, resolved_parameters: dict) -> DeploymentBudgetPreview:
    runtime = _primary(account_id)
    world, custody_id = _request_world(runtime, request)
    repo = runtime.sqlite_repository
    assert repo is not None
    now = repo.clock()
    try:
        quote = prepared_top_of_book(request.symbol, now)
        if quote is None:
            # The read above asked IBKR for this symbol; its quote arrives with
            # a later snapshot, so the client re-checks instead of giving up.
            return DeploymentBudgetPreview(
                state="awaiting_price", detail="Wait for a fresh IBKR price for this instrument, then review the budget.",
                world=world, custody_account_id=custody_id,
            )
        requirement, _ = entry_requirement(quantity=request.sizing.quantity, price=quote.ask, at_ms=now)
        minimum = cents_required(requirement)
        amount = None if request.budget is None else consent_cents(request.budget.amount_usd)
        # Dry Run never draws on the account's money, so it has no bar.
        money_after: AccountMoneyView | None = None
        if world == "synthetic":
            # Private starting cash is the consent amount, never a copy of
            # parent cash. No real account risk policy is borrowed.
            available = None
            observed_at = quote.observed_at_ms
            risk_revision = 0
            risk_summary = "Private simulated starting cash. Real-account daily loss limits and holds do not apply."
        else:
            with repo.write_fence():
                if repo.budget_authority_version() < 2:
                    raise BudgetUnavailable("Switch this account to budgets in Configuration before reviewing a deployment.")
                sync = runtime.envelope_sync
                if sync is None:
                    raise BudgetUnavailable("Wait for account cash and risk observations in Configuration.")
                risk = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
                if not risk.allowed:
                    raise BudgetUnavailable(risk.detail)
                snapshot = sync.risk_snapshot()
                observation = risk.observation
                if observation is None or snapshot.policy is None:
                    raise BudgetUnavailable("Apply account risk limits in Configuration and wait for fresh cash and risk evidence.")
                projection = repo.account_budget(cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms, modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms)
                available = projection.unreserved_cents
                # Drawn under the same fence from the same observation, so the
                # bar's free to deploy is exactly this preview's unreserved cash.
                money_after = _money_view(repo, observation, world=world, account_id=account_id, new_cents=amount)
                observed_at = observation.observed_at_ms
                risk_revision = snapshot.policy.revision
                with money_context():
                    percent = normalize_money(snapshot.policy.loss_fraction) * 100
                    risk_summary = f"Daily loss limit: the smaller of {percent:f}% of prior-close equity and ${display_dollars(normalize_money(snapshot.policy.loss_usd))}. Existing exit terms stay fixed."
        with money_context():
            shortcuts = [DeploymentBudgetShortcut(
                key="position_headroom", label="1.2 × one position",
                amount_usd=dollars(cents_required(requirement * Decimal("1.2"))),
                explanation=f"1.2 × the ${dollars(minimum)} estimated position, including modelled fees. Market fills can cost more.",
            )]
            if available is not None:
                for key, label, fraction in (("quarter", "25%", Decimal("0.25")), ("half", "50%", Decimal("0.5")), ("all", "All unreserved", Decimal(1))):
                    cents = cents_spendable(Decimal(available) / 100 * fraction)
                    shortcuts.append(DeploymentBudgetShortcut(key=key, label=label, amount_usd=dollars(cents), explanation=f"{label} of ${dollars(available)} currently unreserved cash."))
        token = None
        confirmation = None
        if request.budget is not None and amount is not None:
            if request.budget.risk_revision != risk_revision:
                raise BudgetUnavailable("Risk limits changed. Review the current limits and budget again.")
            if amount < minimum:
                raise BudgetUnavailable(f"One estimated position needs at least ${dollars(minimum)}, including fees.")
            if available is not None and amount > available:
                raise BudgetUnavailable(f"Only ${dollars(available)} is unreserved. Choose a smaller budget or resolve existing claims.")
            registration = _STRATEGY_REGISTRY.get(request.strategy_key)
            contract = None if registration is None else registration.signal_program_contract
            token = canonical_sha256({
                "request": request_fingerprint(request, custody_account_id=custody_id, world=world),
                "resolved_parameters": resolved_parameters,
                "program_version": None if contract is None else contract.program_version,
            })
            if world == "real_live":
                confirmation = f"DEPLOY {account_id} ${dollars(amount)}"
        return DeploymentBudgetPreview(
            state="ready", detail="This is an entry-admission budget. Market fills and losses can exceed it.",
            world=world, custody_account_id=custody_id, observed_at_ms=observed_at,
            minimum_budget_usd=dollars(minimum), unreserved_usd=None if available is None else dollars(available),
            estimated_price_usd=display_dollars(normalize_money(quote.ask)), risk_revision=risk_revision,
            risk_limits_summary=risk_summary,
            shortcuts=tuple(shortcuts), review_token=token, confirmation_text=confirmation,
            money_after=money_after,
        )
    except (BudgetUnavailable, MoneyInputError, RateNotPinnedError) as exc:
        return DeploymentBudgetPreview(state="unavailable", detail=str(exc), world=world, custody_account_id=custody_id)


def resolve_consent(account_id: str, request: AlpacaPaperDeployRequest, *, resolved_parameters: dict) -> DeployBudgetConsent:
    preview = preview_budget(account_id, request, resolved_parameters=resolved_parameters)
    budget = request.budget
    if budget is None or preview.state != "ready" or preview.review_token is None or budget.review_token != preview.review_token:
        raise BudgetUnavailable(preview.detail if preview.state != "ready" else "Review this exact budget and configuration before Deploy.")
    if preview.world == "real_live" and budget.live_confirmation != preview.confirmation_text:
        raise BudgetUnavailable("Type the displayed account and dollar confirmation before Live Deploy.")
    return DeployBudgetConsent(
        committed_cents=consent_cents(budget.amount_usd), risk_revision=budget.risk_revision,
        actor=get_broker_configuration_service().owner().owner_id,
        request_fingerprint=request_fingerprint(request, custody_account_id=preview.custody_account_id, world=preview.world), world=preview.world,
    )


@asynccontextmanager
async def _deployment_runtime(account_id: str, sid: str) -> AsyncIterator[ActiveClerkRuntime]:
    primary = _primary(account_id)
    synthetic = get_clerk_runtime(synthetic_account_id_for_strategy(sid))
    if synthetic is not None and synthetic.sqlite_repository is not None and synthetic.sqlite_repository.deployment_budget(sid) is not None:
        yield synthetic
        return
    registry = get_bot_task_registry()
    if registry is not None:
        try:
            binding = registry.binding_for_control("alpaca", sid)
        except UnknownBotError:
            binding = None
        if binding is not None and binding.mode == "dry_run":
            # Reuse the same durable-authority reader as the bot panel. A
            # stopped Dry Run releases its process runtime, not its receipt.
            async with registry.synthetic_runtime_for_projection(binding) as runtime:
                yield _recoverable_dry_run(runtime)
            return
        assert primary.sqlite_repository is not None
        if binding is None and primary.sqlite_repository.deployment_budget(sid) is None:
            # A Dry Run commits in its private authority before the launch
            # records a binding; a crash in between leaves that authority's own
            # activation as the only way to find the committed command.
            async with registry.unbound_synthetic_runtime_for_projection(sid) as runtime:
                if runtime is not None:
                    yield _recoverable_dry_run(runtime)
                    return
    yield primary


def _recoverable_dry_run(runtime: ActiveClerkRuntime) -> ActiveClerkRuntime:
    if runtime.sqlite_repository is None:
        raise BudgetUnavailable("Dry Run custody recovery is unavailable. Restore it before recovering this command.")
    return runtime


async def command_receipt(account_id: str, sid: str, request: AlpacaPaperDeployRequest | None = None) -> BudgetDeployCommandReceipt | None:
    async with _deployment_runtime(account_id, sid) as runtime:
        return _command_receipt(runtime, account_id, sid, request)


def _command_receipt(runtime: ActiveClerkRuntime, account_id: str, sid: str, request: AlpacaPaperDeployRequest | None) -> BudgetDeployCommandReceipt | None:
    repo = runtime.sqlite_repository
    assert repo is not None
    with repo.write_fence():
        row = repo.deployment_budget(sid)
        if row is None:
            return None
        if request is not None:
            fingerprint = request_fingerprint(request, custody_account_id=repo.account_id, world=row["world"])
            if fingerprint != row["request_fingerprint"]:
                raise BudgetUnavailable("This deployment identity already has different consent. Use a fresh deployment identity.")
        command = repo.get_command(row["command_id"])
        if command is None:
            raise BudgetUnavailable("Deployment command evidence is unavailable. Resolve custody recovery before continuing.")
    state = "failed" if command.state == "failed" else ("deployed" if row["launched_at_ms"] is not None else "pending")
    return BudgetDeployCommandReceipt(
        status=state, outcome={"failed": "failure", "deployed": "success", "pending": "pending"}[state],
        receipt_id=command.command_id, command_id=command.command_id, recorded_at_ms=command.updated_at_ms,
        strategy_instance_id=sid, run_id=row["run_id"], account_id=account_id, world=row["world"],
        committed_usd=dollars(row["committed_cents"]),
        message={"failed": "Deployment failed", "deployed": "Deployment launch recorded", "pending": "Deployment committed; launch pending"}[state],
        explanation="The stored deployment result is returned unchanged. Current running and custody state are shown on the bot panel.",
        next_action="Open the bot panel to see current status and retained obligations.",
        panel_path=f"/brokers/alpaca/accounts/{account_id}/bots/{sid}",
    )


async def budget_view(account_id: str, sid: str) -> DeploymentBudgetView:
    async with _deployment_runtime(account_id, sid) as runtime:
        return _budget_view(runtime, sid)


class _EntryConfiguration(BaseModel):
    """Only custody-sealed quantity and symbol contribute to this money read."""
    symbol: str = Field(min_length=1)
    quantity: int = Field(strict=True, gt=0)


def _budget_view(runtime: ActiveClerkRuntime, sid: str) -> DeploymentBudgetView:
    repo = runtime.sqlite_repository
    assert repo is not None
    with repo.write_fence():
        return _fenced_budget_view(runtime, sid)


def _fenced_budget_view(runtime: ActiveClerkRuntime, sid: str) -> DeploymentBudgetView:
    repo = runtime.sqlite_repository
    assert repo is not None
    world = runtime.account_authority_kind
    row = repo.deployment_budget(sid)
    if row is None:
        return DeploymentBudgetView(state="legacy", detail="This earlier deployment has no budget. Stop and reconcile it, then review a fresh Deploy.", strategy_instance_id=sid, world=world)
    try:
        sync = runtime.envelope_sync
        if sync is None:
            raise BudgetUnavailable("Wait for fresh account cash and risk evidence. The original commitment is retained.")
        risk = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
        observation = _fresh_observation(repo, sync.envelope)
        projected = repo.account_budget(cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms, modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms)
        own = next(item for item in projected.deployments if item.strategy_instance_id == sid)
        eligible, detail = False, risk.detail
        if risk.allowed:
            capability = admit_new_exposure(repo, strategy_instance_id=sid)
            if not capability.allowed:
                detail = capability.why or "Resolve this deployment's custody hold before entering again."
            else:
                config = repo.bot_config(sid)
                if config is None:
                    raise BudgetUnavailable("The immutable position sizing is unavailable. Resolve deployment configuration evidence.")
                terms = _EntryConfiguration.model_validate_json(config.config_json)
                quote = prepared_top_of_book(terms.symbol, repo.clock())
                if quote is None:
                    detail = "Wait for a fresh IBKR price to judge the next position's cost."
                else:
                    decision = budget_entry_decision(projected, strategy_instance_id=sid,
                        quantity=terms.quantity, price=quote.ask, at_ms=repo.clock())
                    eligible, detail = decision.allowed, decision.detail
                    if eligible:
                        detail += " Account risk is current. Order-time strategy, session and execution checks still apply."
        with money_context():
            return DeploymentBudgetView(
                state="ready", detail=detail,
                strategy_instance_id=sid, world=world, committed_usd=dollars(own.committed_cents),
                realized_gross_usd=display_dollars(own.realized_gross), fees_usd=display_dollars(own.fees),
                position_cost_usd=display_dollars(own.position_cost), pending_orders_usd=display_dollars(own.pending_orders),
                outstanding_cash_usd=display_dollars(own.outstanding_cash),
                free_usd=dollars(own.spendable_cents), released_usd=dollars(max(0, cents_spendable(own.free))) if not own.active else "0.00",
                shortfall_usd=dollars(cents_required(max(Decimal(0), -own.free))),
                entry_eligible=eligible,
                observed_at_ms=observation.observed_at_ms,
                parts=_parts_view(bot_parts(own)),
            )
    except (BudgetUnavailable, MoneyInputError, RateNotPinnedError, ValidationError) as exc:
        return DeploymentBudgetView(state="unavailable", detail=str(exc), strategy_instance_id=sid, world=world, committed_usd=dollars(row["committed_cents"]))


def _fresh_observation(repo: ClerkSqliteRepository, envelope: LiveEnvelopeGate) -> AccountObservation:
    """The cash observation every money read judges, or why there is none.

    It is the one the Deploy preview admits against. A missing one is named
    by the one readiness check -- a missing loss limit, a loss hold, evidence
    that aged out -- and is never a zero.
    """
    observation = envelope.fresh_observation(repo.clock())
    if observation is None:
        raise BudgetUnavailable(current_risk_readiness(repo, envelope=envelope, now_ms=repo.clock()).detail)
    return observation


def _read_account_money(repo: ClerkSqliteRepository, observation: AccountObservation) -> AccountMoney:
    return repo.account_money(
        cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms,
        modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms,
    )


def account_money_view(account_id: str) -> AccountMoneyView:
    """Where this account's money is, from its own clerk (``account_money_read``)."""
    runtime = _primary(account_id)
    repo = runtime.sqlite_repository
    assert repo is not None
    world = runtime.account_authority_kind
    with repo.write_fence():
        if repo.budget_authority_version() < 2:
            return AccountMoneyView(
                state="legacy", world=world, account_id=account_id,
                detail="This account has not switched to budgets. Switch it in Settings to see where its money is.",
            )
        try:
            if runtime.envelope_sync is None:
                raise BudgetUnavailable("Wait for fresh account cash and risk evidence.")
            observation = _fresh_observation(repo, runtime.envelope_sync.envelope)
        except BudgetUnavailable as exc:
            return AccountMoneyView(state="unavailable", detail=str(exc), world=world, account_id=account_id)
        return _money_view(repo, observation, world=world, account_id=account_id)


def _money_view(
    repo: ClerkSqliteRepository, observation: AccountObservation, *, world: AuthorityKind, account_id: str,
    new_cents: int | None = None,
) -> AccountMoneyView:
    """Draw the account's bar in Python-authored dollars, or say why it cannot be.

    The caller holds the custody fence. ``new_cents`` is a proposed Deploy,
    carved out of free to deploy as a ``new`` slice.
    """
    try:
        money = _read_account_money(repo, observation)
        bar = money_bar(money, new_cents=new_cents)
    except (BudgetUnavailable, MoneyBarUnavailable, MoneyInputError, RateNotPinnedError) as exc:
        return AccountMoneyView(state="unavailable", detail=str(exc), world=world, account_id=account_id)
    except MoneyConservationError:
        # A projection bug, never a display: said loudly here and to the owner,
        # without taking the Deploy preview that embeds this bar down with it.
        logger.exception("Account money does not add up", extra={"action": "account_money_unconserved", "account_id": account_id})
        return AccountMoneyView(
            state="unavailable", world=world, account_id=account_id,
            detail="This account's money does not add up, so the bar is withheld. The fault is logged for repair.",
        )
    with money_context():
        equity = None if observation.equity_usd is None else normalize_money(observation.equity_usd)
        today = None
        if observation.equity_usd is not None and observation.last_equity_usd is not None:
            day = observed_day_pnl(observation=observation, now_ms=repo.clock())
            today = normalize_money(day.total_usd) if day.known else None
        return AccountMoneyView(
            state="ready", detail="Cash plus shares at the price paid.", world=world, account_id=account_id,
            observed_at_ms=observation.observed_at_ms,
            total_usd=dollars(bar.total_cents), cash_usd=dollars(bar.cash_cents),
            free_to_deploy_usd=dollars(bar.cents_of("free")), in_bots_usd=dollars(bar.cents_of("bot")),
            held_by_stopped_usd=dollars(bar.cents_of("stopped")), outside_bots_usd=dollars(bar.cents_of("outside")),
            account_charges_usd=dollars(bar.cents_of("charges")),
            open_pnl_usd=None if equity is None else dollars(display_cents(equity - money.total)),
            equity_usd=None if equity is None else dollars(display_cents(equity)),
            today_pnl_usd=None if today is None else dollars(display_cents(today)),
            segments=tuple(_segment_view(segment) for segment in bar.segments),
        )


def _segment_label(segment: BarSegment) -> str:
    """The legend's words for a slice: one wording per fact (PRD #2560)."""
    match segment.kind, segment.strategy_instance_id:
        case "bot", str(sid):
            return sid
        case "stopped", str(sid):
            return f"held by stopped bot {sid}"
        case "outside", None:
            return "held outside any bot"
        case "charges", None:
            return "account charges"
        case "new", None:
            return "new bot"
        case "free", None:
            return "free to deploy"
    raise ValueError(f"a {segment.kind} slice cannot name bot {segment.strategy_instance_id!r}")


def _segment_view(segment: BarSegment) -> MoneySegment:
    return MoneySegment(
        kind=segment.kind, strategy_instance_id=segment.strategy_instance_id, label=_segment_label(segment),
        amount_usd=dollars(segment.cents), share_bps=segment.bps,
        parts=None if segment.parts is None else _parts_view(segment.parts),
        shortfall_usd=None if segment.shortfall_cents is None else dollars(segment.shortfall_cents),
        released_usd=None if segment.released_cents is None else dollars(segment.released_cents),
        still_claimed_usd=None if segment.still_claimed_cents is None else dollars(segment.still_claimed_cents),
    )


def _parts_view(parts: BarParts) -> MoneyParts:
    return MoneyParts(
        in_shares_usd=dollars(parts.in_shares_cents), in_shares_bps=parts.in_shares_bps,
        pending_usd=dollars(parts.pending_cents), pending_bps=parts.pending_bps,
        free_usd=dollars(parts.free_cents), free_bps=parts.free_bps,
    )


def budget_error(exc: BudgetUnavailable) -> PanelRunnerError:
    return PanelRunnerError("Deployment budget is unavailable.", detail=str(exc), next_action="Review the current budget and account evidence, then retry.", http_status=409, operation_attempted=False)


def money_error(exc: BudgetUnavailable) -> PanelRunnerError:
    """This clerk is not serving the account's custody, so no money can be read."""
    return PanelRunnerError(
        "This account's money cannot be read right now.", detail=str(exc),
        next_action="Open the account's Settings to see why, then retry.", http_status=503, operation_attempted=False,
    )
