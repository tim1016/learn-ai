"""Resolving the panel's live dependencies, and validating account scope.

Every account-scoped panel endpoint starts here: it resolves the broker's
real account, the bot runner's view of a sid, and the activated Clerk, and
translates each subsystem's typed failure into a :mod:`panel_errors` one.

Account scope (spec §3): ``account_id`` is validated against the broker's
real account and :class:`AccountMismatchError` (-> 404) is raised on a
mismatch, so a stale deep link never reads another account's evidence.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.contract.errors import BrokerError
from app.broker.contract.models import BrokerAccountSnapshot
from app.schemas.broker_bots import BotProcessFact, BotStatusView
from app.services.bot_runner import BotRunnerError, get_bot_task_registry
from app.services.broker_account_snapshot import resolve_broker_account_snapshot
from app.services.broker_v2_panel.panel_errors import (
    AccountMismatchError,
    PanelUnavailableError,
    UnknownBotError,
)
from app.services.broker_v2_panel.sqlite_panel_source import read_sqlite_clerk_status

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
    await validate_account(broker, account_id)
    bot_status(broker, sid)


def bot_status(broker: str, sid: str) -> BotStatusView:
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


def bot_process_fact(broker: str, sid: str) -> BotProcessFact:
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


async def clerk_status(*, symbol: str | None = None) -> ClerkStatus:
    try:
        sqlite_status = await read_sqlite_clerk_status(symbol=symbol)
    except (RuntimeError, ValueError) as exc:
        raise PanelUnavailableError(str(exc)) from exc
    if sqlite_status is None:
        raise PanelUnavailableError(
            "The activated SQLite Clerk is unavailable.",
            detail="Restore or reactivate the account-scoped SQLite authority.",
        )
    return sqlite_status


async def validate_account(broker: str, account_id: str) -> str:
    real_account_id = await resolve_account_id(broker)
    if account_id != real_account_id:
        raise AccountMismatchError(
            f"Account '{account_id}' is not the account for broker '{broker}'.",
            detail=f"The broker's account is '{real_account_id}'.",
        )
    return real_account_id
