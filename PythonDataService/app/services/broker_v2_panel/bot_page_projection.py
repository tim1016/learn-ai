"""What the bot page leads with, written from the panel and its run's facts (PRD #2794).

Pure functions over an adapted ``BotPanelView`` and the run's facts:

- the bot's own status (R9): an account condition -- market data down, an old
  unexplained order -- never turns the bot red; it shows in the account
  health group and on the actions it blocks;
- the one-line run summary (R1), from a versioned template, so the same facts
  always give the same line;
- the toolbar's actions (R2, R6), each available, blocked with its reason, or
  not needed -- today both of the last two read as a red BLOCKED. Whether an
  action is needed at all is the recovery policy's word
  (``PanelAction.needed``), decided where it writes the reason;
- health in two groups (R9): what happened during this run, and the account
  right now.

Times in prose are ET, as every backend-written line is (``app.utils.et_words``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.broker.alpaca.clerk.account_money import quantity_text
from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.sqlite.economic_projection_models import RunActivity
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    SETTLE_UNKNOWN_EXIT_ORDER_LABEL,
    settle_unavailable_words,
)
from app.broker.alpaca.clerk.synthesized_orders import SynthesizedAnchor
from app.broker.contract.models import OrderSide
from app.schemas.action_plan import order_symbol
from app.schemas.bot_lifecycle import BotDutyOutcomeKind
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
from app.schemas.broker_bots import BotRunView
from app.schemas.broker_v2_panel import (
    BotPanelView,
    ChannelHealthView,
    PanelAction,
    RunFillView,
    SimulatedFillView,
)
from app.services.broker_v2_panel.catalog_projection_service import duty_outcome_needs_attention
from app.services.broker_v2_panel.chart_projection_service import fill_to_marker
from app.services.broker_v2_panel.feed_continuity_projection import duration_label
from app.services.source_bar_ledger import RECOVERY_QUOTE_PROVIDER, RUN_END_CLOSE_PROVIDER
from app.utils.et_words import et_clock_words, et_day_words
from app.utils.session_anchors import et_date_at_ms

SUMMARY_TEMPLATE_VERSION = 2
# A run the Clerk admitted whose launch record cannot be read.
_UNREADABLE_RUN = "This bot's latest run record could not be read."
# The bot page lists a run's newest fills; a longer run still counts every trade.
RUN_FILL_LIMIT = 50


@dataclass(frozen=True)
class RunFacts:
    """The latest run's facts the panel does not carry."""

    run_id: str | None
    started_at_ms: int | None
    # When the run's own terminal outcome was recorded, and its kind; ``None`` while it runs.
    ended_at_ms: int | None
    ending_kind: BotDutyOutcomeKind | None
    # The Clerk admitted the run, but its launch record cannot be read.
    unreadable: bool
    decision_count: int
    decision_count_is_floor: bool
    # The run's effective fills, as Bot history counts its transactions.
    trade_count: int
    orders_sent: int
    # The bot's budget, and what its Stop released, in cents (``None`` when unrecorded).
    committed_cents: int | None
    released_cents: int | None
    # A sale the Clerk is still working for this bot (#2504).
    exit_in_progress: bool
    # That sale's order was sent and the Clerk could not confirm it: nothing more is sent (#2854).
    exit_unconfirmed: bool = False


def run_facts(
    run: BotRunView | None,
    *,
    unreadable: bool = False,
    run_fills: Sequence[FillRecord],
    activity: RunActivity | None,
    budget: Mapping[str, int | None] | None,
    exit_in_progress: bool,
    exit_unconfirmed: bool = False,
) -> RunFacts:
    """The run's facts from its record, its fills, its activity and the bot's budget row."""
    terminal = None if run is None else run.terminal_outcome
    return RunFacts(
        run_id=None if run is None else run.run_id,
        started_at_ms=None if run is None else run.started_at_ms,
        ended_at_ms=None if terminal is None else terminal.recorded_at_ms,
        ending_kind=None if terminal is None else terminal.kind,
        unreadable=unreadable,
        decision_count=0 if activity is None else activity.decisions,
        decision_count_is_floor=activity is not None and activity.decisions_is_floor,
        trade_count=len(run_fills),
        orders_sent=0 if activity is None else activity.orders_sent,
        committed_cents=None if budget is None else budget["committed_cents"],
        released_cents=None if budget is None else budget["released_cents"],
        exit_in_progress=exit_in_progress,
        exit_unconfirmed=exit_unconfirmed,
    )


