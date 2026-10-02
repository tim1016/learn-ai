"""What the bot page leads with, written from the panel and its run's facts (PRD #2794).

Pure functions over an adapted ``BotPanelView`` and the run's facts:

- the bot's own status (R9): an account condition -- market data down, an old
  unexplained order -- never turns the bot red; it shows in the account
  health group and on the actions it blocks;
- the one-line run summary (R1), from a versioned template, so the same facts
  always give the same line;
- the toolbar's actions (R2, R6), each available, blocked with its reason, or
  not needed -- today both of the last two read as a red BLOCKED;
- health in two groups (R9): what happened during this run, and the account
  right now.

Times in prose are ET, as every backend-written line is (``app.utils.et_words``).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.sqlite.recovery_policy import NOTHING_TO_DO_REASON_CODES
from app.schemas.bot_lifecycle import UNCLEAN_DUTY_OUTCOMES
from app.schemas.bot_page import (
    BotHealthGroupsView,
    BotOwnStatusView,
    BotPageView,
    HealthLineState,
    HealthLineView,
    HeldPositionFact,
    RunEnding,
    RunSummaryFacts,
    RunSummaryView,
    ToolbarActionId,
    ToolbarActionView,
    ToolbarAvailability,
    ToolbarGroup,
    ToolbarTone,
)
from app.schemas.broker_v2_panel import BotPanelView, ChannelHealthView, PanelAction
from app.utils.et_words import et_clock_words
from app.utils.session_anchors import et_date_at_ms

SUMMARY_TEMPLATE_VERSION = 1


@dataclass(frozen=True)
class RunFacts:
    """The latest run's facts the panel does not carry."""

    run_id: str | None
    started_at_ms: int | None
    # When the run's terminal outcome was recorded; ``None`` while it runs.
    ended_at_ms: int | None
    decision_count: int
    # Orders whose fills landed in the run.
    trade_count: int
    orders_sent: int
    # The bot's budget, and what its Stop released, in cents (``None`` when unrecorded).
    committed_cents: int | None
    released_cents: int | None
    # A sale the Clerk is still working for this bot (#2504).
    exit_in_progress: bool


# ── Status ───────────────────────────────────────────────────────────────────


def bot_own_status(
    panel: BotPanelView, *, bot_owns_custody_problem: bool, execution_coverage_complete: bool
) -> BotOwnStatusView:
    """The bot's own status: needs attention, running, ended holding or finished.

    Only this bot's own trouble asks for attention -- an unclean end to its
    run, fills it cannot yet prove, a hold or uncertainty on its own custody --
    the same trouble that flags its row on Home.
    """
    outcome = panel.health.duty_outcome
    if outcome is not None and outcome.kind in UNCLEAN_DUTY_OUTCOMES:
        return BotOwnStatusView(state="needs_attention", label="Needs attention", reason=outcome.explanation)
    if not execution_coverage_complete:
        return BotOwnStatusView(
            state="needs_attention",
            label="Needs attention",
            reason="This bot's fills are not all proven yet.",
        )
    if bot_owns_custody_problem:
        return BotOwnStatusView(
            state="needs_attention",
            label="Needs attention",
            reason="This bot's own custody has a hold or an open question.",
        )
    if panel.health.running:
        return BotOwnStatusView(state="running", label="Running", reason=None)
    if panel.exposure:
        return BotOwnStatusView(state="ended_holding", label="Ended holding", reason=None)
    return BotOwnStatusView(state="finished", label="Finished", reason=None)


# ── Summary ──────────────────────────────────────────────────────────────────


_ENDING_BY_OUTCOME: dict[str, RunEnding] = {
    "STOPPED": "stopped",
    "HALTED": "halted",
    "CRASHED": "crashed",
    "FAILED_LAUNCH": "failed_to_start",
    "EXITED_UNVERIFIED": "exited_unverified",
    "RETIRED": "retired",
}

_ENDING_WORDS: dict[RunEnding, str] = {
    "on_schedule": "ended on schedule",
    "stopped": "stopped",
    "halted": "halted",
    "crashed": "crashed",
    "failed_to_start": "failed to start",
    "exited_unverified": "ended without verified custody",
    "retired": "retired",
}


