"""What the bot page leads with, written by the backend (PRD #2794 R1, R2, R8, R9).

The page shows these as given (ADR 0013, ADR 0014): one status the bot owns, a
one-line summary of its run, the toolbar's actions with their availability,
and health split into the run's own facts and the account's facts right now.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.broker.alpaca.clerk.models import EpochMs

BotOwnState = Literal["running", "finished", "ended_holding", "needs_attention"]
"""The bot's own status: an account condition never sets it (R9)."""


class BotOwnStatusView(BaseModel):
    """One status the bot owns, its label, and why when it needs attention."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: BotOwnState
    label: str
    reason: str | None


RunEnding = Literal[
    "not_started",
    "running",
    "on_schedule",
    "stopped",
    "halted",
    "crashed",
    "failed_to_start",
    "exited_unverified",
    "retired",
]
"""How the bot's latest run ended, or that it has none yet or is still running."""


class HeldPositionFact(BaseModel):
    """Shares a bot still holds: the symbol, and the quantity as the owner reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: str


class RunSummaryFacts(BaseModel):
    """The run facts the summary line is written from; the same facts give the same line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str | None
    # When the run started; the page dates the run by it in ``date-et`` mode.
    started_at_ms: EpochMs | None = None
    ended_at_ms: EpochMs | None = None
    scheduled_end_at_ms: EpochMs | None = None
    ending: RunEnding
    decision_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    # A running bot's budget, in dollars.
    set_aside_usd: str | None
    # What a stopped bot's Stop released, in dollars, as the Stop recorded it.
    returned_usd: str | None
    held: list[HeldPositionFact]
    exit_queued: bool
    # The ET year the line was read in: a run in another year names its year.
    current_year: int


class RunSummaryView(BaseModel):
    """The summary line, the facts it was written from and the template that wrote it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    template_version: int
    facts: RunSummaryFacts


ToolbarActionId = Literal[
    "stop_bot_decisions",
    "prepare_safe_flatten",
    "change_end",
    "deploy_again",
    "archive",
    "manual_order",
    "reconcile_now",
    "cancel_verified_working_orders",
    "discharge_attributed_residue",
    "recover_exact_execution_evidence",
    "resolve_execution_coverage",
    "open_custody_timeline",
    "build_proof",
]
"""Every action the bot page offers, by its system name.

The custody ones are ``PanelAction`` ids; the page runs them through
``BotPanelView.actions``. Sell is ``prepare_safe_flatten``: one button that
opens the two-step ticket, whose second step is ``execute_safe_flatten``.
"""

ToolbarGroup = Literal["bot", "fix", "inspect"]
ToolbarAvailability = Literal["available", "blocked", "not_needed"]
ToolbarTone = Literal["danger", "warning", "neutral"]


class ToolbarActionView(BaseModel):
    """One action on the bot page: offered now, blocked with its reason, or not needed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: ToolbarActionId
    label: str
    group: ToolbarGroup
    availability: ToolbarAvailability
    # What it does when available; why not, otherwise.
    reason: str
    tone: ToolbarTone
    # The one action the page fills and labels.
    primary: bool = False


HealthLineState = Literal["ok", "attention", "problem", "idle"]


class HealthLineView(BaseModel):
    """One health fact: what it is, its state, a short value and the backend's note."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    label: str
    state: HealthLineState
    value: str
    note: str | None = None
    # The instant the value was observed, rendered by the page's timestamp display.
    at_ms: EpochMs | None = None


class BotHealthGroupsView(BaseModel):
    """Health in two groups: what happened during this run, and the account right now."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: list[HealthLineView]
    account: list[HealthLineView]


class BotPageView(BaseModel):
    """What the bot page leads with: status, summary, toolbar and health."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: BotOwnStatusView
    summary: RunSummaryView
    toolbar: list[ToolbarActionView]
    health: BotHealthGroupsView


__all__ = [
    "BotHealthGroupsView",
    "BotOwnState",
    "BotOwnStatusView",
    "BotPageView",
    "HealthLineState",
    "HealthLineView",
    "HeldPositionFact",
    "RunEnding",
    "RunSummaryFacts",
    "RunSummaryView",
    "ToolbarActionId",
    "ToolbarActionView",
    "ToolbarAvailability",
    "ToolbarGroup",
    "ToolbarTone",
]
