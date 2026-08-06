"""Panel data-source facade (spec §3, §5, §7, §8, §11).

Resolves the live dependencies the account-scoped panel endpoints need — the
account id, the order journal, the decision journals, the bot roster, the clerk
status, and the live chart aggregator — and delegates every computation to the
pure projection functions. The router stays transport-only; this facade is the
single seam that touches process singletons.

Account scope (§3): every method validates ``account_id`` against the broker's
real account and raises :class:`AccountMismatchError` (→ 404) on a mismatch, so
a stale deep link never reads another account's evidence.
"""

from __future__ import annotations

import logging
from typing import Literal

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.clerk import InventoryBaselineRefusedError
from app.broker.alpaca.clerk.decision_journal import DecisionJournal, DecisionReceipt
from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.models import (
    ClerkStatus,
    EffectOperationState,
    EffectPurpose,
    OrderJournalEntry,
)
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerAccountSnapshot
from app.config import settings
from app.data_lake.polygon_fetcher import fetch_aggregate_bars
from app.schemas.broker_bots import (
    AlpacaPaperDeployReceipt,
    AlpacaPaperDeployRequest,
    AlpacaPaperDeployView,
    BotControlAuthorityFacts,
    BotProcessFact,
    BotStatusView,
    DeployBotRequest,
)
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    ChartHistoryPreset,
    ChartHistoryResponse,
    ChartLiveResponse,
    PanelActionRequest,
    PanelActionResult,
)
from app.schemas.run_admission import RunAdmissionDecision
from app.services.bot_runner import BotRunnerError, get_bot_task_registry
from app.services.broker_account_snapshot import resolve_broker_account_snapshot
from app.services.broker_v2_panel.account_projection_owner import get_or_create_owner
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    ActionPerformer,
    durable_idempotency_store_for,
    execute_action,
)
from app.services.broker_v2_panel.chart_projection_service import (
    build_history_chart,
    build_live_chart,
    live_window,
)
from app.services.broker_v2_panel.market_pulse import build_market_pulse
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for
from app.services.broker_v2_panel.panel_projection_service import (
    build_clerk_card,
    build_panel,
    channel_health_fresh,
    compute_revision,
)
from app.services.broker_v2_panel.paper_deploy_service import (
    build_alpaca_paper_deploy_receipt,
    build_alpaca_paper_deploy_view,
)
from app.services.broker_v2_panel.presented_actions import build_roster_action
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    adapt_sqlite_catalog,
    adapt_sqlite_panel,
)
from app.services.broker_v2_panel.sqlite_panel_source import (
    SqlitePanelBotNotFound,
    execute_sqlite_panel_action,
    read_sqlite_bot_projection,
    read_sqlite_catalog_projections,
    read_sqlite_clerk_status,
    sqlite_authority_active,
)
from app.services.live_chart_window import (
    ChartWindowError,
    ChartWindowResult,
    coerce_chart_timeframe,
    resolve_chart_window,
)
from app.services.strategy_validation_manifest import (
    StrategyValidationManifestError,
    load_strategy_validation_entries,
    strategy_registry_seeds,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class PanelDataError(Exception):
    """Base typed panel-data error; the router translates to HTTP."""

    http_status: int = 500

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.detail = detail
        self.next_action = next_action


class PanelUnavailableError(PanelDataError):
    """A required backend (clerk / bot runner) is not configured (503)."""

    http_status = 503


class AccountMismatchError(PanelDataError):
    """The path ``account_id`` does not match the broker's account (404)."""

    http_status = 404


class UnknownBotError(PanelDataError):
    """No bot with this sid is bound to the broker (404)."""

    http_status = 404


class PanelRunnerError(PanelDataError):
    """The bot runner rejected a panel operation with a typed status."""

    def __init__(
        self,
        message: str,
        *,
        detail: str | None,
        http_status: int,
        next_action: str | None = None,
        operation_attempted: bool = False,
        admission_decision: RunAdmissionDecision | None = None,
    ) -> None:
        super().__init__(message, detail=detail, next_action=next_action)
        self.http_status = http_status
        self.operation_attempted = operation_attempted
        self.admission_decision = admission_decision


# Only Alpaca has a panel-backing clerk in phase 1.
_PANEL_BROKER = "alpaca"


async def resolve_account_snapshot(broker: str) -> BrokerAccountSnapshot:
    """Return the cached broker-authored account posture."""
    _require_panel_broker(broker)
    try:
        return await resolve_broker_account_snapshot(broker)
    except BrokerError as exc:
        raise PanelUnavailableError("The broker account could not be read.", detail=exc.detail) from exc


async def resolve_account_id(broker: str) -> str:
    """Return the broker's real account id (the source the clerk uses)."""
    return (await resolve_account_snapshot(broker)).account_id


def _require_panel_broker(broker: str) -> None:
    if broker != _PANEL_BROKER:
        raise UnknownBotError(
            f"Broker '{broker}' has no bot control panel.",
            detail="Only Alpaca exposes the broker-v2 panel in phase 1.",
        )


async def validate_account_scope(broker: str, account_id: str, sid: str) -> None:
    """Validate broker + account_id + sid for operator-gated endpoints (§3, §14).

    Raises ``AccountMismatchError`` (→ 404) when the path ``account_id``
    does not match the broker's real account, and ``UnknownBotError`` (→ 404)
    when the bot has no durable binding to the broker.
    """
    await _validate_account(broker, account_id)
    _bot_status(broker, sid)


def _read_order_journal(broker: str, account_id: str) -> list[OrderJournalEntry]:
    if sqlite_authority_active(broker):
        return []
    journal = OrderJournal(account_id=account_id, root=get_clerk_settings().dir)
    owner = get_or_create_owner(account_id, broker)
    return owner.refresh_journal(journal)


def _latest_decision(account_id: str, sid: str) -> DecisionReceipt | None:
    journal = DecisionJournal(account_id=account_id, sid=sid, root=get_clerk_settings().dir)
    tail = journal.tail(1)
    return tail[-1] if tail else None


def _recent_decisions(account_id: str, sid: str, limit: int = 8) -> list[DecisionReceipt]:
    journal = DecisionJournal(account_id=account_id, sid=sid, root=get_clerk_settings().dir)
    return journal.tail(limit)


def _bot_statuses(broker: str) -> list[BotStatusView]:
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        return registry.list_bots(broker)
    except BotRunnerError as exc:
        raise PanelUnavailableError(str(exc), detail=exc.detail) from exc


def _bot_status(broker: str, sid: str) -> BotStatusView:
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        return registry.status(broker, sid)
    except BotRunnerError as exc:
        if exc.http_status == 404:
            raise UnknownBotError(str(exc), detail=exc.detail) from exc
        raise PanelUnavailableError(str(exc), detail=exc.detail) from exc


def _bot_process_fact(broker: str, sid: str) -> BotProcessFact:
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        return registry.process_fact(broker, sid)
    except BotRunnerError as exc:
        if exc.http_status == 404:
            raise UnknownBotError(str(exc), detail=exc.detail) from exc
        raise PanelUnavailableError(str(exc), detail=exc.detail) from exc


async def _clerk_status() -> ClerkStatus:
    try:
        sqlite_status = await read_sqlite_clerk_status()
    except (RuntimeError, ValueError) as exc:
        raise PanelUnavailableError(str(exc)) from exc
    if sqlite_status is not None:
        return sqlite_status
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise PanelUnavailableError(
            "Alpaca order management is not configured.",
            detail="Set Alpaca paper credentials in .env and restart the service.",
        )
    try:
        return await clerk.status()
    except BrokerError as exc:
        raise PanelUnavailableError("The clerk status could not be read.", detail=exc.detail) from exc


async def _validate_account(broker: str, account_id: str) -> str:
    real_account_id = await resolve_account_id(broker)
    if account_id != real_account_id:
        raise AccountMismatchError(
            f"Account '{account_id}' is not the account for broker '{broker}'.",
            detail=f"The broker's account is '{real_account_id}'.",
        )
    return real_account_id


async def get_authority_facts(
    broker: str,
    account_id: str,
    sid: str,
) -> BotControlAuthorityFacts:
    """Compose owner-authored facts without deriving a control decision."""
    resolved_account_id = await _validate_account(broker, account_id)
    process = _bot_process_fact(broker, sid)
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise PanelUnavailableError(
            "Alpaca order management is not configured.",
            detail="The Clerk cannot author current custody facts.",
        )
    custody = await clerk.custody_snapshot(sid)
    if custody.account_id != resolved_account_id:
        raise PanelUnavailableError(
            "The Clerk custody account does not match the panel account.",
            detail="Recover the account-scoped Clerk before using control actions.",
        )
    return BotControlAuthorityFacts(process=process, clerk=custody)


async def deploy_bot(
    broker: str,
    account_id: str,
    request: DeployBotRequest,
) -> BotStatusView:
    """Validate account scope and deploy through the panel's runner facade."""
    await _validate_account(broker, account_id)
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    try:
        return await registry.deploy(
            broker=broker,
            strategy_instance_id=request.strategy_instance_id,
            symbol=request.symbol,
            use_rth=request.use_rth,
            mode=request.mode,
            quantity=request.quantity,
        )
    except BotRunnerError as exc:
        raise PanelRunnerError(
            str(exc),
            detail=exc.detail,
            http_status=exc.http_status,
        ) from exc


async def get_alpaca_paper_deploy_view(
    broker: str,
    account_id: str,
) -> AlpacaPaperDeployView:
    """Author the closed paper-deployment form and its current launch verdict."""
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
    clerk_status = await _clerk_status()
    try:
        validation_entries = load_strategy_validation_entries(strategy_registry_seeds())
    except StrategyValidationManifestError as exc:
        raise PanelUnavailableError(
            "The strategy validation catalog could not be verified.",
            detail="Deploy remains closed until current validation evidence is readable and hash-valid.",
            next_action="Restore the validation manifest and evidence artifacts, then refresh.",
        ) from exc
    return build_alpaca_paper_deploy_view(account, clerk_status, validation_entries)


async def deploy_alpaca_paper_bot(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> AlpacaPaperDeployReceipt:
    """Execute the production paper deployment command through the runner seam."""
    view = await get_alpaca_paper_deploy_view(broker, account_id)
    _require_alpaca_deploy_request(view, request)
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
    )


async def preview_alpaca_paper_start_admission(
    broker: str,
    account_id: str,
    request: AlpacaPaperDeployRequest,
) -> RunAdmissionDecision:
    """Project the same request-specific Start decision used by execution."""
    view = await get_alpaca_paper_deploy_view(broker, account_id)
    _require_alpaca_deploy_request(view, request)
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
) -> None:
    """Apply the shared form/configuration preflight before run admission."""
    if not view.eligibility.eligible:
        raise PanelRunnerError(
            view.eligibility.headline,
            detail=view.eligibility.explanation,
            next_action=view.eligibility.next_action,
            http_status=409,
        )
    if not any(strategy.strategy_key == request.strategy_key for strategy in view.strategies):
        raise PanelRunnerError(
            "The selected strategy is not currently accepted for Alpaca deployment.",
            detail="Its latest validation evidence is missing, superseded, invalidated, or not accepted for deploy.",
            next_action="Review the strategy in Strategy Validation, then refresh this page.",
            http_status=409,
        )
    if request.carryover_policy == "ALLOW" and not view.carryover_available:
        raise PanelRunnerError(
            "Exposure carryover is not enabled for this Alpaca paper account.",
            detail=view.carryover_explanation,
            next_action="Deploy with carryover disabled or enable the account policy first.",
            http_status=409,
        )


