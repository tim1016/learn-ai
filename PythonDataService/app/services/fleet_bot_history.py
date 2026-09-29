"""The all-accounts bot history: every account's answer, merged into one page (#2574).

The fleet coordinator reads each account's ``bot_history_read`` through the
lane router (``aggregate_lane_reads_async``: one lane's failure is that
lane's own ``ok: False``) and this module merges the answers. Merging is a
list concatenation with provenance -- every row keeps the lane and account
it came from -- and never combines a value across accounts (ADR 0062
Decision 1): no row's money is added to another's.

What could not be read is named, never dropped: a lane with no confirmed
account, a lane whose read failed or answered something unreadable, and
each Dry Run or world a lane itself could not read.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from app.schemas.bot_history import (
    AccountBotHistory,
    BotHistoryStatus,
    BotHistoryWorld,
    FleetBotHistoryGap,
    FleetBotHistoryPage,
    FleetBotHistoryRow,
)

#: A lane that serves no account yet has nothing to read (FR-096).
NO_ACCOUNT_REASON_CODE = "no_confirmed_account"
#: A lane answered, but not with a bot history this build can read.
UNREADABLE_ANSWER_REASON_CODE = "unreadable_answer"

_NO_ACCOUNT_COPY = "This account is not set up yet, so it has no bots to list."
_LANE_FAILED_COPY = "This account's bots could not be read right now. Refresh to try again."


@dataclass(frozen=True)
class BotHistoryFilters:
    """What the owner narrowed the list to; ``None`` is "any"."""

    status: BotHistoryStatus | None = None
    world: BotHistoryWorld | None = None
    symbol: str | None = None

    def admits(self, row: FleetBotHistoryRow) -> bool:
        return (
            (self.status is None or row.status == self.status)
            and (self.world is None or row.world == self.world)
            and (self.symbol is None or row.symbol == self.symbol)
        )


def account_less_gap(broker: str, clerk_id: str) -> FleetBotHistoryGap:
    """A lane with no confirmed account: named, so the list never looks complete without it."""
    return FleetBotHistoryGap(
        broker=broker, clerk_id=clerk_id, account_id=None, strategy_instance_id=None,
        reason=_NO_ACCOUNT_COPY, reason_code=NO_ACCOUNT_REASON_CODE,
    )


def merge_bot_history(
    aggregate: Mapping[str, object],
    *,
    accounts: Mapping[str, str],
    extra_gaps: Iterable[FleetBotHistoryGap],
    filters: BotHistoryFilters,
    page: int,
    page_size: int,
) -> FleetBotHistoryPage:
    """One page of every lane's bots, newest first, with every gap named.

    ``aggregate`` is ``aggregate_lane_reads_async``'s envelope over each
    lane's ``AccountBotHistory``; ``accounts`` maps each read lane's
    ``clerk_id`` to the account it was asked for. ``symbols`` lists every
    symbol a read row trades, before the filters, so the symbol filter only
    ever offers a real one.
    """
    rows: list[FleetBotHistoryRow] = []
    gaps: list[FleetBotHistoryGap] = list(extra_gaps)
    for lane in _lanes(aggregate):
        broker, clerk_id = str(lane["broker"]), str(lane["clerk_id"])
        account_id = accounts.get(clerk_id)
        history, failure = _history(lane)
        if history is None:
            gaps.append(FleetBotHistoryGap(
                broker=broker, clerk_id=clerk_id, account_id=account_id, strategy_instance_id=None,
                reason=_LANE_FAILED_COPY, reason_code=failure,
            ))
            continue
        rows += (
            FleetBotHistoryRow(**bot.model_dump(), broker=broker, clerk_id=clerk_id)
            for bot in history.bots
        )
        gaps += (
            FleetBotHistoryGap(
                broker=broker, clerk_id=clerk_id, account_id=history.account_id,
                strategy_instance_id=gap.strategy_instance_id, reason=gap.reason, reason_code=None,
            )
            for gap in history.gaps
        )
    rows.sort(key=lambda row: (-(row.started_at_ms or 0), row.clerk_id, row.strategy_instance_id))
    matching = [row for row in rows if filters.admits(row)]
    start = (page - 1) * page_size
    observed_at_ms = aggregate["observed_at_ms"]
    if not isinstance(observed_at_ms, int):
        raise TypeError("the lane aggregate carries no observation instant")
    return FleetBotHistoryPage(
        observed_at_ms=observed_at_ms,
        rows=tuple(matching[start:start + page_size]),
        gaps=tuple(gaps),
        total=len(matching),
        page=page,
        page_size=page_size,
        symbols=tuple(sorted({row.symbol for row in rows})),
    )


def _lanes(aggregate: Mapping[str, object]) -> Sequence[Mapping[str, object]]:
    lanes = aggregate.get("lanes", ())
    return [lane for lane in lanes if isinstance(lane, Mapping)] if isinstance(lanes, list) else []


def _history(lane: Mapping[str, object]) -> tuple[AccountBotHistory | None, str | None]:
    """The lane's own history, or why it has none."""
    if lane.get("ok") is not True:
        return None, str(lane.get("error_reason") or "lane_read_failed")
    try:
        return AccountBotHistory.model_validate(lane.get("value")), None
    except ValidationError:
        return None, UNREADABLE_ANSWER_REASON_CODE
