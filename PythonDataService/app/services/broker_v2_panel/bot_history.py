"""One account's bot history: every bot it ever ran, and its Dry Runs (#2574).

Served by the account's own Clerk. Three kinds of custody database hold an
account's bots, and each is read on its own query-only snapshot:

* the account's own Clerk database (Paper, Live, or Shadow while the account
  rehearses);
* for a Live account, the other of its two worlds when that database exists
  -- Shadow's ``shadow:<account>`` after the account went live, or the real
  one while it rehearses -- so a Shadow bot's history outlives the stage;
* each Dry Run's own ``sim:<bot>`` database.

Whatever cannot be read is named here, beside every row that could be read
-- never silently left out: the account's own database, its other world, a
Dry Run, or one bot whose own files (its configuration, lifecycle record or
run receipts) cannot be read. A bad file never hides its siblings, and the
account's own words reach the fleet coordinator in the answer, where a
refused read's would not (the lane router keeps a lane's 5xx body in its own
log). Only an account with no Clerk to read refuses the whole read.

The databases are read side by side, and a database no running Clerk owns
(the other world, each Dry Run) is read again only once its custody revision
moves (``RevisionMemo``), so an account's accumulated Dry Runs cost one read
each, once (#2615).

The read can be narrowed to one bot (``strategy_instance_id``): the bot's
own page opens History on all of its runs.

Every dollar is authored here from the custody projection
(``clerk.sqlite.bot_history``); every outcome is worded from its durable
receipt in the one outcome vocabulary (``outcome_copy``), so the browser adds
nothing up and invents no copy.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import NamedTuple

from app.broker.alpaca.clerk.account_authority import (
    authority_kind_for_account,
    is_shadow_account_id,
    live_account_id_for_shadow_account,
    shadow_account_id_for_live_account,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.money import display_dollars, dollars
from app.broker.alpaca.clerk.sqlite.bot_history import (
    BotFacts,
    CustodyHistory,
    CustodySchemaUnreadable,
    OrderCounts,
    RunFacts,
    read_custody_history,
)
from app.broker.alpaca.clerk.sqlite.budget_projection import RevisionMemo
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.broker.ibkr.config import live_artifacts_root
from app.schemas.account_authority import AuthorityKind
from app.schemas.bot_history import (
    AccountBotHistory,
    BotHistoryBot,
    BotHistoryGap,
    BotHistoryOrders,
    BotHistoryOutcome,
    BotHistoryRun,
    BotHistoryStatus,
    BotHistoryWorld,
)
from app.schemas.broker_bots import BotDutyOutcomeView
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_v2_panel.catalog_projection_service import (
    WORLD_LABELS,
    SqliteCatalogProjectionUnavailable,
    bot_status,
    bot_world,
    run_ended_at_ms,
)
from app.services.broker_v2_panel.outcome_copy import outcome_headline
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError
from app.services.broker_v2_panel.panel_scope import validate_account
from app.services.broker_v2_panel.sqlite_roster_status import (
    declared_configuration,
    latest_run_duty_outcome,
    lifecycle_record,
    run_receipt_outcome,
)
from app.services.sqlite_clerk_compat import active_sqlite_facade, custody_account_id_for_route
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

#: What reading one custody database can fail with: the file (missing,
#: locked, corrupt) or a fill lineage the projection cannot use. Our own
#: validation errors are bugs, never "could not be read".
_UNREADABLE = (sqlite3.Error, OSError, EconomicProjectionError)
#: What wording one bot can fail with: its immutable configuration, its
#: lifecycle record or a run receipt cannot be read.
_BOT_UNREADABLE = (SqliteCatalogProjectionUnavailable, OSError)

_BOT_GAP_COPY = "This bot's records could not be read, so it is not listed."
#: Why a whole database is missing: it could not be read, or it keeps a
#: schema this build cannot bring forward (``CustodySchemaUnreadable``).
_UNREADABLE_WHY = "could not be read"
_SCHEMA_WHY = "are kept in a record format this version cannot read"

#: One memo per database no running Clerk owns, for this process's life:
#: an account's Dry Runs and its other world, each kept to its last answer.
_SOURCE_MEMOS: dict[Path, RevisionMemo[CustodyHistory]] = {}
#: The same, for Home's read of a Shadow world's named bots: a memo keeps one
#: answer, and History's own read of that database names other bots.
_REHEARSAL_MEMOS: dict[Path, RevisionMemo[CustodyHistory]] = {}
#: How many of an account's databases one History read reads at once.
_SOURCES_READ_AT_ONCE = 4

_WORLDS: dict[AuthorityKind, BotHistoryWorld] = {
    "real_live": "live",
    "real_paper": "paper",
    "shadow": "shadow",
    "synthetic": "dry_run",
}

#: A Live account's other world, named in a sentence.
_OTHER_WORLD_NAMES: dict[AuthorityKind, str] = {"shadow": "Shadow", "real_live": "Live"}

_STATUS_LABELS: dict[BotHistoryStatus, str] = {
    "running": "Running",
    "holding": "Stopped · still holding",
    "finished": "Finished",
    "cleared": "Cleared",
}


@dataclass(frozen=True)
class _Source:
    """A custody database beside the account's own, which no running Clerk owns.

    Its fee evidence is therefore never fresh: a real Live database read this
    way shows its money as unknown, never as zero, while a simulated one's
    needs no broker evidence.
    """

    path: Path
    world: AuthorityKind
    #: The one Dry Run a ``sim:`` database holds; ``None`` for a whole world.
    strategy_instance_id: str | None = None
    #: Why its bots' pages cannot open: the account's workspace reads its own
    #: world and its Dry Runs, never its other world (``_sibling_sources``).
    page_unavailable_reason: str | None = None


async def account_bot_history(
    broker: str, account_id: str, *, strategy_instance_id: str | None = None,
) -> AccountBotHistory:
    """Every bot this account's Clerk holds, with its Dry Runs, newest first.

    ``strategy_instance_id`` narrows the read to that one bot, wherever it
    lives: the account's own database, its other world, or its own Dry Run.
    """
    resolved = await validate_account(broker, account_id)
    facade = active_sqlite_facade(broker)
    if facade is None or facade.account_id != custody_account_id_for_route(broker, resolved):
        raise PanelUnavailableError(
            "This account's bot history is unavailable.",
            detail="Restore the account's Clerk, then refresh.",
        )
    only = None if strategy_instance_id is None else (strategy_instance_id,)
    world = authority_kind_for_account(facade.account_id, account_mode=facade.account_mode)
    now_ms = now_ms_utc()
    reads = [
        # The account's own, with this Clerk's own fee-evidence freshness.
        _Read(world, None, partial(facade.repository.bot_history, only)),
        *(
            _Read(source.world, source.strategy_instance_id, partial(
                read_custody_history, source.path, now_ms=now_ms, fee_evidence_checked_at_ms=None,
                strategy_instance_ids=only if source.strategy_instance_id is None else (source.strategy_instance_id,),
                memo=_SOURCE_MEMOS.setdefault(source.path, RevisionMemo()),
            ), page_unavailable_reason=source.page_unavailable_reason)
            for source in (*_sibling_sources(facade), *_dry_run_sources(broker, only=strategy_instance_id))
        ),
    ]
    # At most a few at once: the worker threads are shared with the
    # Clerk's own lease heartbeat, which a history read must never hold up.
    limit = asyncio.Semaphore(_SOURCES_READ_AT_ONCE)
    answers = await asyncio.gather(*(_read_source(read, account_id=resolved, limit=limit) for read in reads))
    bots = sorted(
        (bot for read_bots, _ in answers for bot in read_bots),
        key=lambda bot: (-(bot.started_at_ms or 0), bot.strategy_instance_id),
    )
    gaps = tuple(gap for _, read_gaps in answers for gap in read_gaps)
    return AccountBotHistory(account_id=resolved, observed_at_ms=now_ms, bots=tuple(bots), gaps=gaps)


async def shadow_rehearsal_bots(
    facade: SqliteAlpacaClerkFacade, *, account_id: str, strategy_instance_ids: Sequence[str],
) -> tuple[BotHistoryBot, ...]:
    """The named bots of a graduated Live account's Shadow world, as History words them (#2694).

    Home's Finished fold lists a rehearsal bot from this read. An account
    with no such world, or one whose database cannot be read, answers none
    here; History names that gap.
    """
    only = tuple(strategy_instance_ids)
    now_ms = now_ms_utc()
    bots: list[BotHistoryBot] = []
    for source in _sibling_sources(facade):
        if source.world != "shadow":
            continue
        read = _Read(source.world, None, partial(
            read_custody_history, source.path, now_ms=now_ms, fee_evidence_checked_at_ms=None,
            strategy_instance_ids=only, memo=_REHEARSAL_MEMOS.setdefault(source.path, RevisionMemo()),
        ), page_unavailable_reason=source.page_unavailable_reason)
        read_bots, _gaps = await _read_source(read, account_id=account_id, limit=asyncio.Semaphore(1))
        bots.extend(read_bots)
    return tuple(bots)


class _Read(NamedTuple):
    """One database this history reads."""

    world: AuthorityKind
    #: The one Dry Run it holds; ``None`` for a whole world.
    dry_run_sid: str | None
    read: Callable[[], CustodyHistory]
    #: Set for a world the account's workspace does not read (``_Source``).
    page_unavailable_reason: str | None = None


async def _read_source(
    source: _Read, *, account_id: str, limit: asyncio.Semaphore,
) -> tuple[tuple[BotHistoryBot, ...], tuple[BotHistoryGap, ...]]:
    """One database's bots and gaps; a database that cannot be read is its own gap."""
    log = {"action": "bot_history_source_unreadable", "account_id": account_id, "world": source.world,
           "strategy_instance_id": source.dry_run_sid}
    async with limit:
        try:
            history = await asyncio.to_thread(source.read)
        except CustodySchemaUnreadable as exc:
            logger.warning(
                "A bot-history source keeps a schema this build cannot read; it is named as a gap",
                extra={**log, "schema_version": exc.schema_version},
            )
            return (), (_gap_for(source.world, source.dry_run_sid, why=_SCHEMA_WHY),)
        except _UNREADABLE:
            logger.warning("A bot-history source could not be read; it is named as a gap", exc_info=True, extra=log)
            return (), (_gap_for(source.world, source.dry_run_sid, why=_UNREADABLE_WHY),)
        return await asyncio.to_thread(
            compose_bots, history, authority_world=source.world, account_id=account_id,
            page_unavailable_reason=source.page_unavailable_reason,
        )