# ── Status ───────────────────────────────────────────────────────────────────


def bot_own_status(
    panel: BotPanelView,
    *,
    bot_owns_custody_problem: bool,
    execution_coverage_complete: bool,
    run_unreadable: bool = False,
) -> BotOwnStatusView:
    """The bot's own status: needs attention, running, ended holding or finished.

    Only this bot's own trouble asks for attention: an unclean end to its run
    (the test its Home row uses too), fills it cannot yet prove, a hold or
    uncertainty on its own custody. Home's row also flags account-wide trouble;
    this page shows that in the account health group instead.
    """
    outcome = panel.health.duty_outcome
    if outcome is not None and duty_outcome_needs_attention(outcome.kind):
        return BotOwnStatusView(state="needs_attention", label="Needs attention", reason=outcome.explanation)
    if run_unreadable:
        return BotOwnStatusView(
            state="needs_attention",
            label="Needs attention",
            reason=_UNREADABLE_RUN,
        )
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


_ENDING_BY_OUTCOME: dict[BotDutyOutcomeKind, RunEnding] = {
    "STOPPED": "stopped",
    "HALTED": "halted",
    "CRASHED": "crashed",
    "FAILED_LAUNCH": "failed_to_start",
    "EXITED_UNVERIFIED": "exited_unverified",
    "RETIRED": "retired",
}

_ENDING_WORDS: dict[RunEnding, str] = {
    "ended": "ended",
    "on_schedule": "ended on schedule",
    "stopped": "stopped",
    "halted": "halted",
    "crashed": "crashed",
    "failed_to_start": "failed to start",
    "exited_unverified": "ended without verified custody",
    "retired": "retired",
}


def run_ending(panel: BotPanelView, run: RunFacts) -> RunEnding:
    """How the latest run ended, from its own terminal outcome and whether its end was carried out."""
    if run.unreadable:
        return "unreadable"
    if run.started_at_ms is None:
        return "not_started"
    if panel.health.running:
        return "running"
    if run.ending_kind is None:
        return "ended"
    if run.ending_kind == "STOPPED" and panel.end is not None and panel.end.status == "ended":
        return "on_schedule"
    return _ENDING_BY_OUTCOME[run.ending_kind]


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
        decision_count_is_floor=run.decision_count_is_floor,
        trade_count=run.trade_count,
        set_aside_usd=(
            dollars(run.committed_cents) if ending == "running" and run.committed_cents is not None else None
        ),
        returned_usd=dollars(run.released_cents) if stopped and run.released_cents is not None else None,
        held=[
            HeldPositionFact(symbol=symbol, quantity=quantity_text(quantity))
            for symbol, quantity in sorted(panel.exposure.items())
        ],
        # An exit whose order could not be confirmed is held, not queued: nothing more goes out for it.
        # An end that waits for the operator queues no sale (#2854). The operator's
        # own Flatten, while it works, is a sale in progress like any other.
        exit_queued=(run.exit_in_progress and not run.exit_unconfirmed)
        or (panel.end is not None and panel.end.status == "ending" and not panel.end.waits_for_operator),
        exit_unconfirmed=run.exit_unconfirmed,
        authored_at_ms=now_ms,
    )


def _count_words(count: int, noun: str, *, floor: bool = False) -> str:
    if floor:
        return f"at least {count:,} {noun}s"
    if count == 0:
        return f"no {noun}s"
    return f"1 {noun}" if count == 1 else f"{count:,} {noun}s"


def _decision_words(count: int, *, floor: bool) -> str:
    return _count_words(count, "decision", floor=floor)