def run_ending(panel: BotPanelView, run: RunFacts) -> RunEnding:
    """How the latest run ended, from its terminal outcome and whether its end was carried out."""
    if run.started_at_ms is None:
        return "not_started"
    if panel.health.running:
        return "running"
    outcome = panel.health.duty_outcome
    if outcome is None or outcome.run_id not in (None, run.run_id):
        return "stopped"
    if outcome.kind == "STOPPED" and panel.end is not None and panel.end.status == "ended":
        return "on_schedule"
    return _ENDING_BY_OUTCOME.get(outcome.kind, "stopped")


def _quantity_words(quantity: float) -> str:
    """A share count as the owner reads it: ``1``, ``0.5``, never ``1.0``."""
    text = format(Decimal(repr(abs(quantity))).normalize(), "f")
    return f"-{text}" if quantity < 0 else text


def run_summary_facts(panel: BotPanelView, run: RunFacts, *, now_ms: int) -> RunSummaryFacts:
    """The facts the summary line is written from."""
    ending = run_ending(panel, run)
    stopped = ending not in ("not_started", "running")
    return RunSummaryFacts(
        run_id=run.run_id,
        started_at_ms=run.started_at_ms,
        ended_at_ms=run.ended_at_ms if stopped else None,
        scheduled_end_at_ms=(
            panel.end.end_at_ms if panel.end is not None and panel.end.status in ("scheduled", "ending") else None
        ),
        ending=ending,
        decision_count=run.decision_count,
        trade_count=run.trade_count,
        set_aside_usd=(
            dollars(run.committed_cents) if ending == "running" and run.committed_cents is not None else None
        ),
        returned_usd=dollars(run.released_cents) if stopped and run.released_cents is not None else None,
        held=[
            HeldPositionFact(symbol=symbol, quantity=_quantity_words(quantity))
            for symbol, quantity in sorted(panel.exposure.items())
        ],
        exit_queued=run.exit_in_progress or (panel.end is not None and panel.end.status == "ending"),
        current_year=et_date_at_ms(now_ms).year,
    )


def _count_words(count: int, noun: str) -> str:
    if count == 0:
        return f"no {noun}s"
    return f"1 {noun}" if count == 1 else f"{count} {noun}s"


def _day_words(instant_ms: int, current_year: int) -> str:
    day = et_date_at_ms(instant_ms)
    year = "" if day.year == current_year else f" {day.year}"
    return f"{day:%a %b} {day.day}{year}"


def _span_words(facts: RunSummaryFacts) -> str:
    """``Wed Sep 30, 14:30–15:59 ET``, naming the end's day only when it differs."""
    assert facts.started_at_ms is not None
    start = f"{_day_words(facts.started_at_ms, facts.current_year)}, {et_clock_words(facts.started_at_ms)}"
    if facts.ended_at_ms is None:
        return f"{start} ET"
    if et_date_at_ms(facts.ended_at_ms) == et_date_at_ms(facts.started_at_ms):
        return f"{start}–{et_clock_words(facts.ended_at_ms)} ET"
    end = f"{_day_words(facts.ended_at_ms, facts.current_year)}, {et_clock_words(facts.ended_at_ms)}"
    return f"{start} ET – {end} ET"


def _money_words(facts: RunSummaryFacts) -> str | None:
    if facts.held:
        return "holds " + ", ".join(f"{held.quantity} {held.symbol}" for held in facts.held)
    if facts.set_aside_usd is not None:
        return f"${facts.set_aside_usd} set aside"
    if facts.returned_usd is not None:
        return f"${facts.returned_usd} back to the account"
    return None