async def get_catalog(broker: str, account_id: str) -> list[BotCatalogView]:
    """Build the bots-list catalog for one account (§5)."""
    resolved = await _validate_account(broker, account_id)
    statuses = _bot_statuses(broker)
    entries = _read_order_journal(broker, resolved)
    sids = [status.strategy_instance_id for status in statuses]
    decisions = {sid: _latest_decision(resolved, sid) for sid in sids}
    owner = get_or_create_owner(resolved, broker)
    owner.sync(entries, sids, decisions=decisions)
    rows = owner.snapshot_catalog(statuses)
    sqlite_projections = await read_sqlite_catalog_projections(
        broker,
        resolved,
        sids,
    )
    if sqlite_projections is not None:
        return adapt_sqlite_catalog(rows, sqlite_projections)

    now_ms = now_ms_utc()
    try:
        clerk_status = await _clerk_status()
    except PanelUnavailableError as exc:
        logger.warning(
            "broker panel roster actions are failing closed because Clerk posture is unavailable",
            extra={"broker": broker, "account_id": account_id, "detail": exc.detail},
        )
        clerk_status = None
    clerk = (
        build_clerk_card(clerk_status, now_ms)
        if clerk_status is not None
        else None
    )
    profile = panel_profile_for(broker)
    flatten_supported = profile.flatten_supported if profile is not None else False
    clerk_channel_fresh = (
        channel_health_fresh(clerk_status, now_ms)
        if clerk_status is not None
        else False
    )
    status_by_sid = {status.strategy_instance_id: status for status in statuses}
    row_actions: list[BotCatalogView] = []
    for row in rows:
        status = status_by_sid[row.strategy_instance_id]
        decision = decisions[row.strategy_instance_id]
        revision = compute_revision(
            journal_len=len(entries),
            last_transition_at_ms=status.last_transition_at_ms,
            desired_state=status.desired_state,
            hold_active=clerk_status.hold.active if clerk_status is not None else False,
            last_decision_at_ms=decision.ts_ms if decision is not None else None,
        )
        action = build_roster_action(
            status,
            clerk,
            revision=revision,
            flatten_supported=flatten_supported,
            channel_fresh=clerk_channel_fresh,
            exposure=dict(row.exposure),
            account_id=resolved,
        )
        row_actions.append(row.model_copy(update={"row_action": action}))
    return row_actions


