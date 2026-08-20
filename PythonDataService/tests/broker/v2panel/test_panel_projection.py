"""Tests for the panel projection (S1, spec §7).

Composes the health/clerk cards + six-station rail + presented actions from
journal fixtures, and pins the revision determinism and the narrowed
desired_state (never PAUSED).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

import pytest
from pydantic import ValidationError

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ChannelHealth,
    ClerkStatus,
    HoldState,
    ReconciliationSummary,
)
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedCommand,
    ProjectedOperation,
    ProjectedOrder,
    ProjectedPosition,
    ProjectedReconciliation,
    ProjectedRun,
    ProjectionGuidance,
    RecoveryCapability,
)
from app.broker.contract.models import OrderSide
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotHealthCard, BotPanelView, MarketPulseView, PanelAction
from app.schemas.live_runs import BotDutyOutcomeView
from app.schemas.operator_blocker import AccountOperatorPosture
from app.schemas.run_admission import RunAdmissionDecision, RunAdmissionFactAges
from app.services.bot_dry_run import DryRunActivity
from app.services.broker_v2_panel.panel_projection_service import (
    build_panel,
    compute_revision,
    select_primary_action_by_lens,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    adapt_sqlite_panel,
    build_sqlite_catalog,
)
from app.services.sqlite_clerk_compat import sqlite_clerk_status
from tests.broker.v2panel.fixtures import (
    ACCT,
    SID,
    decision_receipt,
    fill_entry,
    intent_entry,
    reconciliation_entry,
    submit_acked_entry,
)

_NOW = 1_700_000_000_000

_MARKET_PULSE = MarketPulseView(
    session="OPEN",
    market_state="TRADABLE",
    market_liveness_reason="Fresh test evidence proves tradability.",
    market_liveness_observed_at_ms=_NOW,
    halted_symbol=None,
    feed_state="LIVE",
    latest_bar_at_ms=_NOW - 60_000,
    age_ms=60_000,
    source="test-feed",
    expected_cadence_ms=60_000,
    headline="Market data live",
    explanation="The test feed is current.",
    next_step=None,
    attention_required=False,
    observed_at_ms=_NOW,
)


def _status(
    *,
    desired_state: str | None = None,
    running: bool = True,
    carryover_policy: str = "FORBID",
    carryover_account_policy_enabled: bool = True,
    checkpoint_exposure: dict[str, float] | None = None,
    checkpoint_matches: bool = False,
    mode: Literal["log_only", "dry_run", "trade"] = "log_only",
) -> BotStatusView:
    resolved_desired_state = desired_state or ("RUNNING" if running else "STOPPED")
    return BotStatusView(
        strategy_instance_id=SID,
        broker="alpaca",
        symbol="SPY",
        mode=mode,
        quantity=1,
        carryover_policy=carryover_policy,  # type: ignore[arg-type]
        carryover_account_policy_enabled=carryover_account_policy_enabled,
        carryover_checkpoint_exposure=checkpoint_exposure or {},
        carryover_checkpoint_config_matches=checkpoint_matches,
        running=running,
        phase="ON_DUTY" if running else "OFF_DUTY",
        desired_state=resolved_desired_state,  # type: ignore[arg-type]
        active_run_id="r1" if running else None,
        duty_outcome=None,
        binding_created_at_ms=1,
        last_transition_at_ms=2,
    )


def _clerk_status(
    *,
    hold: bool = False,
    hold_code: str | None = None,
    healthy: bool = True,
    freeze: AccountFreezeState | None = None,
    reconciliation_verdict: str = "clean",
    outstanding_intents: int = 0,
) -> ClerkStatus:
    return ClerkStatus(
        broker="alpaca",
        account_id=ACCT,
        hold=HoldState(
            active=hold,
            reason_code=hold_code,
            reason="fixture" if hold else None,
            since_ms=_NOW - 100 if hold else None,
        ),
        freeze=freeze or AccountFreezeState(),
        latest_reconciliation=ReconciliationSummary(
            verdict=reconciliation_verdict,  # type: ignore[arg-type]
            recorded_at_ms=_NOW - 200,
        ),
        outstanding_intents=outstanding_intents,
        observed_at_ms=_NOW,
        channel_healths=[
            ChannelHealth(stream="market_data", healthy=healthy, reason="", observed_at_ms=_NOW - 10),
            ChannelHealth(stream="execution", healthy=healthy, reason="", observed_at_ms=_NOW - 10),
        ],
        operator_posture=AccountOperatorPosture(
            condition=None,
            account_desk=None,
            fleet_roster=None,
            status_headline="Account Clerk custody is healthy",
            status_detail=None,
        ),
    )


def _panel(
    status: BotStatusView,
    clerk: ClerkStatus,
    entries: list,
    decision=None,
    *,
    exposure: dict[str, float] | None = None,
    resume_allowed: bool | None = None,
    dry_run_activity: list[DryRunActivity] | None = None,
    last_bar_at_ms: int | None = _NOW - 300,
    admission_evidence_refs: tuple[str, ...] = ("test:resume-admission",),
):
    resolved_exposure = {"SPY": 100.0} if exposure is None else exposure
    if resume_allowed is None:
        resume_allowed = (
            not status.running
            and not clerk.hold.active
            and not clerk.freeze.active
            and (
                not any(abs(quantity) > 0 for quantity in resolved_exposure.values())
                or (
                    status.carryover_policy == "ALLOW"
                    and status.carryover_account_policy_enabled
                    and status.carryover_checkpoint_config_matches
                    and status.carryover_checkpoint_exposure == resolved_exposure
                )
            )
        )
    resume_admission = RunAdmissionDecision(
        operation="RESUME",
        allowed=resume_allowed,
        reason_code="RESUME_ADMITTED" if resume_allowed else "RESUME_TEST_BLOCKED",
        explanation=(
            "The runner and Clerk admit this new run."
            if resume_allowed
            else "The runner and Clerk block this new run."
        ),
        next_step=None,
        strategy_instance_id=status.strategy_instance_id,
        proposed_run_id="run-proposed",
        configuration_hash="a" * 64,
        account_id=ACCT,
        evaluated_at_ms=_NOW,
        fact_ages_ms=RunAdmissionFactAges(
            runtime=0,
            process=0,
            market_data=0,
            market_liveness=0,
            clerk=0,
        ),
        evidence_refs=admission_evidence_refs,
    )
    return build_panel(
        status,
        clerk,
        entries,
        account_id=ACCT,
        exposure=resolved_exposure,
        fills_today=0,
        realized_pnl_today=0.0,
        open_pnl=None,
        latest_decision=decision,
        last_bar_at_ms=last_bar_at_ms,
        journal_tail_ref=f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        flatten_supported=True,
        now_ms=_NOW,
        resume_admission=resume_admission,
        dry_run_activity=dry_run_activity,
        market_pulse=_MARKET_PULSE,
    )


def test_sqlite_adapter_replaces_legacy_custody_with_fold_projection() -> None:
    base = _panel(
        _status(),
        _clerk_status(),
        [fill_entry(sid=SID, intent="legacy", ts_ms=_NOW - 1_000)],
    )
    command = ProjectedCommand(
        command_id="command:enter",
        kind="EFFECT",
        action="ENTER",
        state="in_progress",
        run_id="run:1",
        receipt_id=None,
        created_at_ms=_NOW - 500,
        updated_at_ms=_NOW - 400,
    )
    operation = ProjectedOperation(
        effect_operation_id="effect:enter",
        kind="ENTER",
        state="in_progress",
        custody_owner="ACCOUNT_CLERK",
        strategy_instance_id=SID,
        run_id="run:1",
        created_at_ms=_NOW - 500,
        updated_at_ms=_NOW - 300,
        latest_transition_sequence=9,
        transition_count=3,
        terminal_receipt_id=None,
        command=command,
        orders=(
            ProjectedOrder(
                order_ref="order:enter",
                client_order_id="order:enter",
                broker_order_id="broker-1",
                role="ENTRY",
                broker_state="accepted",
                submitted_at_ms=_NOW - 350,
                updated_at_ms=_NOW - 300,
            ),
        ),
    )
    action = RecoveryCapability(
        action_id="reconcile_now",
        label="Reconcile now",
        explanation="Compare durable custody with Alpaca.",
        available=True,
        unavailable_reason_code=None,
        unavailable_reason=None,
        scope="CUSTODY_SUBJECT",
        freshness="not_required",
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Run the comparison.",
        concurrency_token="sqlite-token",
        execution_ref=None,
        mutation=True,
        primary=True,
    )
    projection = ClerkProjection(
        account_id=ACCT,
        strategy_instance_id=SID,
        authority_generation=4,
        db_identity_token="db-4",
        authority_health="healthy",
        authority_health_reason=None,
        control_revision=17,
        custody_owner="ACCOUNT_CLERK",
        runs=(
            ProjectedRun(
                run_id="run:1",
                strategy_instance_id=SID,
                lifecycle_run_id="lifecycle-1",
                state="ACTIVE",
                started_at_ms=_NOW - 1_000,
                stopped_at_ms=None,
            ),
        ),
        commands=(command,),
        operations=(operation,),
        positions=(
            ProjectedPosition(
                strategy_instance_id=SID,
                symbol="SPY",
                attributed_qty=2.0,
                updated_at_ms=_NOW - 200,
            ),
        ),
        holds=(),
        uncertainties=(),
        latest_reconciliation=None,
        terminal_receipts=(),
        guidance=ProjectionGuidance(
            headline="Account Clerk custody is healthy",
            explanation="SQLite has current custody truth.",
            scope="CUSTODY_SUBJECT",
            impact="Normal Clerk-governed controls remain available.",
            custody_owner="ACCOUNT_CLERK",
            may_create_exposure=True,
            available_safety_actions=("Reconcile now",),
            action_required=False,
            next_step="No recovery action is required.",
        ),
        recovery_actions=(action,),
        generated_at_ms=_NOW,
    )

    adapted = adapt_sqlite_panel(base, projection)

    assert adapted.revision == 17
    assert adapted.exposure == {"SPY": 2.0}
    assert adapted.journal_tail_ref.endswith(f"/{SID}/timeline")
    assert [item.action_id for item in adapted.actions] == ["reconcile_now"]
    assert adapted.actions[0].concurrency_token == "sqlite-token"
    assert adapted.readiness_checks[0].scope == "bot"
    assert adapted.rail.transaction_ref == "effect:enter"
    assert adapted.working_orders[0].order_ref == "order:enter"
    assert adapted.working_orders[0].filled_quantity is None
    assert adapted.recent_fills == []


def test_sqlite_adapter_projects_execution_economics_and_durable_working_order_details() -> None:
    """The active SQLite panel renders its one authoritative economic cut."""
    base = _panel(_status(), _clerk_status(), [])
    projected_order = ProjectedOrder(
        order_ref="order:googl",
        client_order_id="order:googl",
        broker_order_id="broker-googl",
        role="ENTRY",
        broker_state="partially_filled",
        submitted_at_ms=_NOW - 900,
        updated_at_ms=_NOW - 300,
        symbol="GOOGL",
        side="buy",
        quantity=5.0,
        filled_quantity=2.0,
    )
    projection = _rail_projection(orders=(projected_order,))
    fills = (
        FillRecord(
            account_id=ACCT,
            sid=SID,
            intent_id="googl-enter",
            order_ref="order:googl",
            event_key="exec-googl-3",
            symbol="GOOGL",
            side=OrderSide.SELL,
            quantity=2.0,
            fill_price=195.0,
            filled_at_ms=_NOW - 100,
            fee=None,
        ),
        FillRecord(
            account_id=ACCT,
            sid=SID,
            intent_id="googl-enter",
            order_ref="order:googl",
            event_key="exec-googl-2",
            symbol="GOOGL",
            side=OrderSide.BUY,
            quantity=1.0,
            fill_price=185.0,
            filled_at_ms=_NOW - 200,
            fee=None,
        ),
        FillRecord(
            account_id=ACCT,
            sid=SID,
            intent_id="googl-enter",
            order_ref="order:googl",
            event_key="exec-googl-1",
            symbol="GOOGL",
            side=OrderSide.BUY,
            quantity=1.0,
            fill_price=180.0,
            filled_at_ms=_NOW - 300,
            fee=None,
        ),
    )
    economics = EconomicSnapshot(
        account_id=ACCT,
        strategy_instance_id=SID,
        authority_generation=4,
        control_revision=projection.control_revision,
        session_open_ms=_NOW - 86_400_000,
        session_close_ms=_NOW + 86_400_000,
        recent_fills=fills,
        fills_today=3,
        exposure={"GOOGL": 0.0},
        realized_pnl_today=25.0,
        open_pnl=0.0,
        marks_complete=True,
        mark_observed_at_ms={},
        fee_fidelity="not_reported",
        execution_coverage="complete",
        last_activity_at_ms=_NOW - 100,
    )

    adapted = adapt_sqlite_panel(base, projection, economics=economics)

    assert [(fill.symbol, fill.side, fill.quantity, fill.price) for fill in adapted.recent_fills] == [
        ("GOOGL", "sell", 2.0, 195.0),
        ("GOOGL", "buy", 1.0, 185.0),
        ("GOOGL", "buy", 1.0, 180.0),
    ]
    assert adapted.fills_today == 3
    assert adapted.realized_pnl_today == 25.0
    assert adapted.open_pnl == 0.0
    assert [order.model_dump() for order in adapted.working_orders] == [
        {
            "order_ref": "order:googl",
            "broker_order_id": "broker-googl",
            "symbol": "GOOGL",
            "side": "buy",
            "quantity": 5.0,
            "filled_quantity": 2.0,
            "status": "partially_filled",
            "observed_at_ms": _NOW - 300,
        }
    ]


def test_adapt_sqlite_panel_omits_sub_epsilon_exposure() -> None:
    projection = replace(
        _rail_projection(orders=()),
        positions=(
            ProjectedPosition(
                strategy_instance_id=SID,
                symbol="SPY",
                attributed_qty=1e-12,
                updated_at_ms=_NOW - 200,
            ),
        ),
    )

    adapted = adapt_sqlite_panel(
        _panel(_status(running=False), _clerk_status(), [], exposure={"SPY": 1e-12}),
        projection,
    )

    assert adapted.exposure == {}


def test_build_sqlite_catalog_omits_sub_epsilon_exposure_and_reports_flat() -> None:
    status = _status(running=False).model_copy(
        update={"strategy_label": "Deployment Validation"}
    )
    projection = replace(_rail_projection(orders=()), runs=(), commands=(), operations=())
    economics = EconomicSnapshot(
        account_id=ACCT,
        strategy_instance_id=SID,
        authority_generation=4,
        control_revision=projection.control_revision,
        session_open_ms=_NOW - 3_600_000,
        session_close_ms=_NOW + 3_600_000,
        recent_fills=(),
        fills_today=0,
        exposure={"SPY": 1e-12},
        realized_pnl_today=0.0,
        open_pnl=0.0,
        marks_complete=True,
        mark_observed_at_ms={"SPY": _NOW},
        fee_fidelity="reported",
        execution_coverage="complete",
        last_activity_at_ms=_NOW,
    )

    catalog = build_sqlite_catalog(
        [status],
        projections={SID: projection},
        economic_rollups={SID: economics},
        account_id=ACCT,
    )

    assert catalog[0].exposure == {}
    assert catalog[0].status_explanation == "Off duty and flat."


def test_build_panel_uses_flat_resume_copy_for_sub_epsilon_exposure() -> None:
    panel = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={"SPY": 1e-12},
        resume_allowed=True,
    )

    assert panel.health.resume_label == "Flat Resume ready"


def _rail_projection(
    *,
    orders: tuple[ProjectedOrder, ...],
    operation_state: str = "in_progress",
    terminal_receipt_id: str | None = None,
    latest_reconciliation: ProjectedReconciliation | None = None,
) -> ClerkProjection:
    """A minimal single-operation projection for `_transaction_rail` station tests."""
    command = ProjectedCommand(
        command_id="command:enter",
        kind="EFFECT",
        action="ENTER",
        state=operation_state,
        run_id="run:1",
        receipt_id=None,
        created_at_ms=_NOW - 500,
        updated_at_ms=_NOW - 400,
    )
    operation = ProjectedOperation(
        effect_operation_id="effect:enter",
        kind="ENTER",
        state=operation_state,
        custody_owner="ACCOUNT_CLERK",
        strategy_instance_id=SID,
        run_id="run:1",
        created_at_ms=_NOW - 500,
        updated_at_ms=_NOW - 300,
        latest_transition_sequence=9,
        transition_count=3,
        terminal_receipt_id=terminal_receipt_id,
        command=command,
        orders=orders,
    )
    return ClerkProjection(
        account_id=ACCT,
        strategy_instance_id=SID,
        authority_generation=4,
        db_identity_token="db-4",
        authority_health="healthy",
        authority_health_reason=None,
        control_revision=17,
        custody_owner="ACCOUNT_CLERK",
        runs=(
            ProjectedRun(
                run_id="run:1",
                strategy_instance_id=SID,
                lifecycle_run_id="lifecycle-1",
                state="ACTIVE",
                started_at_ms=_NOW - 1_000,
                stopped_at_ms=None,
            ),
        ),
        commands=(command,),
        operations=(operation,),
        positions=(),
        holds=(),
        uncertainties=(),
        latest_reconciliation=latest_reconciliation,
        terminal_receipts=(),
        guidance=ProjectionGuidance(
            headline="Account Clerk custody is healthy",
            explanation="SQLite has current custody truth.",
            scope="CUSTODY_SUBJECT",
            impact="Normal Clerk-governed controls remain available.",
            custody_owner="ACCOUNT_CLERK",
            may_create_exposure=True,
            available_safety_actions=(),
            action_required=False,
            next_step="No recovery action is required.",
        ),
        recovery_actions=(),
        generated_at_ms=_NOW,
    )


def test_sqlite_status_does_not_count_a_filled_entry_as_outstanding() -> None:
    projection = _rail_projection(
        orders=(
            ProjectedOrder(
                order_ref="order:enter",
                client_order_id="order:enter",
                broker_order_id="broker-1",
                role="ENTRY",
                broker_state="filled",
                submitted_at_ms=_NOW - 350,
                updated_at_ms=_NOW - 300,
            ),
        ),
    )

    assert sqlite_clerk_status(projection).outstanding_intents == 0


def test_sqlite_status_counts_a_working_entry_as_outstanding() -> None:
    projection = _rail_projection(
        orders=(
            ProjectedOrder(
                order_ref="order:enter",
                client_order_id="order:enter",
                broker_order_id="broker-1",
                role="ENTRY",
                broker_state="accepted",
                submitted_at_ms=_NOW - 350,
                updated_at_ms=_NOW - 300,
            ),
        ),
    )

    assert sqlite_clerk_status(projection).outstanding_intents == 1


def test_trade_health_uses_decision_bar_reference_for_last_evaluated_bar() -> None:
    decision = decision_receipt(
        seq=1,
        ts_ms=_NOW - 100,
        outcome="no_action",
        reason_code="NO_ACTION",
        bar_ref=f"SPY@{_NOW - 60_000}",
    )

    panel = _panel(
        _status(mode="trade"),
        _clerk_status(),
        [],
        decision,
        last_bar_at_ms=None,
    )

    assert panel.health.last_decision_at_ms == _NOW - 100
    assert panel.health.last_bar_at_ms == _NOW - 60_000


def test_trade_health_does_not_relabel_a_later_fill_as_a_evaluated_bar() -> None:
    decision = decision_receipt(
        seq=1,
        ts_ms=_NOW - 100,
        outcome="no_action",
        reason_code="NO_ACTION",
        bar_ref=f"SPY@{_NOW - 60_000}",
    )

    panel = _panel(
        _status(mode="trade"),
        _clerk_status(),
        [],
        decision,
        last_bar_at_ms=_NOW - 1,
    )

    assert panel.health.last_bar_at_ms == _NOW - 60_000


def _station_states(adapted: BotPanelView) -> dict[str, str]:
    return {station.station_id: station.state for station in adapted.rail.stations}


def test_sqlite_adapter_preserves_stopped_bot_resume_with_sqlite_recovery_actions() -> None:
    """#1410: activating SQLite must not remove the admitted Resume control."""
    base = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
    )
    recovery_action = RecoveryCapability(
        action_id="reconcile_now",
        label="Reconcile now",
        explanation="Compare durable custody with Alpaca.",
        available=True,
        unavailable_reason_code=None,
        unavailable_reason=None,
        scope="CUSTODY_SUBJECT",
        freshness="not_required",
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Run the comparison.",
        concurrency_token="sqlite-token",
        execution_ref=None,
        mutation=True,
        primary=True,
    )
    projection = replace(
        _rail_projection(orders=()),
        runs=(),
        commands=(),
        operations=(),
        recovery_actions=(recovery_action,),
    )

    adapted = adapt_sqlite_panel(base, projection)

    actions = {action.action_id: action for action in adapted.actions}
    assert list(actions) == ["resume", "reconcile_now"]
    assert actions["resume"].enabled is True
    assert actions["reconcile_now"].concurrency_token == "sqlite-token"
    assert len(adapted.actions) == len(actions)