def run_summary_text(facts: RunSummaryFacts) -> str:
    """Template v1: the run in one line, a pure function of its facts.

    "Ran Wed Sep 30, 14:30–15:59 ET · ended on schedule · 5 decisions, no
    trades · $800.00 back to the account."
    """
    if facts.ending == "not_started" or facts.started_at_ms is None:
        return "Not started yet."
    activity = f"{_count_words(facts.decision_count, 'decision')}, {_count_words(facts.trade_count, 'trade')}"
    if facts.ending == "running":
        parts = [f"Running since {_span_words(facts)}"]
        if facts.scheduled_end_at_ms is not None:
            parts.append(f"ends {et_clock_words(facts.scheduled_end_at_ms)} ET")
    else:
        parts = [f"Ran {_span_words(facts)}", _ENDING_WORDS[facts.ending]]
    parts.append(activity)
    money = _money_words(facts)
    if money is not None:
        parts.append(money)
    if facts.exit_queued:
        parts.append("a sale is queued")
    return " · ".join(parts) + "."


def run_summary(panel: BotPanelView, run: RunFacts, *, now_ms: int) -> RunSummaryView:
    facts = run_summary_facts(panel, run, now_ms=now_ms)
    return RunSummaryView(text=run_summary_text(facts), template_version=SUMMARY_TEMPLATE_VERSION, facts=facts)


# ── Toolbar ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CustodyEntry:
    label: str
    group: ToolbarGroup
    tone: ToolbarTone
    # What it does, shown while it is available.
    does: str


# The custody actions in the order the toolbar shows them, with their plain
# names (R6). Sell is ``prepare_safe_flatten``: its ticket's second step is
# ``execute_safe_flatten``, so execute has no button of its own.
_CUSTODY_ENTRIES: dict[str, _CustodyEntry] = {
    "stop_bot_decisions": _CustodyEntry("Stop", "bot", "danger", "Stop this bot's decisions now."),
    "prepare_safe_flatten": _CustodyEntry(
        "Sell", "bot", "danger", "Review a plan to sell what this bot holds, then send it."
    ),
    "reconcile_now": _CustodyEntry("Check against Alpaca", "fix", "neutral", "Compare this account with Alpaca now."),
    "cancel_verified_working_orders": _CustodyEntry(
        "Cancel open orders", "fix", "danger", "Cancel this bot's open orders at Alpaca."
    ),
    "discharge_attributed_residue": _CustodyEntry(
        "Write off missing shares", "fix", "danger", "Write off shares the Clerk holds but Alpaca does not."
    ),
    "recover_exact_execution_evidence": _CustodyEntry(
        "Recover a missing fill", "fix", "warning", "Read a missing fill back from Alpaca's records."
    ),
    "resolve_execution_coverage": _CustodyEntry(
        "Use the exact fill", "fix", "warning", "Settle a fill the Clerk recorded two ways on the exact one."
    ),
    "open_custody_timeline": _CustodyEntry(
        "Custody timeline", "inspect", "neutral", "Open this bot's custody timeline."
    ),
}


def _sell_label(panel: BotPanelView) -> str:
    if len(panel.exposure) != 1:
        return "Sell"
    ((symbol, quantity),) = panel.exposure.items()
    return f"Sell {_quantity_words(abs(quantity))} {symbol}"


def _custody_entry(action: PanelAction, panel: BotPanelView) -> ToolbarActionView:
    entry = _CUSTODY_ENTRIES[action.action_id]
    label = _sell_label(panel) if action.action_id == "prepare_safe_flatten" else entry.label
    availability: ToolbarAvailability
    if action.enabled:
        availability, reason = "available", entry.does
    else:
        blocker = action.blockers[0] if action.blockers else None
        reason = blocker.headline if blocker is not None else action.explanation
        nothing_to_do = blocker is not None and blocker.condition.id in NOTHING_TO_DO_REASON_CODES
        availability = "not_needed" if nothing_to_do else "blocked"
    return ToolbarActionView(
        action_id=action.action_id,  # type: ignore[arg-type]  # a custody id is a toolbar id
        label=label,
        group=entry.group,
        availability=availability,
        reason=reason,
        tone=entry.tone,
    )


def _entry(
    action_id: ToolbarActionId,
    label: str,
    group: ToolbarGroup,
    availability: ToolbarAvailability,
    reason: str,
    tone: ToolbarTone = "neutral",
) -> ToolbarActionView:
    return ToolbarActionView(
        action_id=action_id, label=label, group=group, availability=availability, reason=reason, tone=tone
    )