async def _get_panel_with_entries(
    broker: str,
    account_id: str,
    sid: str,
    *,
    transaction_ref: str | None = None,
) -> tuple[BotPanelView, list[OrderJournalEntry]]:
    """Build one panel and return the immutable journal cut it consumed."""
    resolved = await _validate_account(broker, account_id)
    status = _bot_status(broker, sid)
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
        )
    resume_admission: RunAdmissionDecision | None = None
    if not status.running:
        try:
            # Resume alone needs a fresh Clerk custody fence. Do not perform a
            # broker reconciliation every five seconds for a live run where
            # Resume is inapplicable.
            resume_admission = await registry.preview_resume_admission(broker, sid)
        except BotRunnerError as exc:
            resume_admission = exc.admission_decision
            if resume_admission is None:
                logger.warning(
                    "broker panel Resume admission is unavailable",
                    extra={"broker": broker, "account_id": account_id, "sid": sid, "detail": exc.detail},
                )
        # Preview can reconcile and advance Clerk evidence. Read all projection
        # inputs afterwards so this response is one post-admission evidence cut.
        status = _bot_status(broker, sid)
    try:
        projection = await read_sqlite_bot_projection(broker, resolved, sid)
    except SqlitePanelBotNotFound as exc:
        raise UnknownBotError(str(exc)) from exc
    entries = _read_order_journal(broker, resolved)
    clerk_status = await _clerk_status()
    decisions = _recent_decisions(resolved, sid)
    decision = decisions[-1] if decisions else None

    if projection is None:
        owner = get_or_create_owner(resolved, broker)
        owner.sync(entries, [sid], decisions={sid: decision})
        rollup = owner.get_rollup(sid)
        exposure = dict(rollup.exposure)
        fills_today = rollup.fills_today
        realized_pnl_today = rollup.realized_pnl_today
        open_pnl = rollup.open_pnl
        last_bar_at_ms = rollup.last_activity_at_ms
    else:
        exposure = {
            position.symbol: position.attributed_qty
            for position in projection.positions
            if abs(position.attributed_qty) > 0
        }
        fills_today = None
        realized_pnl_today = None
        open_pnl = None
        last_bar_at_ms = None

    profile = panel_profile_for(broker)
    flatten_supported = profile.flatten_supported if profile is not None else False
    now_ms = now_ms_utc()
    from app.marketdata.ibkr_feed import get_market_data_feed

    binding = registry.binding_for_control(broker, sid)
    panel = build_panel(
        status,
        clerk_status,
        entries,
        account_id=resolved,
        exposure=exposure,
        fills_today=fills_today,
        realized_pnl_today=realized_pnl_today,
        open_pnl=open_pnl,
        latest_decision=decision,
        last_bar_at_ms=last_bar_at_ms,
        journal_tail_ref=f"/api/brokers/{broker}/accounts/{resolved}/bots/{sid}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        flatten_supported=flatten_supported,
        now_ms=now_ms,
        selected_transaction_ref=transaction_ref,
        recent_decisions=decisions,
        resume_admission=resume_admission,
        dry_run_activity=registry.dry_run_activity(broker, sid),
        market_pulse=build_market_pulse(
            get_market_data_feed(),
            now_ms=now_ms,
            use_rth=binding.use_rth,
            bot_running=status.running,
        ),
    )
    if projection is not None:
        panel = adapt_sqlite_panel(panel, projection)
    return panel, entries