def _sibling_sources(facade: SqliteAlpacaClerkFacade) -> tuple[_Source, ...]:
    """A Live account's other world, when its database exists (see module doc).

    It sits in the same account tree as the account's own database.
    """
    if is_shadow_account_id(facade.account_id):
        sibling = live_account_id_for_shadow_account(facade.account_id)
    elif facade.account_mode == "live":
        sibling = shadow_account_id_for_live_account(facade.account_id)
    else:
        return ()
    path = facade.repository.neighbour_custody_file(sibling)
    if not path.is_file():
        return ()
    world = authority_kind_for_account(sibling, account_mode="live")
    return (_Source(
        path=path, world=world,
        page_unavailable_reason=(
            f"This bot ran in the account's {_OTHER_WORLD_NAMES[world]} world, which the account's pages "
            "don't open, so it has no page of its own. History keeps its record."
        ),
    ),)


def _dry_run_sources(broker: str, *, only: str | None) -> tuple[_Source, ...]:
    """Each Dry Run this lane deployed (or just ``only``), from its own ``sim:`` database."""
    registry = get_bot_task_registry()
    if registry is None:
        return ()
    return tuple(
        _Source(
            path=confined_account_file(
                live_artifacts_root(), synthetic_account_id_for_strategy(binding.strategy_instance_id), DB_FILENAME,
            ),
            world="synthetic",
            strategy_instance_id=binding.strategy_instance_id,
        )
        for binding in registry.bindings_for_broker(broker)
        if binding.mode == "dry_run" and only in (None, binding.strategy_instance_id)
    )


