"""The existing Deploy form's money review and durable command recovery.

This service composes the custody calculation; it owns no cash ledger, FIFO,
fee calculator or strategy-evidence gate. All account/world choices come from
installed authorities. Browser amounts are consent, never cash observations.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal

from pydantic import BaseModel, Field, ValidationError

from app.broker.alpaca.clerk.account_authority import canonical_alpaca_account_id, synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.account_money import (
    AccountMoney,
    BarParts,
    BarSegment,
    MoneyConservationError,
    bot_parts,
    money_bar,
)
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime, get_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.budgets import budget_entry_decision, entry_requirement
from app.broker.alpaca.clerk.live_envelope import AccountObservation
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
from app.schemas.exit_terms import ExitTerms
from app.services.bot_runner import UnknownBotError, get_bot_task_registry
from app.services.broker_v2_panel.deploy_submissions import DeploySubmissionLedger, bot_name_note
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
        raise BudgetUnavailable("This account's custody authority is unavailable. Activate it in Settings.")
    if runtime.sqlite_repository is None:
        raise BudgetUnavailable("Wait for this account's custody recovery to finish.")
    return runtime


def _request_world(runtime: ActiveClerkRuntime, request: AlpacaPaperDeployRequest) -> tuple[AuthorityKind, str | None]:
    """The world this Deploy trades in, and the real account whose money it uses.

    Dry Run uses none: its simulated cash lives in the bot's own ``sim:``
    account, which is named from the bot only when the Deploy is committed.
    """
    if request.execution_mode == "dry_run":
        return "synthetic", None
    world = runtime.account_authority_kind
    offered = {"real_paper": "paper", "real_live": "live", "shadow": "shadow"}
    if world not in offered or offered[world] != request.execution_mode:
        raise BudgetUnavailable("The account's execution world changed. Refresh Deploy and review the current world.")
    return world, runtime.selected_account_id


def request_fingerprint(request: AlpacaPaperDeployRequest, *, custody_account_id: str, world: AuthorityKind) -> str:
    """What consent binds to: the settings, the world and this lane's custody account.

    ``custody_account_id`` is the lane's own custody account in every world,
    Dry Run included -- the Dry Run's private account does not exist until
    the bot is named, after the preview this fingerprint is issued from.
    """
    return canonical_sha256({"account": custody_account_id, "world": world, "request": request.content()})


def preview_budget(account_id: str, request: AlpacaPaperDeployRequest, *, resolved_parameters: dict) -> DeploymentBudgetPreview:
    runtime = _primary(account_id)
    world, custody_id = _request_world(runtime, request)
    name_note = bot_name_note(request.symbol, request.strategy_key)
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
                world=world, custody_account_id=custody_id, bot_name_note=name_note,
            )
        requirement, _ = entry_requirement(quantity=request.sizing.quantity, price=quote.ask, at_ms=now)
        minimum = cents_required(requirement)
        amount = None if request.budget is None else consent_cents(request.budget.amount_usd)
        # Dry Run never draws on the account's money, so it has no bar.
        money: AccountMoney | None = None
        observation: AccountObservation | None = None
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
                    raise BudgetUnavailable("Switch this account to budgets in Settings before reviewing a deployment.")
                sync = runtime.envelope_sync
                if sync is None:
                    raise BudgetUnavailable("Wait for account cash and risk observations; Settings shows the daily loss limit.")
                risk = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
                if not risk.allowed:
                    raise BudgetUnavailable(risk.detail)
                snapshot = sync.risk_snapshot()
                observation = risk.observation
                if observation is None or snapshot.policy is None:
                    raise BudgetUnavailable("Set a daily loss limit in Settings and wait for fresh cash and risk evidence.")
                # One read under the fence: the bar's free to deploy IS this
                # preview's unreserved cash, by construction.
                money = _read_account_money(repo, observation)
                available = money.budget.unreserved_cents
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
        refusal = None
        if request.budget is not None and amount is not None:
            refusal = _amount_refusal(request.budget.risk_revision, amount, risk_revision=risk_revision, minimum=minimum, available=available)
        if request.budget is not None and amount is not None and refusal is None:
            registration = _STRATEGY_REGISTRY.get(request.strategy_key)
            contract = None if registration is None else registration.signal_program_contract
            token = canonical_sha256({
                "request": request_fingerprint(request, custody_account_id=runtime.selected_account_id, world=world),
                "resolved_parameters": resolved_parameters,
                "program_version": None if contract is None else contract.program_version,
            })
            if world == "real_live":
                confirmation = f"DEPLOY {account_id} ${dollars(amount)}"
        # A refused amount still returns the bar the Money step draws -- today's,
        # with no new slice -- beside the refusal; an admitted one is carved in.
        money_after = None if money is None or observation is None else _money_view(
            repo, observation, world=world, account_id=account_id, money=money,
            new_cents=amount if refusal is None else None,
        )
        return DeploymentBudgetPreview(
            state="ready" if refusal is None else "unavailable",
            detail=refusal or "This is an entry-admission budget. Market fills and losses can exceed it.",
            world=world, custody_account_id=custody_id, bot_name_note=name_note,
            budget_usd=None if token is None or amount is None else dollars(amount), observed_at_ms=observed_at,
            minimum_budget_usd=dollars(minimum), unreserved_usd=None if available is None else dollars(available),
            estimated_price_usd=display_dollars(normalize_money(quote.ask)), risk_revision=risk_revision,
            risk_limits_summary=risk_summary,
            shortcuts=tuple(shortcuts), review_token=token, confirmation_text=confirmation,
            money_after=money_after,
        )
    except (BudgetUnavailable, MoneyInputError, RateNotPinnedError) as exc:
        return DeploymentBudgetPreview(
            state="unavailable", detail=str(exc), world=world, custody_account_id=custody_id, bot_name_note=name_note,
        )


def _amount_refusal(reviewed_revision: int, amount: int, *, risk_revision: int, minimum: int, available: int | None) -> str | None:
    """Why this amount cannot be reviewed, or ``None``; the bar is drawn either way."""
    if reviewed_revision != risk_revision:
        return "Risk limits changed. Review the current limits and budget again."
    if amount < minimum:
        return f"One estimated position needs at least ${dollars(minimum)}, including fees."
    if available is not None and amount > available:
        return f"Only ${dollars(available)} is unreserved. Choose a smaller budget or resolve existing claims."
    return None


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
        request_fingerprint=request_fingerprint(
            request, custody_account_id=_primary(account_id).selected_account_id, world=preview.world,
        ),
        world=preview.world,
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


async def sealed_exit_terms(account_id: str, sid: str) -> ExitTerms | None:
    """The exit terms ``sid`` was deployed with, from the authority that custodies it."""
    async with _deployment_runtime(account_id, sid) as runtime:
        repo = runtime.sqlite_repository
        assert repo is not None
        return repo.exit_terms(sid)


async def command_receipt(account_id: str, sid: str) -> BudgetDeployCommandReceipt | None:
    """The recorded outcome of ``sid``'s Deploy, or ``None`` when none was committed."""
    async with _deployment_runtime(account_id, sid) as runtime:
        return _command_receipt(runtime, account_id, sid)