async def get_panel(
    broker: str,
    account_id: str,
    sid: str,
    *,
    transaction_ref: str | None = None,
) -> BotPanelView:
    """Build the current panel projection for one bot (§7)."""
    panel, _entries = await _get_panel_with_entries(
        broker,
        account_id,
        sid,
        transaction_ref=transaction_ref,
    )
    return panel


async def _build_live_chart_from_entries(
    sid: str,
    symbol: str,
    entries: list[OrderJournalEntry],
    *,
    resolution: Literal["5s", "1m"],
    now_ms: int,
) -> ChartLiveResponse:
    """Build a live chart from one already-captured journal cut."""
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    window = live_window(now_ms)
    open_ms, close_ms = window
    if now_ms <= open_ms:
        chart_window = ChartWindowResult(
            bars=[],
            timeframe=resolution,
            resolution=resolution,
            is_streaming=False,
        )
    else:
        try:
            if resolution == "5s":
                await LIVE_BAR_AGGREGATOR.ensure_subscribed_5s(symbol)
            else:
                await LIVE_BAR_AGGREGATOR.ensure_subscribed(symbol)
            chart_window = await resolve_chart_window(
                symbol=symbol,
                timeframe=coerce_chart_timeframe(resolution),
                from_ms=open_ms,
                # The resolver rejects a to_ms in the future; an open session's close
                # is later than now, so clamp the fetch bound. The response window
                # (below) keeps the true session close.
                to_ms=min(close_ms, now_ms),
                now_ms=now_ms,
                polygon_api_key=settings.POLYGON_API_KEY,
                live_aggregator=LIVE_BAR_AGGREGATOR,
                polygon_overlay_enabled=False,
            )
        except ChartWindowError as exc:
            raise PanelDataError("The live chart window is invalid.", detail=str(exc)) from exc

    return build_live_chart(
        chart_window,
        entries,
        strategy_instance_id=sid,
        symbol=symbol,
        window=window,
        now_ms=now_ms,
    )


