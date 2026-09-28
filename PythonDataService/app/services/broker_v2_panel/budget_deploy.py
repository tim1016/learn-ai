"""The existing Deploy form's money review and durable command recovery.

This service composes the custody calculation; it owns no cash ledger, FIFO,
fee calculator or strategy-evidence gate. All account/world choices come from
installed authorities. Browser amounts are consent, never cash observations.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal, Inexact, localcontext

from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id, synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime, get_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.budgets import entry_requirement
from app.broker.alpaca.clerk.money import (
    MoneyInputError,
    cents_required,
    cents_spendable,
    consent_cents,
    money_context,
    normalize_money,
)
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_projection import BudgetUnavailable
from app.broker.alpaca.regulatory_fees import RateNotPinnedError
from app.broker_configuration.runtime import get_broker_configuration_service
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.account_authority import AuthorityKind
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import (
    BudgetDeployCommandReceipt,
    DeployBudgetConsent,
    DeploymentBudgetPreview,
    DeploymentBudgetShortcut,
    DeploymentBudgetView,
)
from app.services.bot_runner import UnknownBotError, get_bot_task_registry
from app.services.broker_v2_panel.panel_errors import PanelRunnerError
from app.services.market_liveness import get_market_liveness_store


def dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    value = abs(cents)
    return f"{sign}{value // 100}.{value % 100:02d}"


def display_dollars(amount: Decimal) -> str:
    """Display only; this rounded value never feeds custody admission."""
    with money_context(), localcontext() as context:
        context.traps[Inexact] = False
        return str(amount.quantize(Decimal("0.01")))


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
        quote = get_market_liveness_store().top_of_book(symbol=request.symbol, now_ms=now)
        if quote is None:
            raise BudgetUnavailable("Wait for a fresh IBKR price for this instrument, then review the budget.")
        requirement, _ = entry_requirement(quantity=request.sizing.quantity, price=quote.ask, at_ms=now)
        minimum = cents_required(requirement)
        if world == "synthetic":
            # Private starting cash is the consent amount, never a copy of
            # parent cash. No real account risk policy is borrowed.
            available = None
            observed_at = quote.observed_at_ms
            risk_revision = 0
            risk_summary = "Private simulated starting cash. Real-account daily loss limits and holds do not apply."
        else:
            if repo.budget_authority_version() < 2:
                raise BudgetUnavailable("Switch this account to budgets in Configuration before reviewing a deployment.")
            sync = runtime.envelope_sync
            if sync is None:
                raise BudgetUnavailable("Wait for account cash and risk observations in Configuration.")
            snapshot = sync.risk_snapshot()
            observation = snapshot.observation
            if observation is None or snapshot.policy is None:
                raise BudgetUnavailable("Apply account risk limits in Configuration and wait for fresh cash and risk evidence.")
            if snapshot.hold is not None:
                raise BudgetUnavailable("A standing account loss hold blocks Deploy. Review and clear it in Configuration when the evidence permits.")
            projection = repo.account_budget(cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms, modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms)
            available = projection.unreserved_cents
            observed_at = observation.observed_at_ms
            risk_revision = snapshot.policy.revision
            with money_context():
                percent = normalize_money(snapshot.policy.loss_fraction) * 100
                risk_summary = f"Daily loss limit: the smaller of {percent:f}% of session-start equity and ${display_dollars(normalize_money(snapshot.policy.loss_usd))}. Existing exit terms stay fixed."
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
        if request.budget is not None:
            amount = consent_cents(request.budget.amount_usd)
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
                if runtime.sqlite_repository is None:
                    raise BudgetUnavailable("Dry Run custody recovery is unavailable. Restore it before recovering this command.")
                yield runtime
            return
    yield primary


async def command_receipt(account_id: str, sid: str, request: AlpacaPaperDeployRequest | None = None) -> BudgetDeployCommandReceipt | None:
    async with _deployment_runtime(account_id, sid) as runtime:
        return _command_receipt(runtime, account_id, sid, request)


def _command_receipt(runtime: ActiveClerkRuntime, account_id: str, sid: str, request: AlpacaPaperDeployRequest | None) -> BudgetDeployCommandReceipt | None:
    repo = runtime.sqlite_repository
    assert repo is not None
    with repo._write_lock:
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


def _budget_view(runtime: ActiveClerkRuntime, sid: str) -> DeploymentBudgetView:
    repo = runtime.sqlite_repository
    assert repo is not None
    world = runtime.account_authority_kind
    row = repo.deployment_budget(sid)
    if row is None:
        return DeploymentBudgetView(state="legacy", detail="This earlier deployment has no budget. Stop and reconcile it, then review a fresh Deploy.", strategy_instance_id=sid, world=world)
    try:
        sync = runtime.envelope_sync
        observation = None if sync is None else sync.risk_snapshot().observation
        if observation is None:
            raise BudgetUnavailable("Wait for fresh account cash and risk evidence. The original commitment is retained.")
        projected = repo.account_budget(cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms, modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms)
        own = next(item for item in projected.deployments if item.strategy_instance_id == sid)
        with money_context():
            return DeploymentBudgetView(
                state="ready", detail="Budget remains attributed to this deployment. Stop does not mean flat or fully settled.",
                strategy_instance_id=sid, world=world, committed_usd=dollars(own.committed_cents),
                realized_gross_usd=display_dollars(own.realized_gross), fees_usd=display_dollars(own.fees),
                position_cost_usd=display_dollars(own.position_cost), pending_orders_usd=display_dollars(own.pending_orders),
                outstanding_cash_usd=display_dollars(own.outstanding_cash),
                free_usd=dollars(own.spendable_cents), released_usd=dollars(max(0, cents_spendable(own.free))) if not own.active else "0.00",
                shortfall_usd=dollars(cents_required(max(Decimal(0), -own.free))),
                entry_eligible=own.active and own.free > 0 and projected.available >= 0,
                observed_at_ms=observation.observed_at_ms,
            )
    except (BudgetUnavailable, MoneyInputError) as exc:
        return DeploymentBudgetView(state="unavailable", detail=str(exc), strategy_instance_id=sid, world=world, committed_usd=dollars(row["committed_cents"]))


def budget_error(exc: BudgetUnavailable) -> PanelRunnerError:
    return PanelRunnerError("Deployment budget is unavailable.", detail=str(exc), next_action="Review the current budget and account evidence, then retry.", http_status=409, operation_attempted=False)