def _change_end_entry(panel: BotPanelView) -> ToolbarActionView:
    end = panel.end
    if end is not None and end.editable:
        return _entry("change_end", "Change end", "bot", "available", "Change when this bot ends and what it does then.")
    if end is not None and end.status == "ending":
        return _entry("change_end", "Change end", "bot", "blocked", "This bot's end has come; the Clerk is carrying it out.")
    return _entry("change_end", "Change end", "bot", "not_needed", "This bot has stopped, so it has no end to change.")


def _deploy_again_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.health.running:
        return _entry("deploy_again", "Deploy again", "bot", "not_needed", "This bot is still running.")
    return _entry("deploy_again", "Deploy again", "bot", "available", "Deploy a new bot with these settings.")


def _clear_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.status == "cleared":
        return _entry("archive", "Clear from Home", "bot", "not_needed", "This bot is already cleared from Home.")
    archive = next((action for action in panel.actions if action.action_id == "archive"), None)
    if archive is None:
        return _entry("archive", "Clear from Home", "bot", "not_needed", "A running bot stays on Home.")
    if archive.enabled:
        return _entry("archive", "Clear from Home", "bot", "available", "Take this finished bot off Home.")
    reason = archive.blockers[0].headline if archive.blockers else archive.explanation
    return _entry("archive", "Clear from Home", "bot", "blocked", reason)


def _manual_order_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.mode == "dry_run":
        return _entry(
            "manual_order", "Manual order", "bot", "not_needed", "A Dry Run trades simulated cash, so it has no orders to place."
        )
    if panel.status == "cleared":
        return _entry("manual_order", "Manual order", "bot", "not_needed", "A cleared bot's page is read-only.")
    return _entry("manual_order", "Manual order", "bot", "available", f"Place an order for {panel.symbol} on this account.")


def _build_proof_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.program_build.state == "NOT_APPLICABLE":
        return _entry("build_proof", "Build proof", "inspect", "not_needed", "This strategy has no sealed program to prove.")
    return _entry("build_proof", "Build proof", "inspect", "available", "Show this run's build proof and its hashes.")


def toolbar_actions(panel: BotPanelView) -> list[ToolbarActionView]:
    """Every action the bot page offers, in toolbar order, with one marked primary.

    The primary is the backend's ``primary_action`` (ADR 0026's Button Rule);
    with none, a finished bot's primary is Deploy again (#2794 story 24).
    """
    custody = {action.action_id: action for action in panel.actions if action.action_id in _CUSTODY_ENTRIES}

    def custody_entry(action_id: str) -> list[ToolbarActionView]:
        action = custody.get(action_id)
        return [] if action is None else [_custody_entry(action, panel)]

    entries = [
        *custody_entry("stop_bot_decisions"),
        *custody_entry("prepare_safe_flatten"),
        _change_end_entry(panel),
        _deploy_again_entry(panel),
        _clear_entry(panel),
        _manual_order_entry(panel),
        *custody_entry("reconcile_now"),
        *custody_entry("cancel_verified_working_orders"),
        *custody_entry("discharge_attributed_residue"),
        *custody_entry("recover_exact_execution_evidence"),
        *custody_entry("resolve_execution_coverage"),
        *custody_entry("open_custody_timeline"),
        _build_proof_entry(panel),
    ]
    primary = panel.primary_action
    if primary == "execute_safe_flatten":
        primary = "prepare_safe_flatten"
    if primary is None and not panel.health.running and not panel.exposure:
        primary = "deploy_again"
    return [
        entry.model_copy(update={"primary": True})
        if entry.action_id == primary and entry.availability == "available"
        else entry
        for entry in entries
    ]


# ── Health ───────────────────────────────────────────────────────────────────


_FEED_STATES: dict[str, HealthLineState] = {
    "continuous": "ok",
    "recovered": "ok",
    "interrupted": "attention",
    "compromised": "problem",
    "not_recorded": "idle",
}

_CHANNEL_STATES: dict[str, HealthLineState] = {"healthy": "ok", "unhealthy": "problem", "unknown": "attention"}