async def get_live_chart(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"] = "1m",
) -> ChartLiveResponse:
    """Build the LIVE chart pane for one bot (§8)."""
    resolved = await _validate_account(broker, account_id)
    status = _bot_status(broker, sid)
    entries = _read_order_journal(broker, resolved)
    return await _build_live_chart_from_entries(
        sid,
        status.symbol,
        entries,
        resolution=resolution,
        now_ms=now_ms_utc(),
    )


async def get_live_snapshot_parts(
    broker: str,
    account_id: str,
    sid: str,
    *,
    resolution: Literal["5s", "1m"],
) -> tuple[BotPanelView, ChartLiveResponse]:
    """Build panel and chart from the same immutable account-journal cut."""
    panel, entries = await _get_panel_with_entries(broker, account_id, sid)
    chart = await _build_live_chart_from_entries(
        sid,
        panel.symbol,
        entries,
        resolution=resolution,
        now_ms=now_ms_utc(),
    )
    return panel, chart


async def get_history_chart(broker: str, account_id: str, sid: str, preset: ChartHistoryPreset) -> ChartHistoryResponse:
    """Build the bounded HISTORY chart pane for one bot (§8)."""
    resolved = await _validate_account(broker, account_id)
    status = _bot_status(broker, sid)
    entries = _read_order_journal(broker, resolved)

    async def _bar_source(symbol, start, end, multiplier, timespan):
        return await fetch_aggregate_bars(
            symbol,
            start,
            end,
            settings.POLYGON_API_KEY,
            multiplier=multiplier,
            timespan=timespan,
        )

    return await build_history_chart(
        preset,
        entries,
        strategy_instance_id=sid,
        symbol=status.symbol,
        bar_source=_bar_source,
        now_ms=now_ms_utc(),
    )


