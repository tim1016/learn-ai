"""Panel projection — the full 5s-poll bot control panel view (spec §7).

``build_panel`` composes the bot-health card, the account/clerk card, the six-
station transaction rail, the journal-tail reference, and the presented actions
(with the panel-state ``revision`` those actions bind to). It is a pure
function over its inputs so the router seam test drives it with journal
fixtures — no live clerk/registry required.

The ``revision`` is a deterministic function of the durable panel state
(journal length + lifecycle transition + desired state + hold state). A POST
against a stale revision is a 409; the same revision surviving a no-op re-poll
lets the idempotency key make double-clicks safe (§11).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.alpaca.clerk.decision_journal import DecisionReceipt
from app.broker.alpaca.clerk.exposure import project_expected_account_exposure
from app.broker.alpaca.clerk.fills import project_instance_fills
from app.broker.alpaca.clerk.models import ClerkEntryKind, ClerkStatus, OrderJournalEntry
from app.broker.v2panel.vocabulary import (
    HOLD_REASONS,
    ChannelState,
    copy_for,
    duty_outcome_copy_key,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotHealthCard,
    BotPanelView,
    ChannelHealthView,
    ClerkCard,
    DutyOutcomeView,
    MarketPulseView,
    MissionVerdictView,
    PanelAction,
    ReadinessCheckView,
    RecentDecisionView,
    RecentFillView,
    TransactionRail,
    WorkingOrderView,
)
from app.schemas.run_admission import RunAdmissionDecision
from app.services.bot_dry_run import DryRunActivity
from app.services.broker_v2_panel.presented_actions import build_actions
from app.services.broker_v2_panel.station_derivation import (
    STALE_THRESHOLD_MS,
    derive_stations,
    transaction_refs_for_bot,
)

# A channel-health observation older than this is not "fresh" for the clear-hold
# gate (§7.3). Reuse the station staleness threshold — one trading day.
CHANNEL_FRESH_THRESHOLD_MS = STALE_THRESHOLD_MS
_REQUIRED_CLERK_CHANNELS = ("market_data", "execution")


@dataclass(frozen=True)
class ChannelHealthEvaluation:
    """Canonical health/freshness verdict for Clerk submission channels."""

    ready: bool
    missing: tuple[str, ...]
    stale: tuple[str, ...]
    unhealthy: tuple[str, ...]

_STOP_OUTCOME_COPY: dict[str, tuple[str, str]] = {
    "STOPPED_FLAT": (
        "Stopped flat",
        "The runtime is stopped and the Clerk proved zero attributed exposure.",
    ),
    "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE": (
        "Stopped with approved carryover",
        "The runtime is stopped and exact attributed exposure is preserved by a durable checkpoint.",
    ),
    "STOP_REQUIRES_FLATTEN": (
        "Stopped; flatten required",
        "The runtime is stopped, but carried exposure was not approved. Use the Clerk flatten action before Resume.",
    ),
    "STOPPED_CUSTODY_UNPROVABLE": (
        "Stopped; custody unprovable",
        "The runtime is stopped, but the Clerk could not prove a terminal flat or carryover outcome.",
    ),
}


def _hold_reason_code(hold_active: bool, raw_reason_code: str | None) -> str:
    """Map the clerk's raw hold code to the closed HoldReason vocabulary.

    An inactive hold — or an active hold whose code is not in the closed set —
    resolves to ``NO_HOLD``, so an unknown backend code never leaks to the UI.
    """
    if hold_active and raw_reason_code in HOLD_REASONS:
        return raw_reason_code
    return "NO_HOLD"


def compute_revision(
    *,
    journal_len: int,
    last_transition_at_ms: int | None,
    desired_state: str,
    hold_active: bool,
    last_decision_at_ms: int | None,
) -> int:
    """Deterministic panel-state revision.

    Any durable change (a new journal line, a lifecycle transition, a desired-
    state flip, a hold set/cleared, a new decision receipt) advances it. A
    stable panel state re-polls to the same revision, so an idempotent re-post
    of the same action against the same revision is a safe no-op.
    """
    parts = (
        journal_len,
        last_transition_at_ms or 0,
        1 if desired_state == "RUNNING" else 0,
        1 if hold_active else 0,
        last_decision_at_ms or 0,
    )
    # A stable, non-cryptographic mix; the value only needs to change when any
    # durable input changes and to be reproducible for the same inputs.
    revision = 0
    for part in parts:
        revision = (revision * 1_000_003 + int(part)) & 0x7FFF_FFFF_FFFF_FFFF
    return revision


def _latest_reconciliation_entry(entries: list[OrderJournalEntry]) -> OrderJournalEntry | None:
    latest: OrderJournalEntry | None = None
    for entry in entries:
        if entry.kind is ClerkEntryKind.RECONCILIATION and entry.verdict is not None:
            latest = entry
    return latest


def _duty_outcome_view(status: BotStatusView) -> DutyOutcomeView | None:
    outcome = status.duty_outcome
    if outcome is None:
        return None
    copy = copy_for(duty_outcome_copy_key(outcome.kind))
    label, explanation = _STOP_OUTCOME_COPY.get(
        outcome.reason_code,
        (copy.label, copy.explanation),
    )
    return DutyOutcomeView(
        kind=outcome.kind,  # type: ignore[arg-type]
        reason_code=outcome.reason_code,
        label=label,
        explanation=explanation,
        recorded_at_ms=outcome.recorded_at_ms,
        run_id=outcome.run_id,
    )


def _build_health_card(
    status: BotStatusView,
    *,
    clerk: ClerkCard,
    exposure: dict[str, float],
    last_decision_at_ms: int | None,
    last_bar_at_ms: int | None,
    now_ms: int,
    resume_admission: RunAdmissionDecision | None,
) -> BotHealthCard:
    desired_state = status.desired_state
    decision_stale = last_decision_at_ms is not None and now_ms - last_decision_at_ms > STALE_THRESHOLD_MS
    can_resume = resume_admission is not None and resume_admission.allowed
    has_exposure = any(abs(quantity) > 0 for quantity in exposure.values())
    if can_resume and has_exposure:
        resume_label = "Resume custody proof ready"
        resume_explanation = (
            "The durable checkpoint, current Clerk attribution, and latest "
            "clean reconciliation agree. Resume will obtain one fresh broker proof."
        )
    elif can_resume:
        resume_label = "Flat Resume ready"
        resume_explanation = "The stopped instance is flat and may resume as a newly identified run."
    elif status.running:
        resume_label = "Resume not applicable"
        resume_explanation = "This strategy instance already has a live run."
    elif resume_admission is not None:
        resume_label = "Resume blocked"
        resume_explanation = resume_admission.explanation
    else:
        resume_label = "Resume unknown"
        resume_explanation = "The backend could not obtain one current Resume admission decision."
    return BotHealthCard(
        strategy_instance_id=status.strategy_instance_id,
        phase=status.phase,
        phase_label=copy_for(status.phase).label,
        desired_state=desired_state,
        desired_state_label=copy_for(desired_state).label,
        running=status.running,
        duty_outcome=_duty_outcome_view(status),
        last_decision_at_ms=last_decision_at_ms,
        decision_stale=decision_stale,
        last_bar_at_ms=last_bar_at_ms,
        resume_eligible=can_resume,
        resume_label=resume_label,
        resume_explanation=resume_explanation,
        carryover_checkpoint_exposure=status.carryover_checkpoint_exposure,
    )


def _channel_state(*, healthy: bool, observed_at_ms: int, now_ms: int) -> ChannelState:
    """Derive the closed channel state from health + freshness (§7.3)."""
    if now_ms - observed_at_ms > CHANNEL_FRESH_THRESHOLD_MS:
        return "unknown"
    return "healthy" if healthy else "unhealthy"


def _channel_views(clerk_status: ClerkStatus, now_ms: int) -> list[ChannelHealthView]:
    views: list[ChannelHealthView] = []
    for health in clerk_status.channel_healths or []:
        state = _channel_state(healthy=health.healthy, observed_at_ms=health.observed_at_ms, now_ms=now_ms)
        copy = copy_for(state)
        views.append(
            ChannelHealthView(
                stream=health.stream,
                state=state,
                label=copy.label,
                explanation=copy.explanation,
                reason=health.reason,
                observed_at_ms=health.observed_at_ms,
            )
        )
    return views


def build_clerk_card(clerk_status: ClerkStatus, now_ms: int) -> ClerkCard:
    """Project the account Clerk state shared by panel and roster actions."""
    hold = clerk_status.hold
    reason_code = _hold_reason_code(hold.active, hold.reason_code)
    reason_copy = copy_for(reason_code)
    reconciliation = clerk_status.latest_reconciliation
    verdict = reconciliation.verdict if reconciliation is not None else None
    freeze = clerk_status.freeze
    return ClerkCard(
        account_id=clerk_status.account_id,
        hold_active=hold.active,
        hold_reason=reason_code,  # type: ignore[arg-type]
        hold_reason_label=reason_copy.label,
        hold_reason_explanation=reason_copy.explanation,
        hold_since_ms=hold.since_ms,
        freeze_active=freeze.active,
        freeze_category=freeze.category,
        freeze_label=(
            "Account state unattributable"
            if freeze.category == "ACCOUNT_STATE_UNATTRIBUTABLE"
            else ("Account state unprovable" if freeze.category == "ACCOUNT_STATE_UNPROVABLE" else "No account freeze")
        ),
        freeze_explanation=freeze.explanation or "The Clerk has current, attributable account truth.",
        freeze_next_step=freeze.next_step,
        freeze_observed_at_ms=freeze.observed_at_ms,
        reconciliation_verdict=verdict,  # type: ignore[arg-type]
        reconciliation_verdict_label=(copy_for(verdict).label if verdict is not None else None),
        last_sweep_at_ms=(reconciliation.recorded_at_ms if reconciliation is not None else None),
        outstanding_intents=clerk_status.outstanding_intents,
        channels=_channel_views(clerk_status, now_ms),
    )


def evaluate_channel_health(
    clerk_status: ClerkStatus,
    now_ms: int,
) -> ChannelHealthEvaluation:
    """Evaluate the exact channel set required by every submission gate."""
    by_stream = {health.stream: health for health in clerk_status.channel_healths or []}
    missing = tuple(stream for stream in _REQUIRED_CLERK_CHANNELS if stream not in by_stream)
    stale = tuple(
        stream
        for stream in _REQUIRED_CLERK_CHANNELS
        if stream in by_stream
        and now_ms - by_stream[stream].observed_at_ms > CHANNEL_FRESH_THRESHOLD_MS
    )
    unhealthy = tuple(
        stream
        for stream in _REQUIRED_CLERK_CHANNELS
        if stream in by_stream and not by_stream[stream].healthy
    )
    return ChannelHealthEvaluation(
        ready=not missing and not stale and not unhealthy,
        missing=missing,
        stale=stale,
        unhealthy=unhealthy,
    )


def channel_health_fresh(clerk_status: ClerkStatus, now_ms: int) -> bool:
    """True iff market-data and execution health are both healthy and fresh."""
    return evaluate_channel_health(clerk_status, now_ms).ready


_TERMINAL_ORDER_EVENTS = frozenset({"fill", "canceled", "rejected", "expired"})


def _working_orders(sid: str, entries: list[OrderJournalEntry]) -> list[WorkingOrderView]:
    """Return Clerk-owned submitted orders without a terminal lifecycle event."""
    owned_refs = set(transaction_refs_for_bot(sid, entries))
    terminal_refs = {
        entry.order_ref
        for entry in entries
        if entry.order_ref in owned_refs
        and entry.kind is ClerkEntryKind.ORDER_EVENT
        and entry.event is not None
        and entry.event.event_type in _TERMINAL_ORDER_EVENTS
    }
    latest_by_ref: dict[str, OrderJournalEntry] = {}
    for entry in entries:
        if (
            entry.order_ref in owned_refs
            and entry.order_ref not in terminal_refs
            and entry.order is not None
            and entry.kind in {ClerkEntryKind.SUBMIT_ACKED, ClerkEntryKind.ORDER_EVENT}
        ):
            latest_by_ref[entry.order_ref] = entry
    return [
        WorkingOrderView(
            order_ref=entry.order_ref,
            broker_order_id=entry.order.order_id,
            symbol=entry.order.symbol,
            side=entry.order.side,
            quantity=entry.order.quantity,
            filled_quantity=entry.order.filled_quantity,
            status=entry.order.status,
            observed_at_ms=entry.order.observed_at_ms,
        )
        for entry in latest_by_ref.values()
        if entry.order is not None
    ]


def _account_working_order_count(entries: list[OrderJournalEntry]) -> int:
    """Count current Clerk-owned working orders across every namespace."""

    terminal_refs = {
        entry.order_ref
        for entry in entries
        if entry.order_ref
        and entry.kind is ClerkEntryKind.ORDER_EVENT
        and entry.event is not None
        and entry.event.event_type in _TERMINAL_ORDER_EVENTS
    }
    latest_by_ref: dict[str, OrderJournalEntry] = {}
    for entry in entries:
        if (
            entry.order_ref
            and entry.order_ref not in terminal_refs
            and entry.order is not None
        ):
            latest_by_ref[entry.order_ref] = entry
    return sum(
        1
        for entry in latest_by_ref.values()
        if entry.order is not None
        and entry.order.status.lower()
        not in {"filled", "canceled", "expired", "rejected", "replaced"}
    )


def _recent_decision_views(receipts: list[DecisionReceipt], *, limit: int = 8) -> list[RecentDecisionView]:
    return [
        RecentDecisionView(
            seq=receipt.seq,
            recorded_at_ms=receipt.ts_ms,
            outcome=receipt.outcome,
            reason_code=receipt.reason_code,
            bar_ref=receipt.bar_ref,
            order_ref=receipt.order_ref or None,
        )
        for receipt in reversed(receipts[-limit:])
    ]


def _recent_fill_views(sid: str, entries: list[OrderJournalEntry], *, limit: int = 8) -> list[RecentFillView]:
    fills = project_instance_fills(sid, entries)
    return [
        RecentFillView(
            order_ref=fill.order_ref,
            symbol=fill.symbol,
            side=fill.side.value,
            quantity=fill.quantity,
            price=fill.fill_price,
            filled_at_ms=fill.filled_at_ms,
        )
        for fill in reversed(fills[-limit:])
    ]


def _dry_run_decision_views(
    activity: list[DryRunActivity],
    *,
    limit: int = 8,
) -> list[RecentDecisionView]:
    return [
        RecentDecisionView(
            seq=row.seq,
            recorded_at_ms=row.recorded_at_ms,
            outcome="entered" if row.intent == "ENTER" else "exited",
            reason_code=f"SIMULATED_{row.intent}",
            bar_ref=row.bar_ref,
            order_ref=row.order_ref,
            simulated=True,
        )
        for row in reversed(activity[-limit:])
    ]


def _dry_run_fill_views(
    activity: list[DryRunActivity],
    *,
    limit: int = 8,
) -> list[RecentFillView]:
    return [
        RecentFillView(
            order_ref=row.order_ref,
            symbol=row.symbol,
            side=row.side,
            quantity=row.quantity,
            price=row.fill_price,
            filled_at_ms=row.recorded_at_ms,
            simulated=True,
        )
        for row in reversed(activity[-limit:])
    ]


def _execution_policy(mode: str) -> str:
    policies = {
        "dry_run": (
            "Dry Run. Real market data produces clearly simulated decisions "
            "and fills; broker writes are impossible."
        ),
        "log_only": (
            "Observation only. Decisions are recorded, but the Clerk will not place orders."
        ),
        "trade": "Paper execution. Only the Clerk may submit, cancel, or reduce broker orders.",
    }
    return policies[mode]


def _recent_activity_views(
    status: BotStatusView,
    entries: list[OrderJournalEntry],
    decision_receipts: list[DecisionReceipt],
    dry_run_activity: list[DryRunActivity],
) -> tuple[list[RecentDecisionView], list[RecentFillView]]:
    """Project real or simulated activity behind one explicit mode boundary."""
    if status.mode == "dry_run":
        return (
            _dry_run_decision_views(dry_run_activity),
            _dry_run_fill_views(dry_run_activity),
        )
    return (
        _recent_decision_views(decision_receipts),
        _recent_fill_views(status.strategy_instance_id, entries),
    )


def _dry_run_last_bar_at_ms(activity: list[DryRunActivity]) -> int | None:
    """Return the latest simulated bar timestamp, falling back to receipt time."""
    if not activity:
        return None
    latest = activity[-1]
    _symbol, separator, candidate = latest.bar_ref.rpartition("@")
    if separator and candidate.isdecimal():
        return int(candidate)
    return latest.recorded_at_ms


def _readiness_checks(actions: list[PanelAction], now_ms: int) -> list[ReadinessCheckView]:
    """Project present-tense enforcement checks from the canonical action guards."""
    checks: list[ReadinessCheckView] = []
    authorities = {
        "resume": "Bot lifecycle registry + Clerk custody proof",
        "pause": "Bot lifecycle registry",
        "continue": "Bot lifecycle registry",
        "stop": "Bot lifecycle registry",
        "flatten_stop": "Bot lifecycle registry + Alpaca Clerk",
        "clear_hold": "Alpaca Clerk account hold gate",
        "record_inventory_baseline": "Alpaca Clerk broker-evidence baseline",
        "reconcile_now": "Alpaca Clerk reconciliation sweep",
    }
    for action in actions:
        blocker = action.blockers[0] if action.blockers else None
        checks.append(
            ReadinessCheckView(
                operation=action.action_id,
                label=action.label,
                ready=action.enabled,
                scope=(blocker.condition.scope if blocker else "bot"),
                authority=authorities.get(action.action_id, "Panel action policy"),
                explanation=(
                    action.explanation
                    if action.enabled
                    else (
                        blocker.headline
                        if blocker is not None
                        else "This operation is unavailable in the current state."
                    )
                ),
                evidence=(blocker.condition.evidence if blocker else {}),
                evaluated_at_ms=now_ms,
                cure=(blocker.detail if blocker is not None else None),
            )
        )
    return checks


def _mission_verdict(
    status: BotStatusView,
    clerk: ClerkCard,
    actions: list[PanelAction],
    *,
    channel_health: ChannelHealthEvaluation,
    now_ms: int,
) -> MissionVerdictView:
    resume = next((action for action in actions if action.action_id == "resume"), None)
    if status.phase == "RETIRED":
        return MissionVerdictView(
            state="retired",
            label="Retired",
            explanation="This immutable strategy instance is permanently off duty.",
            next_action="Deploy a replacement strategy instance.",
            evaluated_at_ms=now_ms,
        )
    if clerk.freeze_active or clerk.hold_active:
        reason = clerk.freeze_explanation if clerk.freeze_active else clerk.hold_reason_explanation
        next_action = clerk.freeze_next_step or "Restore the root condition, reconcile, then clear the hold."
        return MissionVerdictView(
            state="blocked",
            label="Mission blocked",
            explanation=reason,
            next_action=next_action,
            evaluated_at_ms=now_ms,
        )
    if status.mode == "trade" and not channel_health.ready:
        blocked_channels = (
            *channel_health.missing,
            *channel_health.stale,
            *channel_health.unhealthy,
        )
        return MissionVerdictView(
            state="blocked",
            label="Mission blocked",
            explanation=(
                "Required Clerk channels are not healthy: "
                f"{', '.join(dict.fromkeys(blocked_channels))}."
            ),
            next_action="Restore fresh market-data and execution health before expecting order effects.",
            evaluated_at_ms=now_ms,
        )
    if status.running:
        if status.desired_state == "PAUSED":
            return MissionVerdictView(
                state="ready",
                label="Paused",
                explanation="The current run is live, but bar evaluation is held.",
                next_action="Use Continue to release this same run without changing its run ID.",
                evaluated_at_ms=now_ms,
            )
        return MissionVerdictView(
            state="working",
            label="Working",
            explanation="The runtime is on duty and the Clerk has not raised an account gate.",
            next_action="Monitor decisions, fills, and custody evidence.",
            evaluated_at_ms=now_ms,
        )
    if resume is not None and resume.enabled:
        return MissionVerdictView(
            state="ready",
            label="Ready to resume",
            explanation="The shared backend admission currently allows a new run.",
            next_action="Resume the bot when the execution session is intended to run.",
            evaluated_at_ms=now_ms,
        )
    blocker = resume.blockers[0] if resume is not None and resume.blockers else None
    return MissionVerdictView(
        state="off_duty",
        label="Off duty",
        explanation=(blocker.headline if blocker is not None else "The bot is not evaluating bars."),
        next_action=(blocker.detail if blocker is not None else "Review readiness before Resume."),
        evaluated_at_ms=now_ms,
    )


def build_panel(
    status: BotStatusView,
    clerk_status: ClerkStatus,
    entries: list[OrderJournalEntry],
    *,
    account_id: str,
    exposure: dict[str, float],
    fills_today: int,
    realized_pnl_today: float,
    open_pnl: float | None,
    latest_decision: DecisionReceipt | None,
    last_bar_at_ms: int | None,
    journal_tail_ref: str,
    journal_tail_seq: int | None,
    flatten_supported: bool,
    now_ms: int,
    selected_transaction_ref: str | None = None,
    recent_decisions: list[DecisionReceipt] | None = None,
    resume_admission: RunAdmissionDecision | None = None,
    dry_run_activity: list[DryRunActivity] | None = None,
    market_pulse: MarketPulseView | None = None,
) -> BotPanelView:
    """Build the full panel view for one bot (§7).

    ``entries`` is the account order journal (read once). ``selected_transaction_ref``
    defaults to the bot's most recent transaction (§7.1).
    """
    refs = transaction_refs_for_bot(status.strategy_instance_id, entries)
    transaction_ref = selected_transaction_ref or (refs[-1] if refs else None)
    latest_reconciliation = _latest_reconciliation_entry(entries)

    stations = derive_stations(
        sid=status.strategy_instance_id,
        transaction_ref=transaction_ref,
        all_entries=entries,
        latest_decision=latest_decision,
        latest_reconciliation=latest_reconciliation,
        now_ms=now_ms,
    )

    activity = dry_run_activity or []
    health_last_decision_at_ms = (
        activity[-1].recorded_at_ms
        if status.mode == "dry_run" and activity
        else (latest_decision.ts_ms if latest_decision is not None else None)
    )
    health_last_bar_at_ms = (
        _dry_run_last_bar_at_ms(activity)
        if status.mode == "dry_run"
        else last_bar_at_ms
    )
    clerk = build_clerk_card(clerk_status, now_ms)
    health = _build_health_card(
        status,
        clerk=clerk,
        exposure=exposure,
        last_decision_at_ms=health_last_decision_at_ms,
        last_bar_at_ms=health_last_bar_at_ms,
        now_ms=now_ms,
        resume_admission=resume_admission,
    )

    revision = compute_revision(
        journal_len=len(entries),
        last_transition_at_ms=status.last_transition_at_ms,
        desired_state=status.desired_state,
        hold_active=clerk_status.hold.active,
        last_decision_at_ms=health.last_decision_at_ms,
    )

    working_orders = _working_orders(status.strategy_instance_id, entries)
    channel_health = evaluate_channel_health(clerk_status, now_ms)
    actions = build_actions(
        status,
        clerk,
        revision=revision,
        flatten_supported=flatten_supported,
        channel_fresh=channel_health.ready,
        exposure=exposure,
        account_id=account_id,
        working_order_count=len(working_orders),
        account_working_order_count=_account_working_order_count(entries),
        account_expected_exposure=project_expected_account_exposure(entries),
        resume_admission=resume_admission,
    )

    decision_receipts = recent_decisions or ([latest_decision] if latest_decision else [])
    decision_views, fill_views = _recent_activity_views(
        status,
        entries,
        decision_receipts,
        activity,
    )

    return BotPanelView(
        strategy_instance_id=status.strategy_instance_id,
        strategy_key=status.strategy_key,
        strategy_label=status.strategy_key.replace("_", " ").replace("-", " ").title(),
        broker=status.broker,
        account_id=account_id,
        symbol=status.symbol,
        mode=status.mode,
        updated_at_ms=now_ms,
        revision=revision,
        market_pulse=market_pulse
        or MarketPulseView(
            session="UNKNOWN",
            feed_state="MISSING",
            latest_bar_at_ms=None,
            age_ms=None,
            source=None,
            expected_cadence_ms=60_000,
            headline="Market-data state unknown",
            explanation="No market-data health evidence was supplied to this projection.",
            next_step="Refresh feed health before starting or resuming a run.",
            attention_required=True,
            observed_at_ms=now_ms,
        ),
        mission_verdict=_mission_verdict(
            status,
            clerk,
            actions,
            channel_health=channel_health,
            now_ms=now_ms,
        ),
        execution_policy=_execution_policy(status.mode),
        health=health,
        clerk=clerk,
        rail=TransactionRail(transaction_ref=transaction_ref, stations=stations),
        journal_tail_ref=journal_tail_ref,
        journal_tail_seq=journal_tail_seq,
        actions=actions,
        readiness_checks=_readiness_checks(actions, now_ms),
        exposure=exposure,
        working_orders=working_orders,
        recent_decisions=decision_views,
        recent_fills=fill_views,
        fills_today=fills_today,
        realized_pnl_today=realized_pnl_today,
        open_pnl=open_pnl,
    )