def _gap_for(world: AuthorityKind, dry_run_sid: str | None, *, why: str) -> BotHistoryGap:
    if dry_run_sid is not None:
        return BotHistoryGap(strategy_instance_id=dry_run_sid, reason=f"This Dry Run's own records {why}, so it is not listed.")
    return BotHistoryGap(
        strategy_instance_id=None,
        reason=f"This account's {WORLD_LABELS[world]} bots {why}, so they are not listed.",
    )


def compose_bots(
    history: CustodyHistory, *, authority_world: AuthorityKind, account_id: str,
    page_unavailable_reason: str | None = None,
) -> tuple[tuple[BotHistoryBot, ...], tuple[BotHistoryGap, ...]]:
    """Word one custody database's bots, each on its own: one it cannot read is a gap.

    Blocking: reads lifecycle files and receipts.
    """
    bots: list[BotHistoryBot] = []
    gaps: list[BotHistoryGap] = []
    for facts in history.bots:
        try:
            bots.append(_bot(
                facts, authority_world=authority_world, account_id=account_id,
                money_unavailable=history.money_unavailable, page_unavailable_reason=page_unavailable_reason,
            ))
        except _BOT_UNREADABLE:
            logger.warning(
                "One bot's history could not be read; it is named as a gap",
                exc_info=True,
                extra={"action": "bot_history_bot_unreadable", "account_id": account_id,
                       "strategy_instance_id": facts.strategy_instance_id},
            )
            gaps.append(BotHistoryGap(strategy_instance_id=facts.strategy_instance_id, reason=_BOT_GAP_COPY))
    return tuple(bots), tuple(gaps)