def _span_words(facts: RunSummaryFacts) -> str:
    """``Wed Sep 30, 14:30–15:59 ET``, naming the end's day only when it differs."""
    assert facts.started_at_ms is not None
    start_day = et_day_words(facts.started_at_ms, now_ms=facts.authored_at_ms)
    start = f"{start_day}, {et_clock_words(facts.started_at_ms)}"
    if facts.ended_at_ms is None:
        return f"{start} ET"
    if et_date_at_ms(facts.ended_at_ms) == et_date_at_ms(facts.started_at_ms):
        return f"{start}–{et_clock_words(facts.ended_at_ms)} ET"
    end_day = et_day_words(facts.ended_at_ms, now_ms=facts.authored_at_ms)
    return f"{start} ET – {end_day}, {et_clock_words(facts.ended_at_ms)} ET"


def _money_words(facts: RunSummaryFacts) -> str | None:
    if facts.held:
        return "holds " + ", ".join(f"{held.quantity} {held.symbol}" for held in facts.held)
    if facts.set_aside_usd is not None:
        return f"${facts.set_aside_usd} set aside"
    if facts.returned_usd is not None:
        return f"${facts.returned_usd} back to the account"
    return None


def run_summary_text(facts: RunSummaryFacts) -> str:
    """Template v2: the run in one line, a pure function of its facts.

    "Ran Wed Sep 30, 14:30–15:59 ET · ended on schedule · 5 decisions, no
    trades · $800.00 back to the account."

    v2 (#2854): a closing order the Clerk could not confirm is said as that.
    v1 said "a sale is queued" of it, and none is: the Clerk holds the
    position and sends nothing more for the bot. None is queued once the
    order is settled as not sent either: the position is then the operator's
    to sell or write off.
    """
    if facts.ending == "unreadable":
        return _UNREADABLE_RUN
    if facts.ending == "not_started" or facts.started_at_ms is None:
        return "Not started yet."
    decisions = _decision_words(facts.decision_count, floor=facts.decision_count_is_floor)
    activity = f"{decisions}, {_count_words(facts.trade_count, 'trade')}"
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
    if facts.exit_unconfirmed:
        parts.append("a closing order is unconfirmed")
    elif facts.exit_queued:
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
    # Why there is nothing for it to do, in the bot page's words.
    not_needed: str


# The custody actions in the order the toolbar shows them, with their plain
# names (R6). Sell is ``prepare_safe_flatten``: its ticket's second step is
# ``execute_safe_flatten``, so execute has no button of its own.
_CUSTODY_ENTRIES: dict[str, _CustodyEntry] = {
    "stop_bot_decisions": _CustodyEntry(
        "Stop", "bot", "danger", "Stop this bot's decisions now.", "This bot is not running."
    ),
    "prepare_safe_flatten": _CustodyEntry(
        "Sell", "bot", "danger", "Review a plan to sell what this bot holds, then send it.", "This bot holds no shares."
    ),
    "reconcile_now": _CustodyEntry(
        "Check against Alpaca", "fix", "neutral", "Compare this account with Alpaca now.", "Nothing to check."
    ),
    "cancel_verified_working_orders": _CustodyEntry(
        "Cancel open orders", "fix", "danger", "Cancel this bot's open orders at Alpaca.", "This bot has no open orders."
    ),
    "settle_unknown_exit_order": _CustodyEntry(
        SETTLE_UNKNOWN_EXIT_ORDER_LABEL,
        "fix",
        "danger",
        "Say what Alpaca shows for a closing order the Clerk could not confirm.",
        "No closing order is uncertain.",
    ),
    "discharge_attributed_residue": _CustodyEntry(
        "Write off missing shares",
        "fix",
        "danger",
        "Write off shares the Clerk holds but Alpaca does not.",
        "No shares are missing.",
    ),
    "recover_exact_execution_evidence": _CustodyEntry(
        "Recover a missing fill",
        "fix",
        "warning",
        "Read a missing fill back from Alpaca's records.",
        "No fill is missing.",
    ),
    "resolve_execution_coverage": _CustodyEntry(
        "Use the exact fill",
        "fix",
        "warning",
        "Settle a fill the Clerk recorded two ways on the exact one.",
        "No fill is recorded two ways.",
    ),
    "open_custody_timeline": _CustodyEntry(
        "Custody timeline", "inspect", "neutral", "Open this bot's custody timeline.", "Nothing to show."
    ),
}


