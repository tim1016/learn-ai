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
from app.schemas.live_runs import BotDutyOutcomeView
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


def _status(
    *,
    desired_state: str | None = None,
    running: bool = True,
    carryover_policy: str = "FORBID",
    carryover_account_policy_enabled: bool = True,
    checkpoint_exposure: dict[str, float] | None = None,
    checkpoint_matches: bool = False,
    mode: Literal["log_only", "trade"] = "log_only",
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
        latest_reconciliation=ReconciliationSummary(verdict="clean", recorded_at_ms=_NOW - 200),
        outstanding_intents=0,
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
):
    return build_panel(
        status,
        clerk,
        entries,
        account_id=ACCT,
        exposure={"SPY": 100.0} if exposure is None else exposure,
        fills_today=0,
        realized_pnl_today=0.0,
        open_pnl=None,
        latest_decision=decision,
        last_bar_at_ms=_NOW - 300,
        journal_tail_ref=f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/decisions",
        journal_tail_seq=(decision.seq if decision is not None else None),
        flatten_supported=True,
        now_ms=_NOW,
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
        "start",
        "stop",
        "flatten_stop",
        "clear_hold",
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


def test_disabled_action_explains_backend_blocker_and_safe_next_step() -> None:
    panel = _panel(
        _status(running=False),
        _clerk_status(hold=True, hold_code="STREAM_HEALTH_HOLD"),
        [],
        exposure={},
    )

    start = _action(panel, "start")
    assert start.enabled is False
    assert start.blockers[0].condition.scope == "account"
    assert start.blockers[0].detail is not None
    readiness = next(check for check in panel.readiness_checks if check.operation == "start")
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


def test_panel_never_emits_paused_desired_state() -> None:
    """Decision #10: a PAUSED lifecycle value narrows to STOPPED on the card."""
    panel = _panel(_status(desired_state="PAUSED", running=False), _clerk_status(), [])
    assert panel.health.desired_state == "STOPPED"
    assert panel.health.desired_state != "PAUSED"


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
    assert _action(stopped_panel, "start").enabled is True


def test_start_requires_flat_exposure_and_no_clerk_hold() -> None:
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

    assert _action(flat, "start").enabled is True
    assert _action(exposed, "start").enabled is False
    assert _action(held, "start").enabled is False
    assert _action(flat, "start").concurrency_token != _action(exposed, "start").concurrency_token
    assert _action(flat, "start").concurrency_token != _action(held, "start").concurrency_token


def test_start_requires_exact_approved_carryover_projection() -> None:
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
    assert _action(exact, "start").enabled is True
    assert mismatch.health.resume_eligible is False
    assert _action(mismatch, "start").enabled is False
    assert account_disabled.health.resume_eligible is False
    assert _action(account_disabled, "start").enabled is False


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
    assert _action(frozen, "start").enabled is False
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