def test_sqlite_adapter_keeps_unavailable_custody_subject_blockers_on_bot_scope() -> None:
    capability = RecoveryCapability(
        action_id="reconcile_now",
        label="Reconcile now",
        explanation="Compare durable custody with Alpaca.",
        available=False,
        unavailable_reason_code="EVIDENCE_STALE",
        unavailable_reason="Fresh custody evidence is required.",
        scope="CUSTODY_SUBJECT",
        freshness="stale",
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Refresh the custody evidence.",
        concurrency_token="sqlite-token",
        execution_ref=None,
        mutation=True,
        primary=True,
    )
    projection = replace(
        _rail_projection(orders=()),
        recovery_actions=(capability,),
    )

    adapted = adapt_sqlite_panel(_panel(_status(), _clerk_status(), []), projection)

    action = _action(adapted, "reconcile_now")
    assert action.blockers[0].condition.scope == "bot"
    assert adapted.readiness_checks[0].scope == "bot"


def test_reconciled_station_requires_resolved_success_outcome() -> None:
    """#1396 P1: a matching reconciliation row must not close the custody
    chain unless its outcome is RESOLVED_SUCCESS — STILL_UNKNOWN and
    RESOLVED_FAILURE attempts must not present as satisfied."""
    base = _panel(_status(), _clerk_status(), [])
    orders = (
        ProjectedOrder(
            order_ref="order:enter",
            client_order_id="order:enter",
            broker_order_id="broker-1",
            role="ENTRY",
            broker_state="accepted",
            submitted_at_ms=_NOW - 350,
            updated_at_ms=_NOW - 300,
        ),
    )
    unresolved_reconciliation = ProjectedReconciliation(
        reconciliation_id="recon-1",
        effect_operation_id="effect:enter",
        order_ref="order:enter",
        trigger="AUTOMATIC",
        attempted_at_ms=_NOW - 100,
        outcome="STILL_UNKNOWN",
        evidence_age_ms=100,
        evidence_refs=(),
    )
    unresolved = adapt_sqlite_panel(
        base,
        _rail_projection(orders=orders, latest_reconciliation=unresolved_reconciliation),
    )
    assert _station_states(unresolved)["RECONCILED"] != "satisfied"

    resolved_reconciliation = replace(unresolved_reconciliation, outcome="RESOLVED_SUCCESS")
    resolved = adapt_sqlite_panel(
        base,
        _rail_projection(orders=orders, latest_reconciliation=resolved_reconciliation),
    )
    assert _station_states(resolved)["RECONCILED"] == "satisfied"


