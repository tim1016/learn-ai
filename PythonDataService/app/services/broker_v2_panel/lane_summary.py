"""One lane's own summary: its attention set and the counts its account card shows.

The attention read (``GET /{broker}/attention``) and the counts this lane
reports on every heartbeat come from here, so the Accounts page's attention
count is always the number of items the lane's bell lists (PRD #2560), and
its bot counts are the lane's own facts rather than a browser tally. Stopped
bots still holding money are the account-money read's stopped slices, which
the card already reads, so the beat never takes the custody write fence.

Every attention item is one line on the account's Home: a backend-authored
headline and exactly one fix, a link to where that fix lives. Only what the
owner must act on is listed -- an old bot that is flat and fully released is
Finished, not attention (hurdles H13, H34).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedUncertainty
from app.broker.alpaca.clerk.sqlite.projections import project_uncertainties
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXECUTION_PRICE_CONFLICT_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    FAILED_ENTER_FILLED_REASON_CODE,
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    STREAM_HEALTH_HOLD_REASON_CODE,
    UNEXPLAINED_ORDER_HOLD_REASON_CODE,
    UNFOLDABLE_BROKER_ORDER_REASON_CODE,
)
from app.schemas.broker_v2_panel import (
    LaneAttentionAction,
    LaneAttentionItem,
    LaneAttentionKind,
    LaneAttentionRead,
)
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_v2_panel.budget_deploy import LEGACY_BUDGET_DETAIL
from app.services.broker_v2_panel.catalog_projection_service import holdings_text
from app.services.broker_v2_panel.sqlite_panel_source import read_account_custody

logger = logging.getLogger(__name__)

_OPEN_BOT = LaneAttentionAction(label="Open bot", destination="bot")
_ORDER_RECORDS = LaneAttentionAction(label="Open order records", destination="activity")

#: What each episode is about, and where its fix lives. A hold on losses is
#: cleared in Settings; a channel is checked there; an order or position the
#: account and Alpaca disagree on is recovered from Activity's order records;
#: an exit that has not flattened is its bot's. An episode not named here
#: opens its bot, or the order records when it names none.
_EPISODE_LINES: dict[str, tuple[LaneAttentionKind, LaneAttentionAction]] = {
    LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE: ("hold", LaneAttentionAction(label="Open Settings", destination="settings")),
    STREAM_HEALTH_HOLD_REASON_CODE: ("channel", LaneAttentionAction(label="Check connection", destination="settings")),
    **{
        reason_code: ("out_of_sync", _ORDER_RECORDS)
        for reason_code in (
            UNEXPLAINED_ORDER_HOLD_REASON_CODE,
            UNFOLDABLE_BROKER_ORDER_REASON_CODE,
            POSITION_DRIFT_REASON_CODE,
            BROKER_SNAPSHOT_STALE_REASON_CODE,
            RECONCILIATION_INCOMPLETE_REASON_CODE,
            ORDER_OUTCOME_UNKNOWN_REASON_CODE,
            EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
            EXECUTION_PRICE_CONFLICT_REASON_CODE,
            FAILED_ENTER_FILLED_REASON_CODE,
        )
    },
    EXIT_NOT_FLAT_REASON_CODE: ("exit", _OPEN_BOT),
    EXIT_STUCK_REASON_CODE: ("exit", _OPEN_BOT),
}

#: The provider behind each channel, named as the owner knows it: IBKR
#: supplies market data and Alpaca the account's orders.
_CHANNEL_NAMES = {"market_data": "IBKR market data", "execution": "Alpaca order updates"}


async def lane_attention_read() -> LaneAttentionRead:
    """Everything currently needing the owner on this lane (#2228, PRD #2560).

    From the lane's own custody ledger alone: its active episodes (holds,
    channels, out-of-sync orders and positions, exits that have not
    flattened), bots it cannot vouch for, stopped bots still holding money,
    and an account that has not switched to budgets. A lane with no active
    authority answers empty rather than unknown.
    """
    runtime = get_active_clerk_runtime()
    repository = None if runtime is None else runtime.sqlite_repository
    if runtime is None or repository is None:
        return LaneAttentionRead(account_id=None, items=[])
    # Read eligibility from the selected facade, exactly as the desk and
    # panel do. A repository without its policy authority cannot name a time.
    clerk = runtime.clerk
    notices = []
    if isinstance(clerk, SqliteAlpacaClerkFacade):
        projection, notices = await read_account_custody(clerk)
        uncertainties = projection.uncertainties
    else:
        uncertainties = project_uncertainties(
            repository.active_uncertainties(),
            now_ms=repository.clock(),
            exits_in_progress=repository.strategies_with_active_exit,
        )
    items = [_episode_item(uncertainty) for uncertainty in uncertainties]
    # The other notice kinds are a stopped bot still holding money, which
    # every stopped bot gets below; this one says the Clerk cannot vouch for
    # what the bot holds.
    items.extend(LaneAttentionItem(
        condition_id=f"terminal-exposure:{notice.strategy_instance_id}:{notice.kind}",
        reason_code=notice.kind.upper(), kind="position_unverified", severity="warning",
        strategy_instance_id=notice.strategy_instance_id, symbol=notice.symbol,
        headline=notice.label, action=_OPEN_BOT,
    ) for notice in notices if notice.kind == "position_unverified")
    # One line per position: a bot whose exit or custody is already named
    # above is not named again as merely stopped.
    named = {item.strategy_instance_id for item in items if item.kind in ("exit", "position_unverified")}
    items.extend(_stopped_holding_items(repository, already_named=named))
    if repository.budget_authority_version() < 2:
        items.append(LaneAttentionItem(
            condition_id="legacy-budget", reason_code="BUDGETS_NOT_SWITCHED_ON", kind="legacy_budget",
            severity="warning", headline=LEGACY_BUDGET_DETAIL,
            action=LaneAttentionAction(label="Open Settings", destination="settings"),
        ))
    return LaneAttentionRead(account_id=repository.account_id, items=items)


def _episode_item(uncertainty: ProjectedUncertainty) -> LaneAttentionItem:
    """One active episode as one line, with the fix its kind names."""
    kind, action = _EPISODE_LINES.get(
        uncertainty.reason_code,
        ("uncertainty", _ORDER_RECORDS if uncertainty.strategy_instance_id is None else _OPEN_BOT),
    )
    headline = (
        _channel_headline(uncertainty.evidence_refs)
        if uncertainty.reason_code == STREAM_HEALTH_HOLD_REASON_CODE
        else uncertainty.headline
    )
    return LaneAttentionItem(
        condition_id=uncertainty.uncertainty_id,
        reason_code=uncertainty.reason_code,
        kind=kind,
        action=action,
        severity=uncertainty.severity,
        strategy_instance_id=uncertainty.strategy_instance_id,
        symbol=uncertainty.symbol,
        headline=headline,
        recovery_status=uncertainty.recovery_status,
    )


def _channel_headline(evidence_refs: tuple[str, ...]) -> str:
    """Which connection holds new entries, from the hold's own evidence lines.

    Each line is ``"{stream}: {reason}"`` (``StreamHealthHoldCause``): the
    stream is a closed name, the reason the provider's own words.
    """
    down = "; ".join(
        f"{_CHANNEL_NAMES.get(stream.strip(), stream.strip())} ({reason.strip()})"
        for stream, _, reason in (line.partition(":") for line in evidence_refs)
    )
    return f"New entries are on hold while a connection is down: {down}." if down else (
        "New entries are on hold while a connection is down."
    )


def _stopped_holding_items(
    repository: ClerkSqliteRepository, *, already_named: set[str | None],
) -> list[LaneAttentionItem]:
    """A stopped bot still holding money: nothing manages it (PRD #2560 D7).

    The same fact that keeps its Home row in the holding group
    (``bots_holding_money``). Shares are flattened from its page; a working
    entry order with no shares yet is cancelled there too.
    """
    items = []
    for sid in sorted(repository.bots_holding_money()):
        if sid in already_named or repository.active_run(sid) is not None:
            continue
        held = {
            symbol: quantity
            for symbol, quantity in repository.attributed_positions_for_strategy(sid).items()
            if position_quantity_is_nonzero(quantity)
        }
        what = f"still holds {holdings_text(held)}" if held else "has an entry order still working"
        items.append(LaneAttentionItem(
            condition_id=f"stopped-holding:{sid}", reason_code="STOPPED_STILL_HOLDING", kind="stopped_holding",
            severity="warning", strategy_instance_id=sid, symbol=min(held, default=None),
            headline=f"{sid} is stopped but {what}. No bot is managing it.",
            action=LaneAttentionAction(label="Flatten…", destination="bot") if held else _OPEN_BOT,
        ))
    return items


@dataclass(frozen=True)
class LaneCounts:
    """What the lane's account card counts; ``None`` is "not counted", never 0.

    ``running_count`` is bots running in the lane's own world, ``dry_run_count``
    running Dry Runs and ``attention_count`` the attention read's items.
    """

    running_count: int | None = None
    dry_run_count: int | None = None
    attention_count: int | None = None

    def reported(self) -> dict[str, int]:
        return {key: value for key, value in asdict(self).items() if value is not None}


async def lane_counts() -> LaneCounts:
    """Count this lane's bots and attention items; each count fails alone."""
    bots = await _counted("bots", _bot_counts)
    running, dry_run = (None, None) if bots is None else bots
    return LaneCounts(
        running_count=running,
        dry_run_count=dry_run,
        attention_count=await _counted("attention", _attention_count),
    )


async def _counted[T](name: str, count: Callable[[], Awaitable[T | None]]) -> T | None:
    """One count, or ``None`` with a logged reason: a count never ends a beat."""
    try:
        return await count()
    except Exception:
        logger.warning(
            "Lane count unavailable; omitting it from this beat",
            exc_info=True, extra={"action": "lane_count_unavailable", "count": name},
        )
        return None


async def _bot_counts() -> tuple[int, int] | None:
    registry = get_bot_task_registry()
    if registry is None:
        return None
    statuses = await asyncio.to_thread(registry.list_bots, "alpaca")
    running = [status for status in statuses if status.running]
    return (
        sum(1 for status in running if status.mode == "trade"),
        sum(1 for status in running if status.mode == "dry_run"),
    )


async def _attention_count() -> int:
    return len((await lane_attention_read()).items)
