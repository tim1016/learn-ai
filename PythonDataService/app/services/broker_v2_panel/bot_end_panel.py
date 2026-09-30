"""The bot end as the panel serves it: the Deploy form's check, a bot's end, and changing it (#2607).

Transport-facing only: the rules are ``app.services.bot_end``, the end lives
in the runner's desired state, and the Clerk carries it out
(``clerk.sqlite.scheduled_end``). This module scopes each read to the account
and translates refusals into the panel's typed errors.
"""

from __future__ import annotations

from app.config import settings
from app.schemas.bot_end import BotEndInput, BotEndPreviewRequest, BotEndView
from app.services.bot_end import (
    BotEndRefused,
    ResolvedBotEnd,
    resolve_bot_end,
    resolved_bot_end_view,
)
from app.services.bot_runner import BotTaskRegistry, get_bot_task_registry
from app.services.bot_runner import UnknownBotError as RunnerUnknownBotError
from app.services.broker_v2_panel.panel_errors import (
    PanelRunnerError,
    PanelUnavailableError,
    UnknownBotError,
)
from app.services.broker_v2_panel.panel_scope import validate_account, validate_account_scope
from app.utils.timestamps import now_ms_utc

#: A refused end on the Deploy form or the bot panel.
BOT_END_REFUSED = "BOT_END_REFUSED"


def resolve_deploy_end(end: BotEndInput | None, *, dry_run: bool) -> ResolvedBotEnd:
    """The end a Deploy records, or the panel's refusal in the owner's words."""
    try:
        return resolve_bot_end(end, now_ms=now_ms_utc(), dry_run=dry_run)
    except BotEndRefused as exc:
        raise _refused(exc) from exc


def preview_default_end() -> BotEndView:
    """The end a Deploy that names none gets now, for the Deploy form to pre-fill."""
    now_ms = now_ms_utc()
    return resolved_bot_end_view(resolve_bot_end(None, now_ms=now_ms, dry_run=False), now_ms=now_ms, dry_run=False)


async def preview_bot_end(broker: str, account_id: str, request: BotEndPreviewRequest) -> BotEndView:
    """The Deploy form's check: the end a Deploy would record, with any move it makes, or its refusal."""
    await validate_account(broker, account_id)
    now_ms = now_ms_utc()
    dry_run = request.execution_mode == "dry_run"
    try:
        resolved = resolve_bot_end(request.end, now_ms=now_ms, dry_run=dry_run)
    except BotEndRefused as exc:
        raise _refused(exc) from exc
    return resolved_bot_end_view(resolved, now_ms=now_ms, dry_run=dry_run)


async def read_bot_end(broker: str, account_id: str, sid: str) -> BotEndView:
    await validate_account_scope(broker, account_id, sid)
    try:
        return _runner().bot_end(broker, sid)
    except RunnerUnknownBotError as exc:
        raise UnknownBotError(str(exc), detail=exc.detail) from exc


async def edit_bot_end(broker: str, account_id: str, sid: str, choice: BotEndInput) -> BotEndView:
    """Change a bot's end now; the runner decides whether it may (``BotTaskRegistry.edit_bot_end``)."""
    await validate_account_scope(broker, account_id, sid)
    try:
        return await _runner().edit_bot_end(broker, sid, choice, updated_by=settings.PANEL_OPERATOR_IDENTITY)
    except BotEndRefused as exc:
        raise _refused(exc) from exc
    except RunnerUnknownBotError as exc:
        raise UnknownBotError(str(exc), detail=exc.detail) from exc


def _refused(exc: BotEndRefused) -> PanelRunnerError:
    return PanelRunnerError(
        str(exc), detail=exc.detail, next_action=exc.next_action, http_status=exc.http_status,
        reason_code=BOT_END_REFUSED,
    )


def _runner() -> BotTaskRegistry:
    registry = get_bot_task_registry()
    if registry is None:
        raise PanelUnavailableError(
            "The bot runner is not available.",
            detail="The service is still starting or has shut down.",
            next_action="Wait for the data plane to become healthy, then refresh.",
        )
    return registry


__all__ = [
    "BOT_END_REFUSED",
    "edit_bot_end",
    "preview_bot_end",
    "preview_default_end",
    "read_bot_end",
    "resolve_deploy_end",
]
