"""Panel projection — the full 5s-poll bot control panel view.

``build_panel`` composes the bot-health card, the account/clerk card and the
panel's market, feed and decision facts. It is a pure function over its inputs
so the router seam test drives it with fixtures — no live clerk/registry
required. The transaction rail, ``revision``, working orders, fills and
presented actions are ``sqlite_panel_adapter.adapt_sqlite_panel``'s, which
every served panel passes through (#2635).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Final, Literal

from app.broker.alpaca.clerk.account_authority import authority_kind_for_account
from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.alpaca.clerk.money import display_cents, dollars
from app.broker.alpaca.clerk.sqlite.decision_receipts import DecisionReceipt
from app.broker.v2panel.vocabulary import (
    ActionId,
    copy_for,
    hold_reason_for,
)
from app.engine.strategy.registry import strategy_experimental_notice
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.schemas.account_authority import SIMULATED_AUTHORITY_KINDS, AuthorityKind
from app.schemas.action_plan import TradedInstrumentView
from app.schemas.bot_end import BotEndView
from app.schemas.bot_history import BotHistoryStatus
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
    RecentDecisionView,
    TradedFeedLaneEvent,
    TradedFeedLaneView,
    TransactionRail,
)
from app.schemas.decision_explanation import DecisionExplanationRecord
from app.schemas.run_admission import (
    ProgramBuildAdmissionFact,
    proven_build_copy,
)
from app.schemas.signal_program_seal import SealedBotProgram
from app.schemas.strategy_view import DecisionExplanationView
from app.schemas.traded_asset_acknowledgement import UntestedTradedAssetWarning
from app.services.bot_binding_repository import ProgramBuildRunEvidence
from app.services.bot_dry_run import DryRunActivity
from app.services.broker_v2_panel.channel_health import (
    ChannelHealthEvaluation,
    channel_state,
    evaluate_channel_health,
)
from app.services.broker_v2_panel.feed_continuity_projection import (
    build_feed_continuity,
    build_startup_join,
    build_warmup_join,
)
from app.services.broker_v2_panel.outcome_copy import outcome_card_copy
from app.services.broker_v2_panel.panel_authority_guard import default_authority_account_id
from app.services.pricing_bar_stream import (
    PRICING_BAR_CONFLICT,
    PRICING_BAR_LATE,
    PRICING_BAR_NO_TRADE,
    PRICING_FEED_UNHEALTHY,
)
from app.services.source_bar_ledger import (
    RetainedContinuityEvent,
    RetainedStartupJoin,
    RetainedWarmupJoin,
)
from app.utils.session_anchors import et_date_at_ms


def decision_is_stale(last_decision_at_ms: int, now_ms: int) -> bool:
    """True when more than one whole regular session has passed since the last decision.

    The silence is counted in market time, from the canonical NYSE calendar.
    Each session the interval ``(last_decision_at_ms, now_ms]`` overlaps adds
    the share of its own open-to-close span that the interval covers, so an
    early close is one whole session at its real length, and a night, a
    weekend or a holiday adds nothing. A decision is stale once those shares
    total more than 1.

    One session is the "one trading day" this flag has always meant. On
    ordinary weekdays 24 wall-clock hours hold exactly one session of market
    time, so the rule agrees with the flat 24 hours it replaces there; it
    differs where that was wrong: a bot whose last decision was Friday's
    close is not late at Monday 08:00, nor across a holiday. The bound is not
    tighter because the health card is not given a bot's bar length: a
    shorter one would call every hourly bot late each morning until its first
    bar closed.

    A decision stamped after ``now_ms`` is not stale.
    """
    first_day = et_date_at_ms(last_decision_at_ms)
    silence = Fraction(0)
    for offset in range((et_date_at_ms(now_ms) - first_day).days + 1):
        try:
            session = session_window_for_date(first_day + timedelta(days=offset))
        except LookupError:  # a weekend or a holiday: no market time passes
            continue
        covered = min(now_ms, session.close_ms_utc) - max(last_decision_at_ms, session.open_ms_utc)
        if covered > 0:
            silence += Fraction(covered, session.close_ms_utc - session.open_ms_utc)
            if silence > 1:
                return True
    return False


def _duty_outcome_view(status: BotStatusView) -> DutyOutcomeView | None:
    outcome = status.duty_outcome
    if outcome is None:
        return None
    label, explanation = outcome_card_copy(outcome.kind, outcome.reason_code)
    return DutyOutcomeView(
        kind=outcome.kind,
        reason_code=outcome.reason_code,
        label=label,
        explanation=explanation,
        recorded_at_ms=outcome.recorded_at_ms,
        run_id=outcome.run_id,
    )


def _build_health_card(
    status: BotStatusView,
    *,
    last_decision_at_ms: int | None,
    last_bar_at_ms: int | None,
    now_ms: int,
) -> BotHealthCard:
    desired_state = status.desired_state
    decision_stale = last_decision_at_ms is not None and decision_is_stale(last_decision_at_ms, now_ms)
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
    )


def _channel_views(clerk_status: ClerkStatus, now_ms: int) -> list[ChannelHealthView]:
    views: list[ChannelHealthView] = []
    for health in clerk_status.channel_healths or []:
        state = channel_state(healthy=health.healthy, observed_at_ms=health.observed_at_ms, now_ms=now_ms)
        copy = copy_for(state)
        views.append(
            ChannelHealthView(
                stream=health.stream,
                name=("IBKR market data" if health.stream == "market_data" else "Alpaca execution"),
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
    reason_code = hold_reason_for(active=hold.active, stored_code=hold.reason_code)
    reason_copy = copy_for(reason_code)
    reconciliation = clerk_status.latest_reconciliation
    verdict = reconciliation.verdict if reconciliation is not None else None
    freeze = clerk_status.freeze
    return ClerkCard(
        account_id=clerk_status.account_id,
        hold_active=hold.active,
        hold_reason=reason_code,
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


# Words one recorded decision explanation for the owner: the bot's strategy
# view bound to its deployed settings (``app.services.strategy_view``).
# ``None`` for a row the renderer cannot word ("values not shown").
ExplanationRenderer = Callable[[DecisionExplanationRecord], DecisionExplanationView | None]


_TRADED_FEED_LANE_STATES: Final[dict[str, Literal["no_trade", "late", "unhealthy", "conflict"]]] = {
    PRICING_BAR_NO_TRADE: "no_trade",
    PRICING_BAR_LATE: "late",
    PRICING_FEED_UNHEALTHY: "unhealthy",
    PRICING_BAR_CONFLICT: "conflict",
}


def traded_feed_lane(
    traded: TradedInstrumentView | None, decisions: Sequence[RecentDecisionView]
) -> TradedFeedLaneView | None:
    """The lane for what a bot trades, when that is not what it listens to (#2915).

    One event per entry the traded asset's price feed refused, at the close
    of the bar it was decided on. It is built from the decisions the panel
    already lists, so it reaches as far back as they do.
    """
    if traded is None:
        return None
    return TradedFeedLaneView(
        title="Market data",
        symbol=traded.symbol,
        events=[
            TradedFeedLaneEvent(
                at_ms=decision.decision_bar_close_ms or decision.recorded_at_ms,
                state=_TRADED_FEED_LANE_STATES[decision.reason_code],
                reason_code=decision.reason_code,
                sentence=decision.refusal_reason,
            )
            for decision in decisions
            if decision.reason_code in _TRADED_FEED_LANE_STATES
        ],
    )


def _recent_decision_views(
    receipts: list[DecisionReceipt],
    *,
    limit: int = 8,
    simulated: bool = False,
    authority_account_id: str | None = None,
    authority_kind: AuthorityKind | None = None,
    render_explanation: ExplanationRenderer | None = None,
) -> list[RecentDecisionView]:
    return [
        RecentDecisionView(
            seq=receipt.seq,
            recorded_at_ms=receipt.ts_ms,
            outcome=receipt.outcome,
            reason_code=receipt.reason_code,
            bar_ref=receipt.bar_ref,
            order_ref=receipt.order_ref or None,
            decision_id=receipt.decision_id,
            effect_operation_id=receipt.effect_operation_id,
            simulated=simulated,
            authority_account_id=authority_account_id,
            authority_kind=authority_kind,
            decision_bar_close_ms=receipt.decision_bar_close_ms,
            explanation=(
                None
                if receipt.explanation is None or render_explanation is None
                else render_explanation(receipt.explanation)
            ),
            refusal_reason=receipt.refusal_reason,
        )
        for receipt in reversed(receipts[-limit:])
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
            # The WAL predates causal capture and has no decision_id /
            # effect_operation_id columns at all, so these rows render the
            # absence explicitly rather than borrowing a guess from SQLite.
            source="legacy_simulated_wal",
            simulated=True,
            authority_account_id=row.authority_account_id,
            authority_kind=row.authority_kind,
        )
        for row in reversed(activity[-limit:])
    ]


def program_build_view_from_run_evidence(
    strategy_key: str,
    evidence: ProgramBuildRunEvidence,
) -> ProgramBuildAdmissionFact:
    """Run evidence (ADR 0043, 2026-09-30 amendment): the exact build proven and durably recorded
    when this run passed Start/Resume admission (#1728 Gap 2) — not a fresh
    re-check, which can drift from what is actually running if the
    qualification manifest or artifacts change underfoot after Start. The
    evidence this reads from is only ever written by
    ``BotBindingRepository._ensure_program_build_evidence`` after the same
    canonical ``prove_running_program_build`` closed ``state="PROVEN"``, so
    reconstructing that state here replays that verdict rather than making a
    new proof. Since #1828 the record also carries the wiring verdict, so the
    round trip is complete for any run started after that; a record written
    before it replays ``NOT_CHECKED``, which is a true statement about a run
    whose wiring was never written down rather than a claim about the wiring.
    """
    # `ProgramBuildAdmissionFact` refuses a PROVEN fact that reports a warning
    # without a next step, and rightly: a replay that shows one and no remedy
    # is a worse artifact than one that shows nothing. The live proof's own
    # remedies still apply to a frozen run.
    explanation, next_step = proven_build_copy(
        wiring=evidence.wiring,
        corpus_coverage=evidence.corpus_coverage,
        matched=(
            "The Signal Program build proven and recorded when this run started "
            "matches its golden qualification receipt."
        ),
        drifted=(
            "The Signal Program math proven and recorded when this run started "
            "matches its golden qualification receipt, but its strategy wiring "
            "had already changed since that receipt was minted."
        ),
    )
    return ProgramBuildAdmissionFact(
        state="PROVEN",
        program_key=strategy_key,
        program_version=evidence.program_version,
        golden_trace_root=evidence.golden_trace_root,
        running_artifact_digest=evidence.running_artifact_digest,
        qualification_receipt_hash=evidence.qualification_receipt_hash,
        verified_at_ms=evidence.verified_at_ms,
        # The verdicts this run actually started under (#1828, ADR 0054).
        # Evidence written before either was recorded carries `NOT_CHECKED`,
        # which is a true statement about that run rather than a claim about
        # it -- a frozen replay silently reporting MATCHED or COVERED would be
        # the exact false assurance the #1735 split was added to prevent.
        wiring=evidence.wiring,
        corpus_coverage=evidence.corpus_coverage,
        evidence_refs=(
            f"signal-program-seal:{evidence.sealed_program_hash}",
            f"program-build-receipt:{evidence.qualification_receipt_hash}",
            f"program-build-digest:{evidence.running_artifact_digest}",
        ),
        explanation=explanation,
        next_step=next_step,
        verification="frozen_run_evidence",
    )


def _execution_policy(mode: str) -> str:
    policies = {
        "dry_run": (
            "Dry Run. Real market data produces clearly simulated decisions "
            "and fills; broker writes are impossible."
        ),
        "log_only": (
            "Observation only. Decisions are recorded, but the Clerk will not place orders."
        ),
        "trade": "Broker execution through the Clerk. Only the Clerk may submit, cancel, or reduce broker orders.",
    }
    return policies[mode]


def _mode_decision_views(
    status: BotStatusView,
    decision_receipts: list[DecisionReceipt],
    dry_run_activity: list[DryRunActivity],
    *,
    authority_account_id: str,
    authority_kind: AuthorityKind,
    render_explanation: ExplanationRenderer | None = None,
) -> list[RecentDecisionView]:
    """Project real or simulated decisions behind one explicit mode boundary.

    Dry Run runs through its own isolated synthetic Clerk authority (PR
    #1722) and durably records every decision through the identical SQLite
    decision-receipt path Paper uses — the caller already reads
    ``decision_receipts`` (each row already carrying its own causal identity)
    from that Dry-Run-scoped authority (see
    ``sqlite_panel_source.read_sqlite_decision_receipts`` and the synthetic
    facade selected by ``panel_data_source._panel_authority_for_binding``),
    so the causal links (``decision_id``/``effect_operation_id``)
    render for Dry Run exactly like Paper whenever that SQLite evidence
    exists. Only a Dry Run instance with no SQLite decision-receipt evidence
    at all — one that never ran under the governed synthetic Clerk — falls
    back to the legacy simulated-activity WAL, and that fallback's
    causal-link fields still render the explicit absence (``None``), never a
    guess borrowed from the SQLite path.

    The panel's fills are not authored here: every fill row is
    ``sqlite_panel_adapter.adapt_sqlite_panel``'s, which stamps and judges
    them (``panel_authority_guard.reject_mixed_authority``) beside these
    decisions.

    ``authority_account_id``/``authority_kind`` name the one Clerk account
    authority ``build_panel`` resolved this evidence cut from (issue #1729
    AC #8). Every branch below stamps every row with these same values —
    except the legacy WAL fallback, whose rows carry authority metadata
    baked in at ``DryRunActivity`` construction time. Stamping uniformly
    rather than trusting each branch to agree by construction is exactly
    what lets the adapter's guard catch a real divergence (e.g. a caller
    accidentally handing this projector another bot's synthetic activity)
    instead of only what convention makes probable.
    """
    if status.mode == "dry_run":
        if decision_receipts:
            return _recent_decision_views(
                decision_receipts,
                simulated=True,
                authority_account_id=authority_account_id,
                authority_kind=authority_kind,
                render_explanation=render_explanation,
            )
        return _dry_run_decision_views(dry_run_activity)
    # A shadow authority's fills are synthesized too (ADR 0059 D2), so
    # "not Dry Run" no longer implies "real": the row's own authority
    # kind decides, and the two worlds that synthesize are named once in
    # ``SIMULATED_AUTHORITY_KINDS``.
    return _recent_decision_views(
        decision_receipts,
        simulated=authority_kind in SIMULATED_AUTHORITY_KINDS,
        authority_account_id=authority_account_id,
        authority_kind=authority_kind,
        render_explanation=render_explanation,
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


def _decision_last_bar_at_ms(decision: DecisionReceipt | None) -> int | None:
    if decision is None:
        return None
    _symbol, separator, candidate = decision.bar_ref.rpartition("@")
    if separator and candidate.isdecimal():
        return int(candidate)
    return None


def _mission_verdict(
    status: BotStatusView,
    clerk: ClerkCard,
    *,
    channel_health: ChannelHealthEvaluation,
    now_ms: int,
) -> MissionVerdictView:
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
        return MissionVerdictView(
            state="working",
            label="Working",
            explanation="The runtime is on duty and the Clerk has not raised an account gate.",
            next_action="Monitor decisions, fills, and custody evidence.",
            evaluated_at_ms=now_ms,
        )
    return MissionVerdictView(
        state="off_duty",
        label="Off duty",
        explanation="The bot is not evaluating bars. Remaining orders and exposure stay with this deployment.",
        next_action="Use Deploy again to review a fresh deployment, or Flatten remaining exposure.",
        evaluated_at_ms=now_ms,
    )


#: A running bot's stop command: the SQLite recovery executor's
#: ``stop_bot_decisions`` (#2605 retired the runner's generic ``stop``).
_RUNNING_STOP_ACTION_ID: ActionId = "stop_bot_decisions"


def select_primary_action(
    actions: list[PanelAction],
    health: BotHealthCard,
    *,
    holds_position: bool = False,
    recovery_primary_action_id: ActionId | None = None,
) -> ActionId | None:
    """Author the one backend-selected command for this bot's page (#1665).

    A stopped bot that still holds shares has none: its page's stranded-position
    warning owns the one action there, Flatten, and no other command may sit
    beside it in the header (#2560; review B4). Otherwise one precedence
    rule (ADR 0027): a recovery capability the custody policy marks as this
    bot's cure outranks the routine lifecycle command, because the routine
    command may not even reflect the bot's true state. The caller passes only
    a *diagnostic* recovery primary -- one that is this bot's cure, not an
    always-available refresh. Otherwise a running bot's primary command is its
    stop, and a stopped bot has none: its page offers Deploy again, which is
    navigation, not a panel command. A candidate that is not presented fails
    closed to ``None`` rather than being guessed from ``health``.
    """
    if not health.running and holds_position:
        return None
    action_ids = {action.action_id for action in actions}
    if recovery_primary_action_id is not None and recovery_primary_action_id in action_ids:
        return recovery_primary_action_id
    if not health.running:
        return None
    return _RUNNING_STOP_ACTION_ID if _RUNNING_STOP_ACTION_ID in action_ids else None


def open_pnl_fields(exact_open_pnl: Decimal | None) -> dict[str, object]:
    """A bot's open P&L with the words the owner reads it in (#2560 D12).

    The one writer of ``BotPanelView.open_pnl_usd`` and
    ``open_pnl_direction``: half-even display cents rounded once from
    canonical FIFO's exact open valuation (``EconomicSnapshot.exact_open_pnl``),
    never from its float view, whose own rounding can cross a half cent
    (#2556); and the direction those cents point, so a loss that rounds to
    "0.00" reads flat rather than red. ``open_pnl`` is the float view.
    ``None`` for all three when the figure is unknown (no current price).
    """
    if exact_open_pnl is None:
        return {"open_pnl": None, "open_pnl_usd": None, "open_pnl_direction": None}
    cents = display_cents(exact_open_pnl)
    direction = "gain" if cents > 0 else "loss" if cents < 0 else "flat"
    return {"open_pnl": float(exact_open_pnl), "open_pnl_usd": dollars(cents), "open_pnl_direction": direction}


def build_panel(
    status: BotStatusView,
    clerk_status: ClerkStatus,
    *,
    account_id: str,
    exposure: dict[str, float],
    fills_today: int | None,
    realized_pnl_today: float | None,
    exact_open_pnl: Decimal | None,
    latest_decision: DecisionReceipt | None,
    last_bar_at_ms: int | None,
    now_ms: int,
    selected_transaction_ref: str | None = None,
    recent_decisions: list[DecisionReceipt] | None = None,
    sealed_program: SealedBotProgram | None = None,
    program_build: ProgramBuildAdmissionFact,
    dry_run_activity: list[DryRunActivity] | None = None,
    authority_account_id: str | None = None,
    account_mode: Literal["paper", "live"],
    market_pulse: MarketPulseView,
    traded_market_pulse: MarketPulseView | None = None,
    bot_status: BotHistoryStatus,
    feed_continuity_events: Sequence[RetainedContinuityEvent] | None = (),
    feed_continuity_run_id: str | None = None,
    warmup_join: RetainedWarmupJoin | None = None,
    startup_join: RetainedStartupJoin | None = None,
    end: BotEndView | None = None,
    render_explanation: ExplanationRenderer | None = None,
) -> BotPanelView:
    """Build the full panel view for one bot.

    ``selected_transaction_ref`` is the transaction the rail renders; the
    adapter resolves it.

    ``authority_account_id`` names the exact Clerk account authority this
    evidence cut (``recent_decisions``) was actually read from. Callers with
    a selected SQLite facade (``panel_data_source.
    _get_panel_with_entries_from_authority``) pass the facade's real
    ``account_id`` so the row-level authority label can never drift from
    which physically account-scoped repository the evidence came from — the
    same account-namespace check that gates a repository open
    (``ClerkSqliteRepository.open``/``DatabaseIdentityMismatch``) also backs
    this label. Callers without a selected facade (every direct test in
    ``tests/broker/v2panel/test_panel_projection.py``) omit it, and it falls
    back to the Dry Run synthetic authority implied by ``status.mode`` /
    ``status.strategy_instance_id``, or to ``account_id`` otherwise.

    ``account_mode`` is the mode that authority learned from the broker (the
    facade's ``account_mode``). It decides ``real_paper`` against
    ``real_live`` on every decision row, and is required: a default
    would label a live account's rows paper (#2823).

    ``bot_status`` is where the bot is now by its custody's own facts
    (``catalog_projection_service.custody_bot_status``); the projection only
    carries it.

    ``program_build`` is required and must be the caller's real evidence cut
    (``panel_data_source._program_build_for_display`` for the live router
    path). This projection must never guess it: ADR 0043 reserves
    ``state="NOT_APPLICABLE"`` for a strategy with no registered Signal
    Program at all, so a caller that simply has no build evidence yet (e.g.
    no per-run record and a fresh ``prove_running_program_build`` re-check)
    must pass the honest ``state="UNPROVEN"`` fact instead — never omit the
    argument to get a fabricated "not applicable" reading.
    """
    # Ascending by seq (oldest first).
    decision_receipts = recent_decisions or ([latest_decision] if latest_decision else [])

    activity = dry_run_activity or []
    health_last_decision_at_ms = (
        activity[-1].recorded_at_ms
        if status.mode == "dry_run" and activity
        else (latest_decision.ts_ms if latest_decision is not None else None)
    )
    health_last_bar_at_ms = (
        _dry_run_last_bar_at_ms(activity)
        if status.mode == "dry_run"
        # A decision receipt is the only durable proof that a bar reached the
        # strategy evaluator. A later fill is execution activity, not proof
        # that a later bar was evaluated, so it cannot replace this clock.
        else _decision_last_bar_at_ms(latest_decision) or last_bar_at_ms
    )
    clerk = build_clerk_card(clerk_status, now_ms)
    health = _build_health_card(
        status,
        last_decision_at_ms=health_last_decision_at_ms,
        last_bar_at_ms=health_last_bar_at_ms,
        now_ms=now_ms,
    )

    channel_health = evaluate_channel_health(clerk_status.channel_healths, now_ms)
    resolved_authority_account_id = authority_account_id or default_authority_account_id(
        status, account_id
    )
    decision_views = _mode_decision_views(
        status,
        decision_receipts,
        activity,
        authority_account_id=resolved_authority_account_id,
        authority_kind=authority_kind_for_account(
            resolved_authority_account_id, account_mode=account_mode
        ),
        render_explanation=render_explanation,
    )

    return BotPanelView(
        bot_kind=status.bot_kind,
        options_bot=status.options_bot,
        strategy_instance_id=status.strategy_instance_id,
        strategy_key=status.strategy_key,
        strategy_label=(
            status.strategy_label
            or status.strategy_key.replace("_", " ").replace("-", " ").title()
        ),
        experimental_notice=strategy_experimental_notice(status.strategy_key),
        broker=status.broker,
        account_id=account_id,
        symbol=status.symbol,
        traded=status.traded,
        traded_asset_warning=(
            None if status.traded is None or status.bot_kind == "options" else UntestedTradedAssetWarning.for_symbols(
                status.symbol, status.traded.symbol, acceptance_required=False,
            )
        ),
        traded_asset_acknowledgement=status.traded_asset_acknowledgement,
        mode=status.mode,
        sealed_program=sealed_program,
        program_build=program_build,
        updated_at_ms=now_ms,
        revision=0,
        market_pulse=market_pulse,
        traded_market_pulse=traded_market_pulse,
        traded_feed_lane=traded_feed_lane(status.traded, decision_views),
        feed_continuity=build_feed_continuity(
            feed_continuity_events,
            run_id=(
                feed_continuity_run_id
                or status.active_run_id
                or (status.duty_outcome.run_id if status.duty_outcome is not None else None)
            ),
            latest_bar_at_ms=market_pulse.latest_bar_at_ms,
            now_ms=now_ms,
        ),
        warmup_join=build_warmup_join(warmup_join),
        startup_join=build_startup_join(startup_join, running=status.running),
        mission_verdict=_mission_verdict(
            status,
            clerk,
            channel_health=channel_health,
            now_ms=now_ms,
        ),
        execution_policy=_execution_policy(status.mode),
        health=health,
        clerk=clerk,
        # The rail's stations, the revision, the journal tail, the working
        # orders and the fills are the SQLite adapter's too
        # (``adapt_sqlite_panel``).
        rail=TransactionRail(transaction_ref=selected_transaction_ref or None, stations=[]),
        journal_tail_ref="",
        journal_tail_seq=None,
        # Commands are the SQLite adapter's (``adapt_sqlite_panel``), which
        # every served panel passes through: Clear's archive and the Clerk's
        # recovery catalog, with their readiness checks and primary command.
        actions=[],
        primary_action=None,
        readiness_checks=[],
        readiness_ready_count=0,
        readiness_blocked_count=0,
        status=bot_status,
        end=end,
        exposure=exposure,
        working_orders=[],
        recent_decisions=decision_views,
        recent_fills=[],
        fills_today=fills_today,
        realized_pnl_today=realized_pnl_today,
        **open_pnl_fields(exact_open_pnl),
    )
