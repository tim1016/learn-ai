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

The read can be narrowed to one bot (``strategy_instance_id``): the bot's
own page opens History on all of its runs.

Every dollar is authored here from the custody projection
(``clerk.sqlite.bot_history``); every outcome is worded here from its
durable receipt, so the browser adds nothing up and invents no copy.
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

from app.broker.alpaca.clerk.account_authority import (
    authority_kind_for_account,
    is_shadow_account_id,
    live_account_id_for_shadow_account,
    shadow_account_id_for_live_account,
    synthetic_account_id_for_strategy,
)
from app.broker.alpaca.clerk.money import display_cents, dollars
from app.broker.alpaca.clerk.sqlite.bot_history import (
    BotFacts,
    CustodyHistory,
    OrderCounts,
    RunFacts,
    read_custody_history,
)
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicProjectionError
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.broker.ibkr.config import live_artifacts_root
from app.marketdata.feed import FEED_REFUSAL_REASON_CODES
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
from app.services.bot_end import SCHEDULED_END_REASON_CODE
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_v2_panel.catalog_projection_service import (
    WORLD_LABELS,
    SqliteCatalogProjectionUnavailable,
    bot_status,
    bot_world,
    run_ended_at_ms,
)
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
_DRY_RUN_GAP_COPY = "This Dry Run's own records could not be read, so it is not listed."

_WORLDS: dict[AuthorityKind, BotHistoryWorld] = {
    "real_live": "live",
    "real_paper": "paper",
    "shadow": "shadow",
    "synthetic": "dry_run",
}

_STATUS_LABELS: dict[BotHistoryStatus, str] = {
    "running": "Running",
    "holding": "Stopped · still holding",
    "finished": "Finished",
    "cleared": "Cleared",
}

#: How a run ended, in the owner's words (#2574) -- one entry for every
#: ``BotDutyOutcomeKind`` (a test holds it to that list). A reason code
#: worded on its own wins over its kind's words.
_OUTCOME_HEADLINES: dict[str, str] = {
    "STOPPED": "Stopped by you",
    "HALTED": "Halted",
    "CRASHED": "Crashed",
    "FAILED_LAUNCH": "Failed to launch",
    "EXITED_UNVERIFIED": "Ended without a clean exit",
    "RETIRED": "Retired before its end was recorded",
}
_REASON_HEADLINES: dict[str, str] = {
    "FEED_DEATH": "Crashed because market data stopped",
    "SERVICE_SHUTDOWN": "Stopped when the service shut down",
    SCHEDULED_END_REASON_CODE: "Ended at its scheduled time",
    **{code: "Stopped because its market data could not be used" for code in FEED_REFUSAL_REASON_CODES},
}
_FLATTENED_HEADLINE = "Stopped and flattened"
#: An outcome kind this build has no words for still ends its run -- never
#: the whole read.
_UNWORDED_HEADLINE = "Ended"


def outcome_headline(kind: str, reason_code: str, *, flattened: bool) -> str:
    """One run's end in plain words: its reason's own words, else its kind's."""
    if kind == "STOPPED" and flattened:
        return _FLATTENED_HEADLINE
    headline = _REASON_HEADLINES.get(reason_code) or _OUTCOME_HEADLINES.get(kind)
    if headline is None:
        logger.warning(
            "A run outcome has no History words; it reads as ended",
            extra={"action": "bot_history_outcome_unworded", "kind": kind, "reason_code": reason_code},
        )
        return _UNWORDED_HEADLINE
    return headline


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
    bots: list[BotHistoryBot] = []
    gaps: list[BotHistoryGap] = []
    # Each database this history reads: the world it holds, the one Dry Run
    # it holds (``None`` for a whole world), and its read.
    reads: list[tuple[AuthorityKind, str | None, Callable[[], CustodyHistory]]] = [
        # The account's own, with this Clerk's own fee-evidence freshness.
        (world, None, partial(facade.repository.bot_history, only)),
        *(
            (source.world, source.strategy_instance_id, partial(
                read_custody_history, source.path, now_ms=now_ms, fee_evidence_checked_at_ms=None,
                strategy_instance_ids=only if source.strategy_instance_id is None else (source.strategy_instance_id,),
            ))
            for source in (*_sibling_sources(facade), *_dry_run_sources(broker, only=strategy_instance_id))
        ),
    ]
    for source_world, dry_run_sid, read in reads:
        try:
            history = await asyncio.to_thread(read)
        except _UNREADABLE:
            logger.warning(
                "A bot-history source could not be read; it is named as a gap",
                exc_info=True,
                extra={"action": "bot_history_source_unreadable", "account_id": resolved, "world": source_world,
                       "strategy_instance_id": dry_run_sid},
            )
            gaps.append(_gap_for(source_world, dry_run_sid))
            continue
        read_bots, read_gaps = await asyncio.to_thread(
            compose_bots, history, authority_world=source_world, account_id=resolved,
        )
        bots += read_bots
        gaps += read_gaps
    bots.sort(key=lambda bot: (-(bot.started_at_ms or 0), bot.strategy_instance_id))
    return AccountBotHistory(account_id=resolved, observed_at_ms=now_ms, bots=tuple(bots), gaps=tuple(gaps))


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
    return (_Source(path=path, world=authority_kind_for_account(sibling, account_mode="live")),)


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


def _gap_for(world: AuthorityKind, dry_run_sid: str | None) -> BotHistoryGap:
    if dry_run_sid is not None:
        return BotHistoryGap(strategy_instance_id=dry_run_sid, reason=_DRY_RUN_GAP_COPY)
    return BotHistoryGap(
        strategy_instance_id=None,
        reason=f"This account's {WORLD_LABELS[world]} bots could not be read, so they are not listed.",
    )


def compose_bots(
    history: CustodyHistory, *, authority_world: AuthorityKind, account_id: str,
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
                money_unavailable=history.money_unavailable,
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
    return None if amount is None else dollars(display_cents(amount))


__all__ = ["account_bot_history", "compose_bots", "outcome_headline"]
