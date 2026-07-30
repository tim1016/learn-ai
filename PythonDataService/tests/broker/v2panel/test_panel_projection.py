"""Tests for the panel projection (S1, spec §7).

Composes the health/clerk cards + six-station rail + presented actions from
journal fixtures, and pins the revision determinism and the narrowed
desired_state (never PAUSED).
"""

from __future__ import annotations

from app.broker.alpaca.clerk.models import (
    ChannelHealth,
    ClerkStatus,
    HoldState,
    ReconciliationSummary,
)
from app.schemas.broker_bots import BotStatusView
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


def _status(*, desired_state: str = "RUNNING", running: bool = True) -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=SID,
        broker="alpaca",
        symbol="SPY",
        mode="log_only",
        running=running,
        phase="ON_DUTY" if running else "OFF_DUTY",
        desired_state=desired_state,  # type: ignore[arg-type]
        active_run_id="r1" if running else None,
        duty_outcome=None,
        binding_created_at_ms=1,
        last_transition_at_ms=2,
    )


def _clerk_status(
    *, hold: bool = False, hold_code: str | None = None, healthy: bool = True
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
        latest_reconciliation=ReconciliationSummary(
            verdict="clean", recorded_at_ms=_NOW - 200
        ),
        outstanding_intents=0,
        observed_at_ms=_NOW,
        channel_healths=[
            ChannelHealth(
                stream="market_data", healthy=healthy, reason="", observed_at_ms=_NOW - 10
            ),
            ChannelHealth(
                stream="execution", healthy=healthy, reason="", observed_at_ms=_NOW - 10
            ),
        ],
    )


def _panel(status: BotStatusView, clerk: ClerkStatus, entries: list, decision=None):
    return build_panel(
        status,
        clerk,
        entries,
        account_id=ACCT,
        exposure={"SPY": 100.0},
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
    decision = decision_receipt(
        seq=3, ts_ms=_NOW - 500, outcome="entered", reason_code="CROSS_UP"
    )
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
        "retire",
        "cancel_order",
        "clear_hold",
        "reconcile_now",
    }


def test_panel_never_emits_paused_desired_state() -> None:
    """Decision #10: a PAUSED lifecycle value narrows to STOPPED on the card."""
    panel = _panel(_status(desired_state="PAUSED", running=False), _clerk_status(), [])
    assert panel.health.desired_state == "STOPPED"
    assert panel.health.desired_state != "PAUSED"


def test_stop_enabled_only_when_running() -> None:
    running_panel = _panel(_status(running=True), _clerk_status(), [])
    stopped_panel = _panel(_status(running=False), _clerk_status(), [])
    assert _action(running_panel, "stop").enabled is True
    assert _action(stopped_panel, "stop").enabled is False
    assert _action(stopped_panel, "start").enabled is True


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