def _sell_label(panel: BotPanelView) -> str:
    if len(panel.exposure) != 1:
        return "Sell"
    ((symbol, quantity),) = panel.exposure.items()
    return f"Sell {quantity_text(abs(quantity))} {symbol}"


def _custody_entry(action: PanelAction, panel: BotPanelView) -> ToolbarActionView:
    entry = _CUSTODY_ENTRIES[action.action_id]
    label = _sell_label(panel) if action.action_id == "prepare_safe_flatten" else entry.label
    # A running bot always needs its Stop, whatever the Clerk's run state says mid-stop.
    needed = action.needed or (action.action_id == "stop_bot_decisions" and panel.health.running)
    availability: ToolbarAvailability
    if action.enabled:
        availability, reason = "available", entry.does
    elif not needed:
        availability, reason = "not_needed", entry.not_needed
    else:
        availability = "blocked"
        reason = action.blockers[0].headline if action.blockers else action.explanation
    return ToolbarActionView(
        action_id=action.action_id,
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
    if end is None:
        return _entry("change_end", "Change end", "bot", "not_needed", "This bot has no end to change.")
    if end.editable:
        return _entry("change_end", "Change end", "bot", "available", "Change when this bot ends and what it does then.")
    # An end that has come is needed and cannot change: it waits on the Clerk, or on the operator (#2854).
    availability: ToolbarAvailability = "blocked" if end.status == "ending" else "not_needed"
    return _entry("change_end", "Change end", "bot", availability, end.edit_refusal or end.explanation)


def _deploy_again_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.health.running:
        return _entry("deploy_again", "Deploy again", "bot", "not_needed", "This bot is still running.")
    return _entry("deploy_again", "Deploy again", "bot", "available", "Deploy a new bot with these settings.")


_HELD_BEHIND_AN_UNCONFIRMED_ORDER = (
    "The Clerk holds this bot's position behind a closing order it could not confirm."
)


def _settle_before_clear(settle: PanelAction) -> str:
    """Clear's reason while a closing order is unconfirmed: settle it, or why it cannot be settled now (#2854).

    Which of the two is the settle action's own availability, as the recovery
    catalog presented it. Refused, it gives its reason and next step: for an
    order Alpaca answered with one that names no client order id, say, there
    is nothing to use first.
    """
    if settle.enabled:
        return (
            f'{_HELD_BEHIND_AN_UNCONFIRMED_ORDER} Use "{_CUSTODY_ENTRIES["settle_unknown_exit_order"].label}" first.'
        )
    blocker = settle.blockers[0] if settle.blockers else None
    return " ".join((
        _HELD_BEHIND_AN_UNCONFIRMED_ORDER,
        settle_unavailable_words(
            None if blocker is None else blocker.headline, None if blocker is None else blocker.detail
        ),
    ))


def _clear_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.status == "cleared":
        return _entry("archive", "Clear from Home", "bot", "not_needed", "This bot is already cleared from Home.")
    archive = next((action for action in panel.actions if action.action_id == "archive"), None)
    if archive is None:
        return _entry("archive", "Clear from Home", "bot", "not_needed", "A running bot stays on Home.")
    if archive.enabled:
        return _entry("archive", "Clear from Home", "bot", "available", "Take this finished bot off Home.")
    settle = next((action for action in panel.actions if action.action_id == "settle_unknown_exit_order"), None)
    if settle is not None and settle.needed:
        # Clear's own refusal says to sell first, and a sale is refused too
        # while a closing order is unconfirmed (#2854): the way on is to settle it.
        return _entry("archive", "Clear from Home", "bot", "blocked", _settle_before_clear(settle))
    reason = archive.blockers[0].headline if archive.blockers else archive.explanation
    return _entry("archive", "Clear from Home", "bot", "blocked", reason)


def _manual_order_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.mode == "dry_run":
        return _entry(
            "manual_order",
            "Manual order",
            "bot",
            "not_needed",
            "A Dry Run trades simulated cash, so it has no orders to place.",
        )
    if panel.status == "cleared":
        return _entry("manual_order", "Manual order", "bot", "not_needed", "A cleared bot's page is read-only.")
    return _entry(
        "manual_order", "Manual order", "bot", "available",
        f"Place an order for {order_symbol(panel.symbol, panel.traded)} on this account.",
    )


def _build_proof_entry(panel: BotPanelView) -> ToolbarActionView:
    if panel.program_build.state == "NOT_APPLICABLE":
        return _entry(
            "build_proof", "Build proof", "inspect", "not_needed", "This strategy has no sealed program to prove."
        )
    return _entry("build_proof", "Build proof", "inspect", "available", "Show this run's build proof and its hashes.")


def toolbar_actions(panel: BotPanelView, *, status: BotOwnStatusView) -> list[ToolbarActionView]:
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
        *custody_entry("settle_unknown_exit_order"),
        *custody_entry("discharge_attributed_residue"),
        *custody_entry("recover_exact_execution_evidence"),
        *custody_entry("resolve_execution_coverage"),
        *custody_entry("open_custody_timeline"),
        _build_proof_entry(panel),
    ]
    primary: str | None = panel.primary_action
    if primary == "execute_safe_flatten":
        primary = "prepare_safe_flatten"
    if primary is None and status.state == "finished":
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
    # The panel's last decision is the bot's latest ever; only this run's counts here.
    last = panel.health.last_decision_at_ms
    last_this_run = last if last is not None and run.started_at_ms is not None and last >= run.started_at_ms else None
    late = panel.health.running and panel.health.decision_stale and last_this_run is not None
    return [
        HealthLineView(
            key="feed",
            label=f"{feed.provider_label} feed",
            state=_FEED_STATES[feed.state],
            value=feed.state_label,
            note=feed.explanation,
        ),
        HealthLineView(
            key="decisions",
            label="Decisions",
            state="attention" if late else "ok",
            value=_decision_words(run.decision_count, floor=run.decision_count_is_floor),
            note="No decision has come on time." if late else None,
            at_ms=last_this_run,
        ),
        HealthLineView(key="orders", label="Orders sent", state="ok", value=_count_words(run.orders_sent, "order")),
    ]


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
    # A hold or freeze stops new orders: a problem for a running bot, a fact to know for a stopped one.
    held_state: HealthLineState = "problem" if running else "attention"
    if clerk.freeze_active:
        lines.append(
            HealthLineView(
                key="holds",
                label="Holds",
                state=held_state,
                value=clerk.freeze_label,
                note=clerk.freeze_explanation if running else _NO_EFFECT_ON_STOPPED,
                at_ms=clerk.freeze_observed_at_ms,
            )
        )
    elif clerk.hold_active:
        lines.append(
            HealthLineView(
                key="holds",
                label="Holds",
                state=held_state,
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
    status = bot_own_status(
        panel,
        bot_owns_custody_problem=bot_owns_custody_problem,
        execution_coverage_complete=execution_coverage_complete,
        run_unreadable=run.unreadable,
    )
    return BotPageView(
        status=status,
        summary=run_summary(panel, run, now_ms=now_ms),
        toolbar=toolbar_actions(panel, status=status),
        health=health_groups(panel, run),
    )


_NOT_A_REAL_FILL = "A real order might not have filled at that price."
_UNRECORDED_SIMULATED_FILL = SimulatedFillView(
    kind="unknown",
    label="Simulated · pricing not on record",
    sentence="Simulated. There is no readable record of how this fill was priced.",
    caution=True,
)


def _price_age(fill: FillRecord, price_made_at_ms: int) -> tuple[int, str]:
    """How long before the fill its price was made, as a figure and in words; never negative.

    The two times can come from different clocks (the feed's and this
    process's), so a price "made" after its fill is one of no age.
    """
    age_ms = max(0, fill.filled_at_ms - price_made_at_ms)
    return age_ms, duration_label(age_ms)


def simulated_fill_view(anchor: SynthesizedAnchor, fill: FillRecord) -> SimulatedFillView:
    """How one simulated fill was priced, in the owner's words (#2912).

    The anchor's provider tells a market bar from the two prices a no-submit
    world makes for itself: a recovery exit's live quote and a run-end
    close's last price seen.
    """
    if anchor.provider == RECOVERY_QUOTE_PROVIDER:
        quoted = "bid" if fill.side is OrderSide.SELL else "ask"
        return SimulatedFillView(
            kind="live_quote",
            label="Simulated · live quote",
            sentence=f"Simulated: filled at once at the live IBKR {quoted}. {_NOT_A_REAL_FILL}",
            caution=False,
        )
    if anchor.provider == RUN_END_CLOSE_PROVIDER:
        if anchor.restated_bar_end_ms is None:
            # A close recorded before its price's own time was kept: its age is not guessed.
            return SimulatedFillView(
                kind="run_end_close",
                label="Simulated · last price seen",
                sentence="Simulated: closed at the last price this run saw, which may be older than this fill.",
                caution=True,
            )
        age_ms, age = _price_age(fill, anchor.restated_bar_end_ms)
        return SimulatedFillView(
            kind="run_end_close",
            label=f"Simulated · last price seen, {age} old",
            sentence=f"Simulated: closed at the last price this run saw, which last traded {age} earlier.",
            price_age_ms=age_ms,
            caution=True,
        )
    if anchor.fill_model == "older_bar_close":
        age_ms, age = _price_age(fill, anchor.decision_bar_end_ms)
        return SimulatedFillView(
            kind="older_bar_close",
            label=f"Simulated · price {age} old",
            sentence=(
                f"Simulated: {fill.symbol} had no price for this minute, so the fill used its "
                f"newest earlier closing price, which was {age} old."
            ),
            price_age_ms=age_ms,
            caution=True,
        )
    if anchor.fill_model == "limit_touch":
        return SimulatedFillView(
            kind="limit_touch",
            label="Simulated · limit reached",
            sentence=(
                "Simulated: filled at its limit price when a later minute's range reached it. "
                "A real order might not have filled."
            ),
            caution=False,
        )
    return SimulatedFillView(
        kind="bar_close",
        label="Simulated",
        sentence=f"Simulated: filled at once at the closing price of its minute. {_NOT_A_REAL_FILL}",
        caution=False,
    )


def run_fill_views(
    run_fills: Sequence[FillRecord], anchors: Mapping[str, SynthesizedAnchor] | None
) -> list[RunFillView]:
    """The run's newest fills as the page lists them, each simulated one saying how it was priced (#2912).

    ``anchors`` is a no-submit authority's record of how it priced each
    order, by order ref. Every fill of such an authority is simulated, so
    one with no record there says its pricing is not on record instead of
    passing for a fill Alpaca made. ``None`` is an authority whose fills
    Alpaca makes: none of them is simulated.
    """
    views: list[RunFillView] = []
    for fill in run_fills[-RUN_FILL_LIMIT:]:
        simulated: SimulatedFillView | None = None
        if anchors is not None:
            anchor = anchors.get(fill.order_ref)
            simulated = _UNRECORDED_SIMULATED_FILL if anchor is None else simulated_fill_view(anchor, fill)
        views.append(RunFillView(**fill_to_marker(fill).model_dump(), simulated=simulated))
    return views


__all__ = [
    "RUN_FILL_LIMIT",
    "SUMMARY_TEMPLATE_VERSION",
    "RunFacts",
    "bot_own_status",
    "bot_page_view",
    "health_groups",
    "run_ending",
    "run_facts",
    "run_fill_views",
    "run_summary",
    "run_summary_facts",
    "run_summary_text",
    "simulated_fill_view",
    "toolbar_actions",
]