def test_fill_station_requires_actual_fill_not_a_terminal_broker_state() -> None:
    """#1396 P2: canceled/expired/rejected orders and failed operations
    definitively received zero fill and must not satisfy FILL — only an
    actual `filled`/`partially_filled` broker state does."""
    base = _panel(_status(), _clerk_status(), [])
    canceled_orders = (
        ProjectedOrder(
            order_ref="order:enter",
            client_order_id="order:enter",
            broker_order_id="broker-1",
            role="ENTRY",
            broker_state="canceled",
            submitted_at_ms=_NOW - 350,
            updated_at_ms=_NOW - 300,
        ),
    )
    canceled = adapt_sqlite_panel(
        base,
        _rail_projection(orders=canceled_orders, operation_state="failed"),
    )
    assert _station_states(canceled)["FILL"] != "satisfied"

    filled_orders = (
        ProjectedOrder(
            order_ref="order:enter",
            client_order_id="order:enter",
            broker_order_id="broker-1",
            role="ENTRY",
            broker_state="filled",
            submitted_at_ms=_NOW - 350,
            updated_at_ms=_NOW - 300,
        ),
    )
    filled = adapt_sqlite_panel(
        base,
        _rail_projection(orders=filled_orders, operation_state="succeeded"),
    )
    assert _station_states(filled)["FILL"] == "satisfied"