def _bot(
    facts: BotFacts, *, authority_world: AuthorityKind, account_id: str, money_unavailable: str | None,
    page_unavailable_reason: str | None,
) -> BotHistoryBot:
    sid = facts.strategy_instance_id
    if facts.config is None:
        raise SqliteCatalogProjectionUnavailable(f"Bot '{sid}' has no immutable configuration.")
    world = bot_world(authority_world, declared_configuration(sid, facts.config.config_json).mode)
    runs = _runs(sid, facts.runs, retired_at_ms=facts.retired_at_ms)
    running = bool(runs) and runs[0].running
    status = bot_status(
        retired=facts.retired_at_ms is not None, live_custody=facts.live_custody,
        running=running, holds_money=facts.holds_money,
    )
    return BotHistoryBot(
        strategy_instance_id=sid,
        strategy_key=facts.config.strategy_key,
        strategy_label=facts.config.display_name,
        symbol=facts.symbol,
        account_id=account_id,
        world=_WORLDS[world],
        world_label=WORLD_LABELS[world],
        status=status,
        status_label=_STATUS_LABELS[status],
        started_at_ms=runs[-1].started_at_ms if runs else None,
        stopped_at_ms=runs[0].stopped_at_ms if runs else run_ended_at_ms(
            running=False, stop_ms=None, outcome_recorded_at_ms=None, retired_at_ms=facts.retired_at_ms,
        ),
        outcome=runs[0].outcome if runs else None,
        transaction_count=facts.transactions,
        orders=_orders(facts.orders),
        budget_usd=None if facts.committed_cents is None else dollars(facts.committed_cents),
        result_usd=_usd(facts.result),
        fees_usd=_usd(facts.fees),
        money_unavailable_reason=(
            None if facts.result is not None and facts.fees is not None
            else money_unavailable or "The result and fees could not be read."
        ),
        money_scope_note=(
            f"Result and fees are for all {len(runs)} runs of this bot; they are not split by run."
            if len(runs) > 1 else None
        ),
        runs=runs,
        page_unavailable_reason=page_unavailable_reason,
    )


def _runs(sid: str, facts: Sequence[RunFacts], *, retired_at_ms: int | None) -> tuple[BotHistoryRun, ...]:
    """Each run, newest first, with how and when it ended.

    The newest run's end follows the roster's own precedence
    (``latest_run_duty_outcome``); an older run's is its receipt. Clearing a
    bot records a registration exit, not how its run ended, so the newest
    run's receipt answers first when the lifecycle says only "retired".
    When it ended is ``run_ended_at_ms``'s answer, the roster's own.
    """
    lifecycle = lifecycle_record(sid) if facts else None
    rows: list[BotHistoryRun] = []
    for index, run in enumerate(facts):
        if index == 0:
            view = latest_run_duty_outcome(sid, lifecycle, run.lifecycle_run_id, running=run.active)
            if view is None or view.kind == "RETIRED":
                view = run_receipt_outcome(sid, run.lifecycle_run_id) or view
        else:
            view = run_receipt_outcome(sid, run.lifecycle_run_id)
        running = run.active and view is None
        rows.append(BotHistoryRun(
            run_id=run.lifecycle_run_id,
            started_at_ms=run.started_at_ms,
            stopped_at_ms=run_ended_at_ms(
                running=running,
                stop_ms=run.stopped_at_ms,
                outcome_recorded_at_ms=None if view is None else view.recorded_at_ms,
                retired_at_ms=retired_at_ms if index == 0 else None,
            ),
            running=running,
            outcome=_outcome(view, flattened=run.flattened),
            transaction_count=run.transactions,
            orders=_orders(run.orders),
        ))
    return tuple(rows)


def _outcome(view: BotDutyOutcomeView | None, *, flattened: bool) -> BotHistoryOutcome | None:
    if view is None:
        return None
    return BotHistoryOutcome(
        kind=view.kind, reason_code=view.reason_code, recorded_at_ms=view.recorded_at_ms,
        headline=outcome_headline(view.kind, view.reason_code, flattened=flattened),
    )


def _orders(counts: OrderCounts) -> BotHistoryOrders:
    return BotHistoryOrders(
        sent=counts.sent, filled=counts.filled, cancelled=counts.cancelled, rejected=counts.rejected,
    )


def _usd(amount: Decimal | None) -> str | None:
    return None if amount is None else display_dollars(amount)


__all__ = ["account_bot_history", "compose_bots", "shadow_rehearsal_bots"]
