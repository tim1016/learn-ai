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

from app.broker.alpaca.clerk import get_alpaca_clerk
from app.broker.alpaca.clerk.decision_journal import DecisionJournal, DecisionReceipt
from app.broker.alpaca.clerk.journal import OrderJournal, get_clerk_settings
from app.broker.alpaca.clerk.models import ClerkStatus, OrderJournalEntry
from app.broker.alpaca.clerk.rollup_cache import BotRollupCache
from app.broker.contract.errors import BrokerError
from app.broker.contract.registry import get_broker_registry
from app.config import settings
from app.data_lake.polygon_fetcher import fetch_aggregate_bars
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotCatalogView,
    BotPanelView,
    ChartHistoryPreset,
    ChartHistoryResponse,
    ChartLiveResponse,
    PanelActionRequest,
    PanelActionResult,
)
from app.services.bot_runner import BotRunnerError, get_bot_task_registry
from app.services.broker_v2_panel.action_execution_service import (
    ActionPerformer,
    execute_action,
)
from app.services.broker_v2_panel.catalog_projection_service import (
    bootstrap_rollup_cache,
    build_catalog,
)
from app.services.broker_v2_panel.chart_projection_service import (
    build_history_chart,
    build_live_chart,
    live_window,
)
from app.services.broker_v2_panel.panel_profile_service import panel_profile_for
from app.services.broker_v2_panel.panel_projection_service import build_panel
from app.services.live_chart_window import (
    ChartWindowError,
    coerce_chart_timeframe,
    resolve_chart_window,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


class PanelDataError(Exception):
    """Base typed panel-data error; the router translates to HTTP."""

    http_status: int = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class PanelUnavailableError(PanelDataError):
    """A required backend (clerk / bot runner) is not configured (503)."""

    http_status = 503


class AccountMismatchError(PanelDataError):
    """The path ``account_id`` does not match the broker's account (404)."""

    http_status = 404


class UnknownBotError(PanelDataError):
    """No bot with this sid is bound to the broker (404)."""

    http_status = 404


# Only Alpaca has a panel-backing clerk in phase 1.
_PANEL_BROKER = "alpaca"


async def resolve_account_id(broker: str) -> str:
    """Return the broker's real account id (the source the clerk uses).

    The unscoped alias routes call this to resolve the single account before
    delegating to the validated scoped path (§3).
    """
    _require_panel_broker(broker)
    try:
        port = get_broker_registry().resolve(broker)
        account = await port.get_account()
    except BrokerError as exc:
        raise PanelUnavailableError(
            "The broker account could not be read.", detail=exc.detail
        ) from exc
    return account.account_id


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
    real_account_id = await resolve_account_id(broker)
    if account_id != real_account_id:
        raise AccountMismatchError(
            f"Account '{account_id}' does not match broker '{broker}'.",
            detail="Stale deep link — the account id in the URL does not match.",
        )
    _bot_status(broker, sid)


def _read_order_journal(account_id: str) -> list[OrderJournalEntry]:
    journal = OrderJournal(account_id=account_id, root=get_clerk_settings().dir)
    return journal.read_entries()


def _latest_decision(account_id: str, sid: str) -> DecisionReceipt | None:
    journal = DecisionJournal(
        account_id=account_id, sid=sid, root=get_clerk_settings().dir
    )
    tail = journal.tail(1)
    return tail[-1] if tail else None


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


async def _clerk_status() -> ClerkStatus:
    clerk = get_alpaca_clerk()
    if clerk is None:
        raise PanelUnavailableError(
            "Alpaca order management is not configured.",
            detail="Set Alpaca paper credentials in .env and restart the service.",
        )
    try:
        return await clerk.status()
    except BrokerError as exc:
        raise PanelUnavailableError(
            "The clerk status could not be read.", detail=exc.detail
        ) from exc


async def _validate_account(broker: str, account_id: str) -> str:
    real_account_id = await resolve_account_id(broker)
    if account_id != real_account_id:
        raise AccountMismatchError(
            f"Account '{account_id}' is not the account for broker '{broker}'.",
            detail=f"The broker's account is '{real_account_id}'.",
        )
    return real_account_id


async def get_catalog(broker: str, account_id: str) -> list[BotCatalogView]:
    """Build the bots-list catalog for one account (§5)."""
    resolved = await _validate_account(broker, account_id)
    statuses = _bot_statuses(broker)
    entries = _read_order_journal(resolved)
    cache = BotRollupCache()
    sids = [status.strategy_instance_id for status in statuses]
    bootstrap_rollup_cache(cache, sids, entries)
    # Fold decision receipts into the cache so needs_attention reflects the
    # latest 'blocked' decision (spec §5 attention-first sort).
    for sid in sids:
        decision = _latest_decision(resolved, sid)
        if decision is not None:
            cache.on_decision_appended(decision, sid=sid)
    return build_catalog(statuses, cache, account_id=resolved)


async def get_panel(
    broker: str, account_id: str, sid: str, *, transaction_ref: str | None = None
) -> BotPanelView:
    """Build the 5s-poll panel projection for one bot (§7)."""
    resolved = await _validate_account(broker, account_id)
    status = _bot_status(broker, sid)
    entries = _read_order_journal(resolved)
    clerk_status = await _clerk_status()
    decision = _latest_decision(resolved, sid)

    cache = BotRollupCache()
    bootstrap_rollup_cache(cache, [sid], entries)
    rollup = cache.get_rollup(sid)

    profile = panel_profile_for(broker)
    flatten_supported = profile.flatten_supported if profile is not None else False
    now_ms = now_ms_utc()
    return build_panel(
        status,
        clerk_status,
        entries,
        account_id=resolved,
        exposure=dict(rollup.exposure),
        latest_decision=decision,
        last_bar_at_ms=rollup.last_activity_at_ms,
        journal_tail_ref=f"/api/brokers/{broker}/accounts/{resolved}/bots/{sid}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        flatten_supported=flatten_supported,
        now_ms=now_ms,
        selected_transaction_ref=transaction_ref,
    )


async def get_live_chart(broker: str, account_id: str, sid: str) -> ChartLiveResponse:
    """Build the LIVE chart pane for one bot (§8).

    Reuses the existing ``live_chart_window`` resolver — today's session window
    from the canonical NY calendar, capped at the resolver's untouched 7-day
    limit — then decorates with this bot's fill markers.
    """
    from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

    resolved = await _validate_account(broker, account_id)
    status = _bot_status(broker, sid)
    symbol = status.symbol
    entries = _read_order_journal(resolved)
    now_ms = now_ms_utc()

    window = live_window(now_ms)
    open_ms, close_ms = window
    try:
        chart_window = await resolve_chart_window(
            symbol=symbol,
            timeframe=coerce_chart_timeframe("1m"),
            from_ms=open_ms,
            # The resolver rejects a to_ms in the future; an open session's close
            # is later than now, so clamp the fetch bound. The response window
            # (below) keeps the true session close.
            to_ms=min(close_ms, now_ms),
            now_ms=now_ms,
            polygon_api_key=settings.POLYGON_API_KEY,
            live_aggregator=LIVE_BAR_AGGREGATOR,
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


async def get_history_chart(
    broker: str, account_id: str, sid: str, preset: ChartHistoryPreset
) -> ChartHistoryResponse:
    """Build the bounded HISTORY chart pane for one bot (§8)."""
    resolved = await _validate_account(broker, account_id)
    status = _bot_status(broker, sid)
    entries = _read_order_journal(resolved)

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


def _action_performers(broker: str, sid: str) -> dict[str, ActionPerformer]:
    """Map each executable action id to the coroutine that performs it (§11, §12).

    Only the actions whose backend exists in phase 1 are wired: ``stop``
    (bot runner), ``reconcile_now`` and ``clear_hold`` (clerk). The remaining
    closed-set actions raise ``ActionNotAvailableError`` from the executor
    (their lifecycle backend lands in later slices) rather than presenting a
    fake success.
    """

    async def _stop(operator: str) -> str:
        registry = get_bot_task_registry()
        if registry is None:
            raise PanelUnavailableError("The bot runner is not available.")
        await registry.stop(broker, sid, reason=f"Panel stop by {operator}")
        return "Bot stopped; working entry orders cancelled. Exposure left untouched."

    async def _reconcile(operator: str) -> str:
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        verdict = await clerk.reconcile_once()
        return f"Reconciliation sweep complete: {verdict}."

    async def _clear_hold(operator: str) -> str:
        clerk = get_alpaca_clerk()
        if clerk is None:
            raise PanelUnavailableError("Alpaca order management is not configured.")
        await clerk.clear_hold(operator=operator, reason="Panel clear-hold")
        return "Exposure hold cleared."

    return {
        "stop": _stop,
        "reconcile_now": _reconcile,
        "clear_hold": _clear_hold,
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
    return await execute_action(
        request,
        sid=sid,
        current_revision=panel.revision,
        performers=_action_performers(broker, sid),
        operator_identity=operator_identity,
    )