def test_panel_composes_cards_rail_and_actions() -> None:
    entries = [
        intent_entry(sid=SID, intent="i1", ts_ms=_NOW - 1000),
        submit_acked_entry(sid=SID, intent="i1", ts_ms=_NOW - 900),
        fill_entry(sid=SID, intent="i1", ts_ms=_NOW - 800),
        reconciliation_entry(verdict="clean", ts_ms=_NOW - 700),
    ]
    decision = decision_receipt(seq=3, ts_ms=_NOW - 500, outcome="entered", reason_code="CROSS_UP")
    panel = _panel(_status(), _clerk_status(), entries, decision)

    assert panel.strategy_instance_id == SID
    assert panel.account_id == ACCT
    assert panel.health.phase == "ON_DUTY"
    assert panel.health.desired_state == "RUNNING"
    assert panel.health.last_decision_at_ms == _NOW - 500
    assert panel.clerk.account_id == ACCT
    assert panel.clerk.hold_active is False
    assert panel.rail.transaction_ref is not None
    assert len(panel.rail.stations) == 6
    action_ids = {a.action_id for a in panel.actions}
    assert action_ids == {
        "resume",
        "pause",
        "continue",
        "stop",
        "flatten_stop",
        "reconcile_now",
    }
    assert panel.mission_verdict.state == "working"
    assert panel.strategy_key == "deployment_validation"
    assert panel.exposure == {"SPY": 100.0}
    assert panel.recent_decisions[0].reason_code == "CROSS_UP"
    assert {check.operation for check in panel.readiness_checks} == action_ids
    assert panel.readiness_ready_count == sum(
        check.ready for check in panel.readiness_checks
    )
    assert panel.readiness_blocked_count == sum(
        not check.ready for check in panel.readiness_checks
    )
    assert (
        panel.readiness_ready_count + panel.readiness_blocked_count
        == len(panel.readiness_checks)
    )


