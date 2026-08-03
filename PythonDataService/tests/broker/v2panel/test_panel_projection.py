"""Tests for the panel projection (S1, spec §7).

Composes the health/clerk cards + six-station rail + presented actions from
journal fixtures, and pins the revision determinism and the narrowed
desired_state (never PAUSED).
"""

from __future__ import annotations

from typing import Literal

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ChannelHealth,
    ClerkStatus,
    HoldState,
    ReconciliationSummary,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import MarketPulseView
from app.schemas.live_runs import BotDutyOutcomeView
from app.schemas.run_admission import RunAdmissionDecision, RunAdmissionFactAges
from app.services.bot_dry_run import DryRunActivity
from app.services.broker_v2_panel.panel_projection_service import (
    build_panel,
    compute_revision,
)
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
        last_bar_at_ms=_NOW - 300,
        journal_tail_ref=f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        flatten_supported=True,
        now_ms=_NOW,
        resume_admission=resume_admission,
        dry_run_activity=dry_run_activity,
        market_pulse=_MARKET_PULSE,
    )


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
        "clear_hold",
        "record_inventory_baseline",
        "reconcile_now",
    }
    assert panel.mission_verdict.state == "working"
    assert panel.strategy_key == "deployment_validation"
    assert panel.exposure == {"SPY": 100.0}
    assert panel.recent_decisions[0].reason_code == "CROSS_UP"
    assert {check.operation for check in panel.readiness_checks} == action_ids


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


def test_missing_intent_presents_confirmed_inventory_baseline_recovery() -> None:
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

    action = _action(panel, "record_inventory_baseline")
    assert action.enabled is True
    assert action.confirmation is not None
    assert action.confirmation.required_token == "BASELINE"
    assert "Earlier trades remain in history" in action.confirmation.consequence


def test_clean_flat_account_presents_stale_bot_attribution_recovery() -> None:
    panel = _panel(
        _status(running=False),
        _clerk_status(reconciliation_verdict="clean"),
        [],
        exposure={"SPY": 1.0},
    )

    action = _action(panel, "record_inventory_baseline")
    assert action.enabled is True
    assert action.confirmation is not None
    assert action.confirmation.required_token == "BASELINE"
    assert "All pre-cutover bot attribution is retired" in (
        action.confirmation.consequence
    )


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


def test_clear_hold_gated_on_healthy_and_fresh() -> None:
    # Hold active + channels healthy & fresh → clear_hold enabled.
    healthy = _panel(_status(), _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD"), [])
    assert healthy.clerk.hold_active is True
    assert _action(healthy, "clear_hold").enabled is True

    # Hold active but channels unhealthy → clear_hold stays disabled.
    unhealthy = _panel(
        _status(),
        _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD", healthy=False),
        [],
    )
    assert _action(unhealthy, "clear_hold").enabled is False


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
