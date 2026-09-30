"""One lane's own summary: its attention set and the counts its account card shows.

The attention read (``GET /{broker}/attention``) and the counts this lane
reports on every heartbeat come from here, so the Accounts page's attention
count is always the number of items the lane's bell lists (PRD #2560), and
its bot counts are the lane's own facts rather than a browser tally. The
heartbeat reaches ``lane_counts`` through the probe the composition root
installs (``FleetLaneBoot.lane_counts_probe``).

Every attention item is one line on the account's Home: a backend-authored
headline and exactly one fix, a link to where that fix lives. Only what the
owner must act on is listed -- an old bot that is flat with nothing still
claimed is Finished, not attention (hurdles H13, H34).

Locks a beat takes: the bot counts read the runner's task table only -- no
lock, no file. The attention count is the attention read: one SQLite read
transaction on its own connection for the account projection, plus short
holds of the repository's write lock for each roster query and for each
registration that can still need attention. A retired registration with no
live custody is skipped (it is the catalog's inert row, #1911), so the walk
does not grow with retired history. No lock is held across the event loop
or around a broker call; it all runs off the loop.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from app.broker.alpaca.clerk.account_authority import authority_kind_for_account
from app.broker.alpaca.clerk.account_money import holdings_text
from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ClerkStartupFailure
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import next_redrive_at_ms
from app.broker.alpaca.clerk.sqlite.account_eligibility import (
    AUTHORITY_FAILED_HEADLINE as _AUTHORITY_FAILED_HEADLINE,
)
from app.broker.alpaca.clerk.sqlite.account_eligibility import (
    AUTHORITY_RECONNECTING_HEADLINE as _AUTHORITY_RECONNECTING_HEADLINE,
)
from app.broker.alpaca.clerk.sqlite.budget_authority import BUDGETS_NOT_SWITCHED_ON
from app.broker.alpaca.clerk.sqlite.exit_resolution import REGULAR_SESSION_SALE_WAITS_FOR_OPEN
from app.broker.alpaca.clerk.sqlite.folds import position_quantity_is_nonzero
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection, ProjectedUncertainty
from app.broker.alpaca.clerk.sqlite.projections import project_uncertainties
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.scheduled_end import EndSaleWaiting, end_sales_waiting_for_open
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
from app.schemas.account_authority import AuthorityKind
from app.schemas.broker_v2_panel import (
    LaneAttentionAction,
    LaneAttentionItem,
    LaneAttentionKind,
    LaneAttentionRead,
)
from app.services.bot_end import when_words
from app.services.bot_runner import get_bot_task_registry
from app.services.broker_account_snapshot import cached_broker_account_snapshot
from app.services.broker_v2_panel.budget_deploy import LEGACY_BUDGET_DETAIL
from app.services.broker_v2_panel.sqlite_panel_source import home_roster, read_account_projection
from app.services.sqlite_clerk_compat import account_eligibility

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

    From the lane's own custody ledger, never the broker: its active episodes
    (holds, channels, out-of-sync orders and positions, exits that have not
    flattened), a bot whose lifecycle cannot be read, stopped bots still
    holding money, bots that ended uncleanly since the account was last
    checked against Alpaca, the account's own standing (a failed authority,
    or what the latest cached account observation says), a bot's end sale
    waiting for the next open, and an account that has not switched to
    budgets. A lane with no authority at all answers empty rather than
    unknown.
    """
    runtime = get_active_clerk_runtime()
    repository = None if runtime is None else runtime.sqlite_repository
    if runtime is None or repository is None:
        failure = None if runtime is None else runtime.startup_failure
        if failure is None or not failure.activation_detected:
            return LaneAttentionRead(account_id=None, items=[])
        return LaneAttentionRead(account_id=failure.account_id, items=[_failed_authority_item(failure)])
    # Read eligibility from the selected facade, exactly as the desk and
    # panel do. A repository without its policy authority cannot name a time.
    clerk = runtime.clerk
    account_items: list[LaneAttentionItem] = []
    if isinstance(clerk, SqliteAlpacaClerkFacade):
        projection = await read_account_projection(clerk)
        uncertainties = projection.uncertainties
        account_items = _account_standing_items(projection)
        world = authority_kind_for_account(clerk.account_id, account_mode=clerk.account_mode)
    else:
        uncertainties = await asyncio.to_thread(lambda: project_uncertainties(
            repository.active_uncertainties(),
            now_ms=repository.clock(),
            exits_in_progress=repository.strategies_with_active_exit,
        ))
        world = runtime.selected_account_authority_kind or "real_paper"
    items = [*account_items, *(_episode_item(uncertainty) for uncertainty in uncertainties)]
    items.extend(await asyncio.to_thread(_end_sale_items, repository))
    # One line per bot: a bot whose exit is already named above is not named
    # again as merely stopped.
    named = {item.strategy_instance_id for item in items if item.kind == "exit"}
    items.extend(await asyncio.to_thread(_bot_items, repository, world=world, already_named=named))
    if repository.budget_authority_version() < 2:
        items.append(LaneAttentionItem(
            condition_id="legacy-budget", reason_code=BUDGETS_NOT_SWITCHED_ON, kind="legacy_budget",
            severity="warning", headline=LEGACY_BUDGET_DETAIL,
            action=LaneAttentionAction(label="Open Settings", destination="settings"),
        ))
    return LaneAttentionRead(account_id=repository.account_id, items=items)