def test_unperformed_actions_are_not_advertised_and_flatten_has_blast_radius() -> None:
    panel = _panel(_status(), _clerk_status(), [], exposure={"SPY": 2.0})
    changed_exposure = _panel(_status(), _clerk_status(), [], exposure={"SPY": 3.0})

    assert "retire" not in {action.action_id for action in panel.actions}
    assert "cancel_order" not in {action.action_id for action in panel.actions}
    confirmation = _action(panel, "flatten_stop").confirmation
    assert confirmation is not None
    assert confirmation.required_token == "FLATTEN"
    assert "SPY 2" in confirmation.body
    assert (
        _action(panel, "flatten_stop").concurrency_token != _action(changed_exposure, "flatten_stop").concurrency_token
    )


def test_missing_intent_does_not_present_retired_inventory_baseline_recovery() -> None:
    panel = _panel(
        _status(running=False),
        _clerk_status(
            reconciliation_verdict="missing_intent",
            freeze=AccountFreezeState(
                active=True,
                category="ACCOUNT_STATE_UNATTRIBUTABLE",
                explanation="Broker inventory does not match the journal.",
                next_step="Recover the verified inventory baseline.",
                observed_at_ms=_NOW - 200,
            ),
        ),
        [],
        exposure={},
    )

    assert "record_inventory_baseline" not in {
        action.action_id for action in panel.actions
    }


def test_stale_bot_attribution_does_not_restore_retired_baseline_recovery() -> None:
    panel = _panel(
        _status(running=False),
        _clerk_status(reconciliation_verdict="clean"),
        [],
        exposure={"SPY": 1.0},
    )

    assert "record_inventory_baseline" not in {
        action.action_id for action in panel.actions
    }


def test_active_hold_does_not_present_retired_direct_clear() -> None:
    panel = _panel(
        _status(),
        _clerk_status(hold=True, hold_code="UNEXPLAINED_ORDER_HOLD", healthy=True),
        [],
    )

    assert "clear_hold" not in {action.action_id for action in panel.actions}


def test_disabled_action_explains_backend_blocker_and_safe_next_step() -> None:
    panel = _panel(
        _status(running=False),
        _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD"),
        [],
        exposure={},
    )

    resume = _action(panel, "resume")
    assert resume.enabled is False
    assert resume.blockers[0].condition.id == "RESUME_TEST_BLOCKED"
    assert resume.blockers[0].detail is not None
    readiness = next(check for check in panel.readiness_checks if check.operation == "resume")
    assert readiness.ready is False
    assert readiness.cure is not None


def test_working_orders_and_fills_are_bounded_clerk_attributed_projections() -> None:
    open_entries = [
        intent_entry(sid=SID, intent="open", ts_ms=_NOW - 1_000),
        submit_acked_entry(sid=SID, intent="open", ts_ms=_NOW - 900),
    ]
    working = _panel(_status(), _clerk_status(), open_entries)
    assert len(working.working_orders) == 1
    assert working.working_orders[0].order_ref == working.rail.transaction_ref

    filled = _panel(
        _status(),
        _clerk_status(),
        [*open_entries, fill_entry(sid=SID, intent="open", ts_ms=_NOW - 800)],
    )
    assert filled.working_orders == []
    assert len(filled.recent_fills) == 1
    assert filled.recent_fills[0].symbol == "SPY"


def test_recent_fills_reuse_canonical_fill_deduplication() -> None:
    first = fill_entry(
        sid=SID,
        intent="open",
        ts_ms=_NOW - 800,
        event_key="exec:redelivered",
    )
    redelivery = fill_entry(
        sid=SID,
        intent="open",
        ts_ms=_NOW - 700,
        event_key="exec:redelivered",
    )

    panel = _panel(_status(), _clerk_status(), [first, redelivery])

    assert len(panel.recent_fills) == 1
    assert panel.recent_fills[0].filled_at_ms == _NOW - 800


def test_unhealthy_required_channel_blocks_trade_mode_mission() -> None:
    panel = _panel(
        _status(mode="trade"),
        _clerk_status(healthy=False),
        [],
    )

    assert panel.mission_verdict.state == "blocked"
    assert "market_data" in panel.mission_verdict.explanation
    assert "execution" in panel.mission_verdict.explanation


