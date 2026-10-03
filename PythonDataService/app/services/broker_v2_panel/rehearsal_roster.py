"""Home's rows for the bots that rehearsed in a Live account's Shadow world (#2694).

A bot that rehearsed on a live account's ``shadow:`` store stays sealed on it
after the account graduates. The installed Clerk holds no custody record for
it, so the roster has no row for it and it has no page -- yet Clear, on Home's
Finished fold, is the one way a bot leaves the list. A finished one is listed
there from its own store's records, read as History reads them (a query-only
snapshot, never the store's lease): stopped, flat, no money still claimed.
One still holding stays in History alone, where nothing offers to clear it.

Whether a listed bot clears is not decided here: Clear re-reads the store
under its lease and applies the whole proof (``BotTaskRegistry.archive``), so
a holding History does not count -- an open uncertainty, say -- is refused
there, by name.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from app.broker.alpaca.clerk.account_authority import is_shadow_account_id
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.bot_history import BotHistoryBot
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotCatalogView
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_runner import BotTaskRegistry
from app.services.broker_v2_panel.bot_history import shadow_rehearsal_bots
from app.services.broker_v2_panel.catalog_projection_service import (
    CatalogEconomicRollup,
    compose_catalog_view,
)


class _Rehearsal(NamedTuple):
    """One stopped, uncleared bot sealed on a ``shadow:`` store the installed authority does not custody."""

    status: BotStatusView
    sealed_account_id: str


async def finished_rehearsal_rows(
    broker: str,
    facade: SqliteAlpacaClerkFacade,
    registry: BotTaskRegistry,
    bindings: Sequence[BrokerBotBinding],
) -> list[BotCatalogView]:
    """One Finished row per rehearsal bot whose Shadow records show it stopped and flat."""
    if is_shadow_account_id(facade.account_id):
        # The installed authority is the Shadow world itself: its bots are
        # its own, on its own pages.
        return []
    rehearsals = _uncleared_rehearsals(broker, registry, bindings)
    if not rehearsals:
        return []
    bots = await shadow_rehearsal_bots(
        facade, account_id=facade.account_id, strategy_instance_ids=sorted(rehearsals)
    )
    return [
        _finished_row(rehearsals[bot.strategy_instance_id], bot)
        for bot in bots
        if bot.status == "finished"
    ]


def _uncleared_rehearsals(
    broker: str, registry: BotTaskRegistry, bindings: Sequence[BrokerBotBinding]
) -> dict[str, _Rehearsal]:
    rehearsals: dict[str, _Rehearsal] = {}
    for binding in bindings:
        # The classification reads the bot's files, so a binding that names
        # no shadow store is passed over on what is already in hand.
        if binding.mode == "dry_run" or not is_shadow_account_id(binding.sealed_account_id or ""):
            continue
        foreign = registry.foreign_binding(binding.strategy_instance_id)
        if foreign is None or not is_shadow_account_id(foreign.sealed_account_id):
            continue
        status = registry.status(broker, binding.strategy_instance_id)
        # A cleared bot is off Home, and no bot the runner still runs is finished.
        if status.phase != "RETIRED" and not status.running:
            rehearsals[binding.strategy_instance_id] = _Rehearsal(status, foreign.sealed_account_id)
    return rehearsals


def _finished_row(rehearsal: _Rehearsal, bot: BotHistoryBot) -> BotCatalogView:
    """The roster row of a finished rehearsal bot: what its store's records show, and no page."""
    return compose_catalog_view(
        rehearsal.status,
        # Finished is stopped and flat; the day's rollups are the installed
        # account's and say nothing about a store it does not custody.
        CatalogEconomicRollup(
            sid=bot.strategy_instance_id,
            exposure={},
            fills_today=None,
            realized_pnl_today=None,
            open_pnl=None,
            last_activity_at_ms=None,
            needs_attention=False,
        ),
        account_id=rehearsal.sealed_account_id,
        world="shadow",
        holds_money=False,
        latest_stop_ms=bot.stopped_at_ms,
    ).with_facts(
        strategy_label=bot.strategy_label,
        trade_count=bot.transaction_count,
        final_result_usd=bot.result_usd,
        page_unavailable_reason=bot.page_unavailable_reason,
    )


__all__ = ["finished_rehearsal_rows"]
