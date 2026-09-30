"""Bot history: every bot, when it ran, how it ended, its trades and money (#2574).

One account's read (``AccountBotHistory``) is served by that account's own
Clerk; the fleet coordinator fans it out to every account and merges the
rows into one page (``FleetBotHistoryPage``). Python authors every dollar
string, every label and every outcome phrase; every instant is ``int64 ms
UTC``. An unknown figure is ``None`` with its reason, never zero, and an
account or Dry Run that could not be read is a named gap, never a missing
row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.bot_lifecycle import BotDutyOutcomeKind
from app.utils.session_anchors import MAX_TIMESTAMP_MS

#: Where a bot is now: running; stopped but still holding money; finished
#: (stopped, flat, nothing still claimed); or cleared from Home by the owner.
BotHistoryStatus = Literal["running", "holding", "finished", "cleared"]

#: The world a bot trades in. A Dry Run is its own world, whatever account
#: it sits under: simulated cash, never the account's money.
BotHistoryWorld = Literal["live", "paper", "shadow", "dry_run"]

_INSTANT = Field(ge=0, le=MAX_TIMESTAMP_MS)


class BotHistoryOrders(BaseModel):
    """Orders by the broker's last reported state.

    ``sent`` counts every order the broker reported at all; ``filled``,
    ``cancelled`` (cancelled or expired) and ``rejected`` are subsets of it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sent: int = Field(ge=0)
    filled: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    rejected: int = Field(ge=0)


class BotHistoryOutcome(BaseModel):
    """How one run ended: the durable outcome and its plain-words headline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BotDutyOutcomeKind
    reason_code: str = Field(min_length=1, max_length=128)
    headline: str
    recorded_at_ms: int = _INSTANT


class BotHistoryRun(BaseModel):
    """One run of a bot. ``run_id`` is the run's own identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    started_at_ms: int = _INSTANT
    #: ``None`` while the run runs, or when it ended without a recorded stop.
    stopped_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    running: bool
    #: ``None`` while running, or when no outcome was recorded for the run.
    outcome: BotHistoryOutcome | None
    transaction_count: int = Field(ge=0)
    orders: BotHistoryOrders


class BotHistoryBot(BaseModel):
    """One bot's line: identity, its runs, its trades and its money.

    Since the budget cutover every bot has exactly one run. An older bot the
    retired Resume ran several times lists each run, but its result and fees
    are the bot's own, across all of them (``money_scope_note`` says so).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    strategy_key: str
    strategy_label: str
    symbol: str
    account_id: str
    world: BotHistoryWorld
    #: The world worded once, e.g. "PAPER · practice money" (PRD #2560 D4).
    world_label: str
    status: BotHistoryStatus
    status_label: str
    #: The first run's start; ``None`` for a bot that never ran.
    started_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    #: The newest run's stop; ``None`` while it runs or never stopped.
    stopped_at_ms: int | None = Field(default=None, ge=0, le=MAX_TIMESTAMP_MS)
    #: How the newest run ended; ``None`` while running or when not recorded.
    outcome: BotHistoryOutcome | None
    #: Effective fills after broker corrections -- the same count as the
    #: bot's ``trade_count``.
    transaction_count: int = Field(ge=0)
    orders: BotHistoryOrders
    #: The dollars set aside at Deploy; ``None`` for a bot deployed before
    #: budgets.
    budget_usd: str | None
    #: What the bot's balance gained over its budget: realized P&L less fees.
    result_usd: str | None
    fees_usd: str | None
    #: Why the result and fees are unknown; set exactly when they are.
    money_unavailable_reason: str | None
    #: Set when the bot ran more than once: its money is not split by run.
    money_scope_note: str | None
    #: Newest first.
    runs: tuple[BotHistoryRun, ...]
    #: Why the bot's own page cannot open from its account's workspace;
    #: ``None`` when it can. A bot of the account's other world is kept in a
    #: database that workspace does not read, so only History shows it.
    page_unavailable_reason: str | None


class BotHistoryGap(BaseModel):
    """Something that could not be read, named -- never a silently missing row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The Dry Run whose own records could not be read, or ``None`` when the
    #: gap is the account's own records.
    strategy_instance_id: str | None
    reason: str


class AccountBotHistory(BaseModel):
    """Every bot one account's Clerk holds, with its Dry Runs (#2574)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    observed_at_ms: int = _INSTANT
    bots: tuple[BotHistoryBot, ...]
    gaps: tuple[BotHistoryGap, ...]


class FleetBotHistoryRow(BotHistoryBot):
    """One bot of the all-accounts list, naming the account's lane."""

    broker: str
    clerk_id: str


class FleetBotHistoryGap(BaseModel):
    """One account (or one of its Dry Runs) the all-accounts list could not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker: str
    clerk_id: str
    account_id: str | None
    strategy_instance_id: str | None
    reason: str
    #: Why the account's read failed, as the fleet names it (e.g.
    #: ``clerk_unreachable``); ``None`` for a gap the account itself named.
    reason_code: str | None


class FleetBotHistoryPage(BaseModel):
    """One page of every bot across every account, newest first.

    ``total`` counts the rows the filters match; ``symbols`` lists every
    symbol any read row trades, so the symbol filter offers only real ones.
    ``gaps`` always names every account that could not be read, whatever the
    filters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at_ms: int = _INSTANT
    rows: tuple[FleetBotHistoryRow, ...]
    gaps: tuple[FleetBotHistoryGap, ...]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    symbols: tuple[str, ...]