def test_panel_preserves_authoritative_paused_desired_state() -> None:
    panel = _panel(_status(desired_state="PAUSED", running=True), _clerk_status(), [])
    assert panel.health.desired_state == "PAUSED"
    assert _action(panel, "continue").enabled is True


def test_stop_outcome_copy_distinguishes_approved_carryover() -> None:
    status = _status(running=False)
    status = status.model_copy(
        update={
            "duty_outcome": BotDutyOutcomeView(
                kind="STOPPED",
                reason_code="STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
                recorded_at_ms=_NOW,
                run_id="run-1",
            ),
        }
    )
    panel = _panel(status, _clerk_status(), [], exposure={"SPY": 1.0})

    assert panel.health.duty_outcome is not None
    assert panel.health.duty_outcome.label == "Stopped with approved carryover"
    assert "durable checkpoint" in panel.health.duty_outcome.explanation


@pytest.mark.parametrize("reason_code", ["TypeError", "FEED_DEATH"])
def test_crash_copy_is_source_neutral_and_not_a_market_data_verdict(
    reason_code: str,
) -> None:
    status = _status(running=False)
    status = status.model_copy(
        update={
            "duty_outcome": BotDutyOutcomeView(
                kind="CRASHED",
                reason_code=reason_code,
                recorded_at_ms=_NOW,
                run_id="run-1",
            ),
        }
    )

    panel = _panel(status, _clerk_status(), [])

    assert panel.health.duty_outcome is not None
    assert panel.health.duty_outcome.label == "Crashed"
    assert panel.health.duty_outcome.explanation == (
        "The bot exited on an unhandled runtime error. "
        "This terminal outcome is not a market-data health verdict."
    )


def test_stop_enabled_only_when_running() -> None:
    running_panel = _panel(_status(running=True), _clerk_status(), [])
    stopped_panel = _panel(_status(running=False), _clerk_status(), [], exposure={})
    assert _action(running_panel, "stop").enabled is True
    assert _action(stopped_panel, "stop").enabled is False
    assert _action(stopped_panel, "resume").enabled is True


def test_pause_and_continue_are_mutually_exclusive_same_run_actions() -> None:
    evaluating = _panel(_status(running=True), _clerk_status(), [])
    paused = _panel(
        _status(running=True, desired_state="PAUSED"),
        _clerk_status(),
        [],
    )

    assert _action(evaluating, "pause").enabled is True
    assert _action(evaluating, "continue").enabled is False
    assert _action(paused, "pause").enabled is False
    assert _action(paused, "continue").enabled is True
    assert paused.health.desired_state == "PAUSED"
    assert paused.mission_verdict.label == "Paused"


def test_resume_requires_flat_exposure_and_no_clerk_hold() -> None:
    flat = _panel(_status(running=False), _clerk_status(), [], exposure={})
    exposed = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={"SPY": 1.0},
    )
    held = _panel(
        _status(running=False),
        _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD"),
        [],
        exposure={},
    )

    assert _action(flat, "resume").enabled is True
    assert _action(exposed, "resume").enabled is False
    assert _action(held, "resume").enabled is False
    assert _action(flat, "resume").concurrency_token != _action(exposed, "resume").concurrency_token
    assert _action(flat, "resume").concurrency_token != _action(held, "resume").concurrency_token


def test_resume_requires_exact_approved_carryover_projection() -> None:
    exact = _panel(
        _status(
            running=False,
            carryover_policy="ALLOW",
            checkpoint_exposure={"SPY": 1.0},
            checkpoint_matches=True,
        ),
        _clerk_status(),
        [],
        exposure={"SPY": 1.0},
    )
    mismatch = _panel(
        _status(
            running=False,
            carryover_policy="ALLOW",
            checkpoint_exposure={"SPY": 1.0},
            checkpoint_matches=True,
        ),
        _clerk_status(),
        [],
        exposure={"SPY": 2.0},
    )
    account_disabled = _panel(
        _status(
            running=False,
            carryover_policy="ALLOW",
            carryover_account_policy_enabled=False,
            checkpoint_exposure={"SPY": 1.0},
            checkpoint_matches=True,
        ),
        _clerk_status(),
        [],
        exposure={"SPY": 1.0},
    )

    assert exact.health.resume_eligible is True
    assert exact.health.resume_label == "Resume custody proof ready"
    assert _action(exact, "resume").enabled is True
    assert mismatch.health.resume_eligible is False
    assert _action(mismatch, "resume").enabled is False
    assert account_disabled.health.resume_eligible is False
    assert _action(account_disabled, "resume").enabled is False


def test_typed_resume_admission_outranks_projection_fields() -> None:
    flat_but_refused = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        resume_allowed=False,
    )
    exposed_but_admitted = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={"SPY": 2.0},
        resume_allowed=True,
    )

    assert _action(flat_but_refused, "resume").enabled is False
    assert flat_but_refused.health.resume_eligible is False
    assert _action(exposed_but_admitted, "resume").enabled is True
    assert exposed_but_admitted.health.resume_eligible is True


def test_dry_run_activity_is_structurally_labelled_simulated() -> None:
    activity = DryRunActivity(
        seq=1,
        strategy_instance_id=SID,
        run_id="run-dry",
        recorded_at_ms=_NOW - 100,
        bar_ref="SPY@1699999999900",
        intent="ENTER",
        order_ref="simulated:run-dry:1699999999900:ENTER",
        symbol="SPY",
        side="buy",
        quantity=1,
        fill_price=401.25,
    )

    panel = _panel(
        _status(running=True, mode="dry_run"),
        _clerk_status(),
        [],
        dry_run_activity=[activity],
    )

    assert panel.mode == "dry_run"
    assert "broker writes are impossible" in panel.execution_policy
    assert panel.recent_decisions[0].simulated is True
    assert panel.recent_decisions[0].reason_code == "SIMULATED_ENTER"
    assert panel.recent_fills[0].simulated is True
    assert panel.recent_fills[0].order_ref.startswith("simulated:")
    assert panel.health.last_decision_at_ms == _NOW - 100
    assert panel.health.last_bar_at_ms == 1_699_999_999_900


def test_resume_token_ignores_market_data_observation_timestamp() -> None:
    common = ("bot-process-registry:registry-1",)
    first = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        admission_evidence_refs=(*common, "market-data-feed:alpaca:1700000000000"),
    )
    second = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        admission_evidence_refs=(*common, "market-data-feed:alpaca:1700000000001"),
    )

    assert _action(first, "resume").concurrency_token == _action(second, "resume").concurrency_token