def _failed_authority_item(failure: ClerkStartupFailure) -> LaneAttentionItem:
    """An activated authority that failed to start: the account is not managed.

    Review B5: its only renderer was retired with Overview, so a failed
    custody authority left Home quiet. Recovery is an offline step the
    account's order records and recovery explain.
    """
    if failure.reconnecting:
        # Not a failure (#2582): the authority installs on its own once Alpaca
        # answers, and this line clears with it.
        return LaneAttentionItem(
            condition_id="account:authority-reconnecting", reason_code=failure.reason_code,
            kind="account", severity="warning", headline=_AUTHORITY_RECONNECTING_HEADLINE,
            action=_ORDER_RECORDS,
        )
    return LaneAttentionItem(
        condition_id=f"account:authority-failed:{failure.reason_code}", reason_code=failure.reason_code,
        kind="account", severity="blocking", headline=_AUTHORITY_FAILED_HEADLINE,
        action=_ORDER_RECORDS,
    )


def _account_standing_items(projection: ClerkProjection) -> list[LaneAttentionItem]:
    """What the account's own standing needs from the owner (review B5).

    Judged by the account's eligibility (``account_eligibility``) on the
    latest account observation already cached
    (``cached_broker_account_snapshot``) -- never a broker read of its own --
    so an account Alpaca blocked, left inactive or runs in the wrong mode is a
    line on Home and in the bell. A channel that holds entries is its own
    episode above. Nothing cached means nothing is claimed either way.
    """
    account = cached_broker_account_snapshot("alpaca")
    if account is None:
        return []
    condition = account_eligibility(projection, account)
    if condition is None:
        return []
    return [LaneAttentionItem(
        condition_id=f"account:{condition.condition_id}", reason_code=condition.condition_id, kind="account",
        severity=condition.severity, headline=condition.headline,
        action=LaneAttentionAction(label="Open Settings", destination="settings"),
    )]


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


def _end_sale_items(repository: ClerkSqliteRepository) -> list[LaneAttentionItem]:
    """A bot's end sale waiting for the next regular open: one line each until it is sent (#2607).

    The owner chose to be told (grill 2026-09-29, "Alert bell too"): the bot
    reached its end while the market was closed, so the sale it scheduled
    waits for the open. The line clears the moment the sale is sent.
    Blocking: runs off the event loop.
    """
    now_ms = repository.clock()
    opens_at_ms = next_redrive_at_ms(not_before_ms=now_ms, policy=ProgramLegPolicy.regular_only())
    return [_end_sale_item(waiting, opens_at_ms=opens_at_ms, now_ms=now_ms) for waiting in end_sales_waiting_for_open(repository)]