_NO_EFFECT_ON_STOPPED = "No effect on this stopped bot."


def _run_lines(panel: BotPanelView, run: RunFacts) -> list[HealthLineView]:
    feed = panel.feed_continuity
    lines = [
        HealthLineView(
            key="feed",
            label=f"{feed.provider_label} feed",
            state=_FEED_STATES[feed.state],
            value=feed.state_label,
            note=feed.explanation,
        )
    ]
    if panel.health.running and panel.health.decision_stale:
        lines.append(
            HealthLineView(
                key="decisions",
                label="Decisions",
                state="attention",
                value=_count_words(run.decision_count, "decision"),
                note="No decision has come on time.",
                at_ms=panel.health.last_decision_at_ms,
            )
        )
    else:
        lines.append(
            HealthLineView(
                key="decisions",
                label="Decisions",
                state="ok",
                value=_count_words(run.decision_count, "decision"),
                at_ms=panel.health.last_decision_at_ms,
            )
        )
    lines.append(
        HealthLineView(key="orders", label="Orders sent", state="ok", value=_count_words(run.orders_sent, "order"))
    )
    return lines


def _channel_line(channel: ChannelHealthView, *, running: bool) -> HealthLineView:
    state = _CHANNEL_STATES[channel.state]
    return HealthLineView(
        key=f"channel:{channel.stream}",
        label=channel.name,
        state=state,
        value=channel.label,
        note=channel.explanation if running or state == "ok" else _NO_EFFECT_ON_STOPPED,
        at_ms=channel.observed_at_ms,
    )


def _account_lines(panel: BotPanelView) -> list[HealthLineView]:
    clerk = panel.clerk
    running = panel.health.running
    lines = [_channel_line(channel, running=running) for channel in clerk.channels]
    if clerk.freeze_active:
        lines.append(
            HealthLineView(
                key="holds", label="Holds", state="problem", value=clerk.freeze_label,
                note=clerk.freeze_explanation, at_ms=clerk.freeze_observed_at_ms,
            )
        )
    elif clerk.hold_active:
        lines.append(
            HealthLineView(
                key="holds", label="Holds", state="problem" if running else "attention",
                value=clerk.hold_reason_label,
                note=clerk.hold_reason_explanation if running else _NO_EFFECT_ON_STOPPED,
                at_ms=clerk.hold_since_ms,
            )
        )
    else:
        lines.append(HealthLineView(key="holds", label="Holds", state="ok", value="None"))
    verdict = clerk.reconciliation_verdict
    lines.append(
        HealthLineView(
            key="reconciliation",
            label="Last check against Alpaca",
            state="idle" if verdict is None else ("ok" if verdict == "clean" else "attention"),
            value=clerk.reconciliation_verdict_label or "Not checked yet",
            at_ms=clerk.last_sweep_at_ms,
        )
    )
    return lines


def health_groups(panel: BotPanelView, run: RunFacts) -> BotHealthGroupsView:
    """Health split in two: this run's own facts, and the account's facts right now."""
    return BotHealthGroupsView(run=_run_lines(panel, run), account=_account_lines(panel))


def bot_page_view(
    panel: BotPanelView,
    run: RunFacts,
    *,
    bot_owns_custody_problem: bool,
    execution_coverage_complete: bool,
    now_ms: int,
) -> BotPageView:
    """Everything the bot page leads with, from the adapted panel and its run's facts."""
    return BotPageView(
        status=bot_own_status(
            panel,
            bot_owns_custody_problem=bot_owns_custody_problem,
            execution_coverage_complete=execution_coverage_complete,
        ),
        summary=run_summary(panel, run, now_ms=now_ms),
        toolbar=toolbar_actions(panel),
        health=health_groups(panel, run),
    )


__all__ = [
    "SUMMARY_TEMPLATE_VERSION",
    "RunFacts",
    "bot_own_status",
    "bot_page_view",
    "health_groups",
    "run_ending",
    "run_summary",
    "run_summary_facts",
    "run_summary_text",
    "toolbar_actions",
]