def test_resume_token_ignores_reconciliation_observation_timestamp() -> None:
    # A fresh Clerk reconciliation stamps a new observation instant every pass
    # (custody.py emits ``alpaca-reconciliation:<observed_at_ms>``). For an
    # unchanged off-duty bot that instant is pure churn — it must not make an
    # already presented Resume stale (the 2026-08-04 val-nvda-0804-05 409).
    common = (
        "bot-process-registry:registry-1",
        "market-data-feed:alpaca:1700000000000",
        "alpaca-clerk-journal:PA3KWXU1C4C3:418",
    )
    first = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        admission_evidence_refs=(*common, "alpaca-reconciliation:1722800212000"),
    )
    second = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        admission_evidence_refs=(*common, "alpaca-reconciliation:1722800217000"),
    )

    assert _action(first, "resume").concurrency_token == _action(second, "resume").concurrency_token


def test_resume_token_changes_when_clerk_journal_advances() -> None:
    # Stripping observation timestamps must NOT blind the token to a real
    # custody change. The Clerk appends a journal line only when something
    # happens on the account, so an advancing journal sequence is a genuine
    # change that must invalidate a presented Resume.
    common = (
        "bot-process-registry:registry-1",
        "market-data-feed:alpaca:1700000000000",
        "alpaca-reconciliation:1722800212000",
    )
    before = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        admission_evidence_refs=(*common, "alpaca-clerk-journal:PA3KWXU1C4C3:418"),
    )
    after = _panel(
        _status(running=False),
        _clerk_status(),
        [],
        exposure={},
        admission_evidence_refs=(*common, "alpaca-clerk-journal:PA3KWXU1C4C3:419"),
    )

    assert _action(before, "resume").concurrency_token != _action(after, "resume").concurrency_token