async def submission_receipt(account_id: str, submission_key: str) -> BudgetDeployCommandReceipt | None:
    """The recovery read: what one Deploy submission recorded, by its key.

    ``None`` when the key named no bot, or named one whose Deploy was never
    committed -- nothing was set aside and nothing started.
    """
    registry = get_bot_task_registry()
    submission = None if registry is None else DeploySubmissionLedger(registry.artifacts_root).by_key(submission_key)
    return None if submission is None else await command_receipt(account_id, submission.strategy_instance_id)


#: The receipt's words per outcome. Honest on a first read and on every
#: recovery read alike, so no copy claims a replay (H12).
_RECEIPT_COPY: dict[str, tuple[str, str]] = {
    "deployed": ("{sid} is deployed", "{money} is set aside for it."),
    "pending": ("{sid} is committed; its launch is not confirmed yet",
                "{money} is set aside for it. Check again in a moment; checking never starts a second bot."),
    "failed": ("{sid} did not launch", "Nothing is running. Its budget is released."),
}


def _command_receipt(runtime: ActiveClerkRuntime, account_id: str, sid: str) -> BudgetDeployCommandReceipt | None:
    repo = runtime.sqlite_repository
    assert repo is not None
    with repo.write_fence():
        row = repo.deployment_budget(sid)
        if row is None:
            return None
        command = repo.get_command(row["command_id"])
        if command is None:
            raise BudgetUnavailable("Deployment command evidence is unavailable. Resolve custody recovery before continuing.")
    state = "failed" if command.state == "failed" else ("deployed" if row["launched_at_ms"] is not None else "pending")
    committed = dollars(row["committed_cents"])
    money = f"${committed} of simulated cash" if row["world"] == "synthetic" else f"${committed}"
    message, explanation = _RECEIPT_COPY[state]
    registry = get_bot_task_registry()
    submission = None if registry is None else DeploySubmissionLedger(registry.artifacts_root).by_name(sid)
    return BudgetDeployCommandReceipt(
        status=state, outcome={"failed": "failure", "deployed": "success", "pending": "pending"}[state],
        receipt_id=command.command_id, command_id=command.command_id, recorded_at_ms=command.updated_at_ms,
        strategy_instance_id=sid, run_id=row["run_id"], account_id=account_id, world=row["world"],
        committed_usd=committed,
        message=message.format(sid=sid), explanation=explanation.format(money=money),
        next_action="Open the bot's page to watch it trade." if state != "failed" else "Deploy again when the cause is fixed.",
        first_deployed_at_ms=None if submission is None else submission.first_deployed_at_ms,
        replaces_strategy_instance_id=None if submission is None else submission.replaces_strategy_instance_id,
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
        now = repo.clock()
        risk = current_risk_readiness(repo, envelope=sync.envelope, now_ms=now)
        # Only a current admission judges the next entry; the money itself is
        # shown from the last reading of the account, as the money bar is.
        observation = risk.observation or sync.display_observation(now)
        if observation is None:
            raise BudgetUnavailable(risk.detail)
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


_NO_FRESH_READING = (
    "A fresh reading of this account's cash is not available yet. It refreshes every few seconds; "
    "if it stays missing, check the account's connection in Settings."
)
_OPEN_PNL_UNPRICED = "Open P&L needs a current price for every holding, and this account's prices are not part of this read."


def _read_account_money(repo: ClerkSqliteRepository, observation: AccountObservation) -> AccountMoney:
    return repo.account_money(
        cash=observation.cash_available_usd, seen_before_ms=observation.fills_seen_before_ms,
        modelled_fees_seen_before_ms=observation.modelled_fees_seen_before_ms,
    )


async def account_money_view(account_id: str) -> AccountMoneyView:
    """Where this account's money is, from its own clerk (``account_money_read``).

    The custody fence and the projection are blocking work, so they run off
    the event loop, as ``read_account_custody`` does.
    """
    return await asyncio.to_thread(_account_money_view, account_id)


def _account_money_view(account_id: str) -> AccountMoneyView:
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
        # The sync's last reading, for display only: a loss hold or an
        # unjudgeable day withdraws admission, never the account's money.
        sync = runtime.envelope_sync
        observation = None if sync is None else sync.display_observation(repo.clock())
        if observation is None:
            return AccountMoneyView(state="unavailable", detail=_NO_FRESH_READING, world=world, account_id=account_id)
        return _money_view(repo, observation, world=world, account_id=account_id)


def _money_view(
    repo: ClerkSqliteRepository, observation: AccountObservation, *, world: AuthorityKind, account_id: str,
    money: AccountMoney | None = None, new_cents: int | None = None,
) -> AccountMoneyView:
    """Draw the account's bar in Python-authored dollars, or say why it cannot be.

    The caller holds the custody fence, or passes the ``money`` it read under
    it. ``new_cents`` is an admitted Deploy, carved out of free to deploy as a
    ``new`` slice. The broker's own equity and day P&L ride along whenever
    they are known, whether or not the bar can be drawn.
    """
    broker = _broker_figures(repo, observation)
    try:
        money = money if money is not None else _read_account_money(repo, observation)
        bar = money_bar(money, new_cents=new_cents)
    except (BudgetUnavailable, MoneyInputError, RateNotPinnedError) as exc:
        return AccountMoneyView(state="unavailable", detail=str(exc), world=world, account_id=account_id, **broker)
    except MoneyConservationError:
        # A projection bug, never a display: said loudly here and to the owner,
        # without taking the Deploy preview that embeds this bar down with it.
        logger.exception("Account money does not add up", extra={"action": "account_money_unconserved", "account_id": account_id})
        return AccountMoneyView(
            state="unavailable", world=world, account_id=account_id, **broker,
            detail="This account's money does not add up, so the bar is withheld. The fault is logged for repair.",
        )
    open_pnl, open_pnl_detail = _open_pnl(money, observation)
    detail = "Cash plus shares at the price paid."
    if money.unvalued:
        detail += " Not on the bar: " + "; ".join(money.unvalued) + "."
    return AccountMoneyView(
        state="ready", detail=detail, world=world, account_id=account_id, **broker,
        total_usd=dollars(bar.total_cents), cash_usd=dollars(bar.cash_cents),
        free_to_deploy_usd=dollars(bar.cents_of("free")), in_bots_usd=dollars(bar.cents_of("bot")),
        held_by_stopped_usd=dollars(bar.cents_of("stopped")), outside_bots_usd=dollars(bar.cents_of("outside")),
        account_charges_usd=dollars(bar.cents_of("charges")), settling_usd=dollars(bar.cents_of("settling")),
        account_shortfall_usd=_shortfall_usd(bar.shortfall_cents), stopped_holding_count=bar.count_of("stopped"),
        open_pnl_usd=open_pnl, open_pnl_detail=open_pnl_detail,
        segments=tuple(_segment_view(segment) for segment in bar.segments),
    )


def _broker_figures(repo: ClerkSqliteRepository, observation: AccountObservation) -> dict[str, object]:
    """Alpaca's equity and today's account P&L, when the reading knows them."""
    with money_context():
        equity = _known_usd(observation.equity_usd)
        today = None
        if equity is not None and _known_usd(observation.last_equity_usd) is not None:
            day = observed_day_pnl(observation=observation, now_ms=repo.clock())
            today = _known_usd(day.total_usd) if day.known else None
    if equity is None and today is None:
        return {}
    return {
        "observed_at_ms": observation.observed_at_ms,
        "equity_usd": None if equity is None else dollars(display_cents(equity)),
        "today_pnl_usd": None if today is None else dollars(display_cents(today)),
    }


def _known_usd(value: float | None) -> Decimal | None:
    """A broker figure, or ``None`` when it is absent or not a finite number."""
    return None if value is None or not math.isfinite(value) else normalize_money(value)


def _open_pnl(money: AccountMoney, observation: AccountObservation) -> tuple[str | None, str | None]:
    """Canonical FIFO open P&L over every held lot, or why there is none.

    Flat, it is zero (FIFO values no lot). Simulated custody values its own
    lots with its own marks (``SimulatedAccountProjection.observe`` ->
    ``fifo.open_pnl``) on the observation. Real custody has no mark in this
    read, so the figure is withheld with its reason -- never equity less cost.
    """
    if not money.holds_positions:
        return "0.00", None
    if observation.simulation_session_start_ms is not None:
        with money_context():
            return dollars(display_cents(normalize_money(observation.unrealized_pl_usd))), None
    return None, _OPEN_PNL_UNPRICED


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
        case "settling", None:
            return "settling into cash"
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
        shortfall_usd=_shortfall_usd(segment.shortfall_cents),
        released_usd=None if segment.released_cents is None else dollars(segment.released_cents),
        still_claimed_usd=None if segment.still_claimed_cents is None else dollars(segment.still_claimed_cents),
        palette_index=segment.palette_index,
    )


def _shortfall_usd(cents: int | None) -> str | None:
    """A shortfall in dollars, or ``None`` when nothing is short.

    Absent rather than "0.00", so a legend states a shortfall only where there
    is one -- the browser never compares dollar strings to hide a zero.
    """
    return None if cents is None or cents == 0 else dollars(cents)


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
