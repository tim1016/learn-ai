"""The one selection of the Clerk authority that custodies a bot (hurdle H33, #2563).

Panel reads, panel actions, the bot's money, its recovery and every per-bot
Clerk route act in the authority this module selects, so a Dry Run's reads and
actions can never land in two different worlds:

- a Dry Run bot is custodied in its own ``sim:<strategy_instance_id>``
  simulator, reopened for the request when the bot is stopped -- its ports
  never reach Alpaca;
- a Dry Run whose Deploy committed in its simulator but crashed before the
  launch recorded its binding is found through that simulator's own
  activation, exactly as its money is;
- every other bot, and a custody subject the runner never bound (a manual
  order's), belongs to the account's own authority -- ``None`` when none is
  installed.

Route authorization against the account stays the caller's: this selects where
an already-authorized request acts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import SQLITE_FACADE_AUTHORITIES, ActiveClerkRuntime
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.services.bot_runner import get_bot_task_registry
from app.services.bot_runner_errors import InvalidStrategyInstanceIdError, UnknownBotError
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError


@asynccontextmanager
async def bot_clerk_runtime(broker: str, sid: str) -> AsyncIterator[ActiveClerkRuntime | None]:
    """The Clerk runtime that custodies bot ``sid``, held open for the caller's request."""
    registry = get_bot_task_registry()
    try:
        binding = None if registry is None else registry.binding_for_control(broker, sid)
    except (UnknownBotError, InvalidStrategyInstanceIdError):
        binding = None
    if binding is not None:
        async with binding_clerk_runtime(registry, binding) as runtime:
            yield runtime
        return
    account = _account_runtime(broker)
    repository = None if account is None else account.sqlite_repository
    unbound = getattr(registry, "unbound_synthetic_runtime_for_projection", None)
    if callable(unbound) and repository is not None and repository.deployment_budget(sid) is None:
        async with unbound(sid) as runtime:
            if runtime is not None:
                yield _own_simulator(runtime)
                return
    yield account


@asynccontextmanager
async def binding_clerk_runtime(registry: object, binding: object) -> AsyncIterator[ActiveClerkRuntime | None]:
    """The same selection for a caller that already holds the bot's binding.

    ``getattr`` because several callers hand this a duck-typed binding that
    carries only what the projection needs.
    """
    if getattr(binding, "mode", None) != "dry_run":
        yield _account_runtime(str(getattr(binding, "broker", "alpaca")))
        return
    projection_runtime = getattr(registry, "synthetic_runtime_for_projection", None)
    if not callable(projection_runtime):
        raise PanelUnavailableError(
            "The Dry Run custody authority is unavailable.",
            detail="The bot runner cannot compose the sealed synthetic Clerk for this projection.",
        )
    async with projection_runtime(binding) as runtime:
        yield _own_simulator(runtime)


def custody_facade(runtime: ActiveClerkRuntime | None) -> SqliteAlpacaClerkFacade | None:
    """The SQLite Clerk facade of a selected runtime, or ``None`` when it has none."""
    if runtime is None or not isinstance(runtime.clerk, SqliteAlpacaClerkFacade):
        return None
    return runtime.clerk if runtime.authority_kind in {*SQLITE_FACADE_AUTHORITIES, "synthetic"} else None


@asynccontextmanager
async def bot_custody_facade(broker: str, sid: str) -> AsyncIterator[SqliteAlpacaClerkFacade | None]:
    """``bot_clerk_runtime``'s facade, for a caller that acts through the Clerk facade."""
    async with bot_clerk_runtime(broker, sid) as runtime:
        yield custody_facade(runtime)


def _account_runtime(broker: str) -> ActiveClerkRuntime | None:
    return get_active_clerk_runtime() if broker == "alpaca" else None


def _own_simulator(runtime: ActiveClerkRuntime) -> ActiveClerkRuntime:
    if not isinstance(runtime.clerk, SqliteAlpacaClerkFacade) or runtime.authority_kind != "synthetic":
        # The cause rides with the refusal (#2582): a Dry Run that failed to
        # open says why on its own panel, not only in the lane's log.
        detail = "This Dry Run's own simulated Clerk could not be opened."
        if runtime.startup_failure is not None:
            detail = f"{detail} {runtime.startup_failure.recovery}"
        raise PanelUnavailableError("The Dry Run custody authority is unavailable.", detail=detail)
    return runtime