def test_clear_hold_remains_absent_regardless_of_channel_health() -> None:
    healthy = _panel(_status(), _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD"), [])
    assert healthy.clerk.hold_active is True
    assert "clear_hold" not in {action.action_id for action in healthy.actions}

    unhealthy = _panel(
        _status(),
        _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD", healthy=False),
        [],
    )
    assert "clear_hold" not in {action.action_id for action in unhealthy.actions}


def test_account_freeze_blocks_start_and_flatten_with_authored_copy() -> None:
    frozen = _panel(
        _status(running=False),
        _clerk_status(
            freeze=AccountFreezeState(
                active=True,
                category="ACCOUNT_STATE_UNPROVABLE",
                explanation="Fresh account truth is unavailable.",
                next_step="Restore broker observation and reconcile.",
                observed_at_ms=_NOW,
            )
        ),
        [],
        exposure={"SPY": 1.0},
    )

    assert frozen.clerk.freeze_active is True
    assert frozen.clerk.freeze_category == "ACCOUNT_STATE_UNPROVABLE"
    assert frozen.clerk.freeze_label == "Account state unprovable"
    assert frozen.clerk.freeze_explanation == "Fresh account truth is unavailable."
    assert frozen.clerk.freeze_next_step == "Restore broker observation and reconcile."
    assert _action(frozen, "resume").enabled is False
    assert _action(frozen, "flatten_stop").enabled is False


def test_revision_is_deterministic_and_changes_on_state_change() -> None:
    base = compute_revision(
        journal_len=3,
        last_transition_at_ms=100,
        desired_state="RUNNING",
        hold_active=False,
        last_decision_at_ms=200,
    )
    same = compute_revision(
        journal_len=3,
        last_transition_at_ms=100,
        desired_state="RUNNING",
        hold_active=False,
        last_decision_at_ms=200,
    )
    changed = compute_revision(
        journal_len=4,
        last_transition_at_ms=100,
        desired_state="RUNNING",
        hold_active=False,
        last_decision_at_ms=200,
    )
    assert base == same
    assert base != changed


def _action(panel, action_id):
    return next(a for a in panel.actions if a.action_id == action_id)


# ── primary_action_by_lens policy (#1665) ────────────────────────────────────


def _health(*, running: bool, desired_state: str = "RUNNING") -> BotHealthCard:
    return BotHealthCard(
        strategy_instance_id=SID,
        phase="ON_DUTY" if running else "OFF_DUTY",
        phase_label="On duty" if running else "Off duty",
        desired_state=desired_state,  # type: ignore[arg-type]
        desired_state_label=desired_state.title(),
        running=running,
        duty_outcome=None,
        last_decision_at_ms=None,
        decision_stale=False,
        last_bar_at_ms=None,
        resume_eligible=not running,
        resume_label="Resume",
        resume_explanation="Resume.",
        carryover_checkpoint_exposure={},
    )


def _stub_action(action_id: str, *, enabled: bool = True) -> PanelAction:
    return PanelAction(
        action_id=action_id,  # type: ignore[arg-type]
        label=action_id,
        explanation=f"{action_id} this bot.",
        enabled=enabled,
        blockers=[],
        confirmation=None,
        revision=1,
        concurrency_token=f"{action_id}-token",
    )


def _recovery_capability(action_id: str, *, primary: bool, available: bool = True) -> RecoveryCapability:
    return RecoveryCapability(
        action_id=action_id,
        label=action_id,
        explanation=f"{action_id} recovery capability.",
        available=available,
        unavailable_reason_code=None,
        unavailable_reason=None,
        scope="CUSTODY_SUBJECT",
        freshness="not_required",
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Do it.",
        concurrency_token=f"{action_id}-token",
        execution_ref=None,
        mutation=True,
        primary=primary,
    )


def test_select_primary_action_by_lens_stopped_resumable() -> None:
    selection = select_primary_action_by_lens([_stub_action("resume")], _health(running=False))

    assert selection.trader == "resume"
    assert selection.operator == "resume"


def test_select_primary_action_by_lens_paused_continuable() -> None:
    selection = select_primary_action_by_lens(
        [_stub_action("continue")],
        _health(running=True, desired_state="PAUSED"),
    )

    assert selection.trader == "continue"
    assert selection.operator == "continue"


def test_select_primary_action_by_lens_running_stoppable() -> None:
    selection = select_primary_action_by_lens([_stub_action("stop")], _health(running=True))

    assert selection.trader == "stop"
    assert selection.operator == "stop"


def test_select_primary_action_by_lens_blocked_action_still_referenced() -> None:
    """A disabled lifecycle action is still the reference; ``enabled`` only
    gates the button, not whether the banner may point at it (matches the
    pre-existing behavior of the frontend's retired ``primaryLifecycleAction``,
    and ADR 0027's ``wait`` disposition — a block is allowed to name its
    control without offering a fake, always-enabled button)."""
    selection = select_primary_action_by_lens(
        [_stub_action("resume", enabled=False)],
        _health(running=False),
    )

    assert selection.trader == "resume"
    assert selection.operator == "resume"


def test_select_primary_action_by_lens_missing_action_fails_closed() -> None:
    """No Trader-visible lifecycle action is presented: both references are
    ``None`` rather than guessing from `health` alone."""
    selection = select_primary_action_by_lens([], _health(running=False))

    assert selection.trader is None
    assert selection.operator is None


def test_select_primary_action_by_lens_recovery_primary_never_becomes_trader_reference() -> None:
    """The one deterministic Operator precedence rule (#1665): a recovery
    capability marked primary outranks the routine lifecycle command for the
    Operator lens, but can never leak into the Trader lens even when the
    Trader-visible lifecycle action is also presented alongside it."""
    selection = select_primary_action_by_lens(
        [_stub_action("stop"), _stub_action("rebuild_from_mirror")],
        _health(running=True),
        recovery_primary_action_id="rebuild_from_mirror",
    )

    assert selection.trader == "stop"
    assert selection.operator == "rebuild_from_mirror"


def test_select_primary_action_by_lens_dangling_recovery_primary_falls_back() -> None:
    """A recovery-primary id that is not actually presented (stale evidence,
    a caller bug) must never leak through as a dangling reference — Operator
    falls back to the same lifecycle candidate as Trader."""
    selection = select_primary_action_by_lens(
        [_stub_action("stop")],
        _health(running=True),
        recovery_primary_action_id="rebuild_from_mirror",
    )

    assert selection.trader == "stop"
    assert selection.operator == "stop"


def test_build_panel_populates_primary_action_by_lens_for_stopped_resumable_bot() -> None:
    panel = _panel(_status(running=False), _clerk_status(), [], exposure={})

    assert panel.primary_action_by_lens.trader == "resume"
    assert panel.primary_action_by_lens.operator == "resume"


def test_build_panel_populates_primary_action_by_lens_for_running_stoppable_bot() -> None:
    panel = _panel(_status(running=True), _clerk_status(), [])

    assert panel.primary_action_by_lens.trader == "stop"
    assert panel.primary_action_by_lens.operator == "stop"


def test_build_panel_populates_primary_action_by_lens_for_paused_continuable_bot() -> None:
    panel = _panel(_status(running=True, desired_state="PAUSED"), _clerk_status(), [])

    assert panel.primary_action_by_lens.trader == "continue"
    assert panel.primary_action_by_lens.operator == "continue"


def test_build_panel_populates_primary_action_by_lens_for_blocked_bot() -> None:
    """An account-held, stopped bot still names Resume as its reference —
    only ``enabled`` reflects the block; the reference itself is stable."""
    panel = _panel(
        _status(running=False),
        _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD"),
        [],
        exposure={},
    )

    assert panel.mission_verdict.state == "blocked"
    assert _action(panel, "resume").enabled is False
    assert panel.primary_action_by_lens.trader == "resume"
    assert panel.primary_action_by_lens.operator == "resume"


def test_sqlite_adapter_recovery_primary_selects_operator_reference_without_leaking_to_trader() -> None:
    """#1665: the audience-aware precedence, exercised through the real
    SQLite adapter path. A running, SQLite-activated bot never gets a plain
    ``stop`` back (only the Operator-only ``stop_bot_decisions`` capability
    survives activation while running), so the Trader reference must fail
    closed to ``None`` — it must never fall back to the Operator-only
    recovery action id. Also proves the retained
    ``readiness_checks[].evidence['primary']`` diagnostic marker can never
    disagree with the Operator reference, since both derive from the same
    ``RecoveryCapability.primary`` flag."""
    base = _panel(_status(running=True), _clerk_status(), [])
    projection = replace(
        _rail_projection(orders=()),
        recovery_actions=(
            _recovery_capability("reconcile_now", primary=False),
            _recovery_capability("rebuild_from_mirror", primary=True),
        ),
    )

    adapted = adapt_sqlite_panel(base, projection)

    assert adapted.primary_action_by_lens.trader is None
    assert adapted.primary_action_by_lens.operator == "rebuild_from_mirror"
    primary_check = next(
        check for check in adapted.readiness_checks if check.evidence.get("primary") is True
    )
    assert primary_check.operation == adapted.primary_action_by_lens.operator


def test_sqlite_adapter_falls_back_to_lifecycle_when_no_recovery_action_is_primary() -> None:
    base = _panel(_status(running=False), _clerk_status(), [], exposure={})
    projection = replace(
        _rail_projection(orders=()),
        recovery_actions=(_recovery_capability("reconcile_now", primary=False),),
    )

    adapted = adapt_sqlite_panel(base, projection)

    assert adapted.primary_action_by_lens.trader == "resume"
    assert adapted.primary_action_by_lens.operator == "resume"
    assert all(check.evidence.get("primary") is not True for check in adapted.readiness_checks)


def test_primary_action_by_lens_rejects_operator_only_action_as_trader_reference() -> None:
    """Schema-level defense in depth: the model validator itself must refuse
    to construct a ``BotPanelView`` whose Trader reference is an
    Operator-only action, independent of any policy-function test above."""
    base = _panel(_status(running=True), _clerk_status(), [])
    payload = base.model_dump()
    payload["primary_action_by_lens"] = {"trader": "rebuild_from_mirror", "operator": None}

    with pytest.raises(ValidationError):
        BotPanelView.model_validate(payload)


def test_primary_action_by_lens_rejects_dangling_operator_reference() -> None:
    base = _panel(_status(running=True), _clerk_status(), [])
    payload = base.model_dump()
    payload["primary_action_by_lens"] = {"trader": None, "operator": "rebuild_from_mirror"}

    with pytest.raises(ValidationError):
        BotPanelView.model_validate(payload)