def _action_performers(broker: str, sid: str, *, idempotency_key: str) -> dict[str, ActionPerformer]:
    """Map each executable action id to the coroutine that performs it (§11, §12).

    Only actions with production custody are wired. The remaining closed-set
    actions raise ``ActionNotAvailableError`` from the executor rather than
    presenting a fake success.
    """

    async def _resume(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        try:
            admitted = await registry.resume_existing_with_admission(broker, sid)
        except BotRunnerError as exc:
            raise ActionNotAvailableError(
                "Resume is no longer available for this bot.",
                detail=exc.detail or str(exc),
            ) from exc
        return (
            f"Bot resumed as new run {admitted.bot.active_run_id} from its immutable configuration. "
            "The Clerk remains the only owner of broker order effects."
        )

    async def _stop(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        await registry.stop(broker, sid, reason=f"Panel stop by {operator}")
        return "Bot stopped. The Clerk cancelled any working entry orders; attributed exposure was left untouched."

    async def _pause(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        status = await registry.pause(
            broker,
            sid,
            updated_by=operator,
            reason=f"Panel pause by {operator}",
        )
        return f"Run {status.active_run_id} is paused; its process remains live."

    async def _continue(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        status = await registry.continue_paused(
            broker,
            sid,
            updated_by=operator,
            reason=f"Panel Continue by {operator}",
        )
        return f"Run {status.active_run_id} continued without changing run identity."

    async def _reconcile(operator: str, reason: str | None) -> str:
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        verdict = await clerk.reconcile_once()
        return f"Reconciliation sweep complete: {verdict}."

    async def _flatten_stop(operator: str, reason: str | None) -> str:
        registry = get_bot_task_registry()
        clerk = get_alpaca_clerk()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        binding = registry.binding_for_control(broker, sid)
        status = registry.status(broker, sid)
        if status.running:
            # STOP-AND-FLATTEN is ordered custody: first persist STOPPED and
            # cancel strategy evaluation/working entries, then derive the
            # reducing EXIT. A failed flatten must never leave the strategy
            # running or able to submit a fresh entry.
            await registry.stop(
                broker,
                sid,
                reason=f"Panel flatten-and-stop by {operator}",
            )
        receipt = await clerk.execute_for_instance(
            strategy_instance_id=sid,
            run_id=binding.run_id,
            decision_id=f"panel-flatten:{idempotency_key}",
            purpose=EffectPurpose.EXIT,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
        )
        if receipt.state is EffectOperationState.UNPROVABLE:
            return (
                "The bot is stopped, but the Clerk cannot prove that attributed exposure "
                "is flat. Inspect the Clerk receipt before issuing another action."
            )
        if receipt.state is EffectOperationState.FLAT:
            return "The Clerk proved attributed exposure is flat and the bot is stopped."
        return (
            "The bot is stopped and the Clerk submitted the reducing operation; "
            "await its durable fill receipt before treating exposure as flat."
        )

    async def _clear_hold(operator: str, reason: str | None) -> str:
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        try:
            await clerk.clear_hold(operator=operator, reason=reason or "Panel clear-hold")
        except InventoryBaselineRefusedError as exc:
            raise ActionNotAvailableError(str(exc), detail=exc.detail) from exc
        return "Exposure hold cleared."

    async def _record_inventory_baseline(operator: str, reason: str | None) -> str:
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        try:
            baseline = await clerk.record_inventory_baseline(
                operator=operator,
                reason=reason or "Operator confirmed current Alpaca inventory from the bot panel.",
                strategy_instance_id=sid,
            )
        except InventoryBaselineRefusedError as exc:
            raise ActionNotAvailableError(str(exc), detail=exc.detail) from exc
        except BrokerError as exc:
            raise ActionNotAvailableError(
                "Alpaca inventory could not be read for recovery.",
                detail=exc.detail or str(exc),
            ) from exc
        positions = ", ".join(
            f"{position.symbol} {position.signed_quantity:g}"
            for position in baseline.positions
        )
        return (
            "Inventory baseline recorded and reconciliation is clean. "
            f"Broker positions at the cutover: {positions or 'flat'}."
        )

    return {
        "resume": _resume,
        "pause": _pause,
        "continue": _continue,
        "stop": _stop,
        "flatten_stop": _flatten_stop,
        "reconcile_now": _reconcile,
        "clear_hold": _clear_hold,
        "record_inventory_baseline": _record_inventory_baseline,
    }


async def run_action(
    broker: str,
    account_id: str,
    sid: str,
    request: PanelActionRequest,
    *,
    operator_identity: str,
) -> PanelActionResult:
    """Execute one presented action for a bot (§11).

    Recomputes the current panel revision (the guard the POST is checked
    against), then delegates to the execution service. Identity is the
    configured ``operator_identity`` — never a request field.
    """
    panel = await get_panel(broker, account_id, sid)
    action = next(
        (candidate for candidate in panel.actions if candidate.action_id == request.action_id),
        None,
    )
    if action is None:
        raise UnknownBotError(
            f"Action '{request.action_id}' is not available for bot '{sid}'.",
            detail="Refresh the panel before retrying the command.",
        )
    availability_error: ActionNotAvailableError | None = None
    if not action.enabled:
        blocker = action.blockers[0] if action.blockers else None
        availability_error = ActionNotAvailableError(
            f"The '{action.label}' action is blocked by the current panel state.",
            detail=(
                blocker.detail
                if blocker is not None
                else "Refresh the panel and inspect the operation's readiness check."
            ),
        )
    try:
        sqlite_result = await execute_sqlite_panel_action(
            broker,
            account_id,
            sid,
            request=request,
            panel=panel,
            action=action,
            availability_error=availability_error,
        )
    except SqlitePanelBotNotFound as exc:
        raise UnknownBotError(str(exc)) from exc
    if sqlite_result is not None:
        return sqlite_result
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError("The bot runner is not available.")
    return await execute_action(
        request,
        sid=sid,
        current_revision=panel.revision,
        current_concurrency_token=action.concurrency_token,
        performers=_action_performers(broker, sid, idempotency_key=request.idempotency_key),
        operator_identity=operator_identity,
        store=durable_idempotency_store_for(registry.panel_action_receipt_path(sid)),
        availability_error=availability_error,
    )
