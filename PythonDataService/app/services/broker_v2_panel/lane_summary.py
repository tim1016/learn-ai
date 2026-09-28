"""One lane's own summary: its attention set and the counts its account card shows.

The attention read (``GET /{broker}/attention``) and the counts this lane
reports on every heartbeat come from here, so the Accounts page's attention
count is always the number of items the lane's bell lists (PRD #2560), and
its bot counts are the lane's own facts rather than a browser tally. Stopped
bots still holding money are the account-money read's stopped slices, which
the card already reads, so the beat never takes the custody write fence.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.projections import project_uncertainties
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.broker_v2_panel import LaneAttentionItem, LaneAttentionRead
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_v2_panel.sqlite_panel_source import read_account_custody

logger = logging.getLogger(__name__)


async def lane_attention_read() -> LaneAttentionRead:
    """Everything currently needing the operator on this lane (#2228).

    The lane's active uncertainties (including ``EXIT_NOT_FLAT`` and every
    exit waiting for an operator) plus its terminal-exposure notices, from
    the lane's own custody ledger alone: a lane with no active authority
    answers empty rather than unknown.
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
    items = [
        LaneAttentionItem(
            condition_id=uncertainty.uncertainty_id,
            reason_code=uncertainty.reason_code,
            severity=uncertainty.severity,
            strategy_instance_id=uncertainty.strategy_instance_id,
            symbol=uncertainty.symbol,
            headline=uncertainty.headline,
            recovery_status=uncertainty.recovery_status,
        )
        for uncertainty in uncertainties
    ]
    items.extend(LaneAttentionItem(
        condition_id=f"terminal-exposure:{notice.strategy_instance_id}:{notice.kind}",
        reason_code=notice.kind.upper(), kind=notice.kind, severity="warning",
        strategy_instance_id=notice.strategy_instance_id, symbol=notice.symbol,
        headline=notice.label, action_label=notice.action_label,
    ) for notice in notices)
    return LaneAttentionRead(account_id=repository.account_id, items=items)


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