def _end_sale_item(waiting: EndSaleWaiting, *, opens_at_ms: int, now_ms: int) -> LaneAttentionItem:
    return LaneAttentionItem(
        condition_id=f"end-sale-waits:{waiting.strategy_instance_id}",
        reason_code=REGULAR_SESSION_SALE_WAITS_FOR_OPEN,
        kind="exit",
        severity="warning",
        strategy_instance_id=waiting.strategy_instance_id,
        symbol=waiting.symbol,
        headline=(
            f"{waiting.strategy_instance_id} reached its end while the market was closed. "
            f"Its sale of {holdings_text({waiting.symbol: waiting.quantity})} goes out at the open, "
            f"{when_words(opens_at_ms, now_ms=now_ms)}."
        ),
        action=_OPEN_BOT,
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


def _bot_items(
    repository: ClerkSqliteRepository, *, world: AuthorityKind, already_named: set[str | None],
) -> list[LaneAttentionItem]:
    """The lines Home's bots need, placed by the catalog's own groups (PRD #2560 D7).

    - A stopped bot still holding money -- the catalog's ``holding`` group,
      so a crashed bot whose run row is stuck ACTIVE is here exactly as it
      is on Home -- is one line: nothing manages it. Shares are flattened
      from its page; a working entry order with no shares yet is cancelled
      there too.
    - A bot whose lifecycle cannot be read is one line: where it sits, and
      what it holds, are unknown.
    - A flat bot with nothing claimed is never a line of its own (H13, H34).
      When some ended without a clean exit after the account was last
      checked against Alpaca, the Clerk cannot vouch that they left nothing
      behind: that is ONE account line, cleared by reconciling the account.
    Blocking: runs off the event loop.
    """
    items: list[LaneAttentionItem] = []
    unchecked: list[str] = []
    last_check_ms = repository.last_clean_account_check_ms()
    for bot in home_roster(repository, world=world):
        sid = bot.strategy_instance_id
        if bot.group is None:
            items.append(LaneAttentionItem(
                condition_id=f"lifecycle-unreadable:{sid}", reason_code="LIFECYCLE_UNREADABLE",
                kind="position_unverified", severity="warning", strategy_instance_id=sid, symbol=bot.symbol,
                # Its fix is in the app (hurdle H29): reconciling re-reads
                # what the account holds at Alpaca.
                headline=(
                    f"{sid}'s lifecycle could not be read, so what it holds is unknown. "
                    "Reconcile now to re-read the account at Alpaca."
                ),
                action=_ORDER_RECORDS,
            ))
        elif bot.group == "holding" and sid not in already_named:
            items.append(_stopped_holding_item(repository, sid))
        elif bot.group == "finished" and bot.unclean_ended_at_ms is not None and (
            last_check_ms is None or bot.unclean_ended_at_ms > last_check_ms
        ):
            unchecked.append(sid)
    if unchecked:
        items.append(LaneAttentionItem(
            condition_id="positions-unchecked", reason_code="POSITIONS_UNCHECKED_SINCE_UNCLEAN_EXIT",
            kind="out_of_sync", severity="warning",
            headline=(
                f"{len(unchecked)} bot{'s' if len(unchecked) != 1 else ''} ended without a clean exit after "
                "this account was last checked against Alpaca. Reconcile now to confirm nothing is still held."
            ),
            action=_ORDER_RECORDS,
        ))
    return items


def _stopped_holding_item(repository: ClerkSqliteRepository, sid: str) -> LaneAttentionItem:
    """A stopped bot still holding money: nothing manages it (PRD #2560 D7)."""
    held = {
        symbol: quantity
        for symbol, quantity in repository.attributed_positions_for_strategy(sid).items()
        if position_quantity_is_nonzero(quantity)
    }
    what = f"still holds {holdings_text(held)}" if held else "has an entry order still working"
    return LaneAttentionItem(
        condition_id=f"stopped-holding:{sid}", reason_code="STOPPED_STILL_HOLDING", kind="stopped_holding",
        severity="warning", strategy_instance_id=sid, symbol=min(held, default=None),
        headline=f"{sid} is stopped but {what}. No bot is managing it.",
        action=LaneAttentionAction(label="Flatten…", destination="bot") if held else _OPEN_BOT,
    )


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
    """Count this lane's bots and attention items; each count fails alone.

    A count that cannot be taken -- no bot runner, no clerk runtime, or a
    failed read -- is ``None``: unknown, never zero.
    """
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
    running = registry.running_mode_counts("alpaca")
    return running["trade"], running["dry_run"]


async def _attention_count() -> int | None:
    read = await lane_attention_read()
    # No clerk is serving an account: the bell has nothing to list, but that
    # is "not counted", never "none need attention". A failed authority names
    # its account, and its line counts.
    return None if read.account_id is None else len(read.items)
