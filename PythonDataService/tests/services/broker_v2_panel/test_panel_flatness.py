"""Regression coverage for the live Broker V2 panel flatness boundary."""

from __future__ import annotations

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
    ProjectedPosition,
    ProjectionGuidance,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotPanelView, MarketPulseView
from app.schemas.run_admission import RunAdmissionDecision, RunAdmissionFactAges
from app.services.broker_v2_panel.panel_projection_service import build_panel
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    adapt_sqlite_panel,
    build_sqlite_catalog,
)

_ACCOUNT_ID = "test-account"
_STRATEGY_INSTANCE_ID = "flatness-boundary-bot"
_NOW_MS = 1_700_000_000_000
_SUB_EPSILON_QUANTITY = 1e-12


def _stopped_status() -> BotStatusView:
    return BotStatusView(
        strategy_instance_id=_STRATEGY_INSTANCE_ID,
        strategy_key="deployment_validation",
        strategy_label="Deployment Validation",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        running=False,
        phase="OFF_DUTY",
        desired_state="STOPPED",
        active_run_id=None,
        duty_outcome=None,
        binding_created_at_ms=1,
        last_transition_at_ms=2,
    )


def _healthy_clerk_status() -> ClerkStatus:
    return ClerkStatus(
        broker="alpaca",
        account_id=_ACCOUNT_ID,
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        latest_reconciliation=ReconciliationSummary(
            verdict="clean",
            recorded_at_ms=_NOW_MS - 200,
        ),
        outstanding_intents=0,
        observed_at_ms=_NOW_MS,
        channel_healths=[
            ChannelHealth(
                stream="market_data",
                healthy=True,
                reason="",
                observed_at_ms=_NOW_MS - 10,
            ),
            ChannelHealth(
                stream="execution",
                healthy=True,
                reason="",
                observed_at_ms=_NOW_MS - 10,
            ),
        ],
    )


def _market_pulse() -> MarketPulseView:
    return MarketPulseView(
        session="OPEN",
        feed_state="LIVE",
        latest_bar_at_ms=_NOW_MS - 60_000,
        age_ms=60_000,
        source="test-feed",
        expected_cadence_ms=60_000,
        headline="Market data live",
        explanation="The test feed is current.",
        next_step=None,
        attention_required=False,
        observed_at_ms=_NOW_MS,
    )


def _admitted_resume() -> RunAdmissionDecision:
    return RunAdmissionDecision(
        operation="RESUME",
        allowed=True,
        reason_code="RESUME_ADMITTED",
        explanation="The runner and Clerk admit this new run.",
        next_step=None,
        strategy_instance_id=_STRATEGY_INSTANCE_ID,
        proposed_run_id="run-proposed",
        configuration_hash="a" * 64,
        account_id=_ACCOUNT_ID,
        evaluated_at_ms=_NOW_MS,
        fact_ages_ms=RunAdmissionFactAges(
            runtime=0,
            process=0,
            market_data=0,
            clerk=0,
        ),
        evidence_refs=("test:resume-admission",),
    )


def _base_panel() -> BotPanelView:
    return build_panel(
        _stopped_status(),
        _healthy_clerk_status(),
        [],
        account_id=_ACCOUNT_ID,
        exposure={"SPY": _SUB_EPSILON_QUANTITY},
        fills_today=0,
        realized_pnl_today=0.0,
        open_pnl=None,
        latest_decision=None,
        last_bar_at_ms=_NOW_MS - 300,
        journal_tail_ref="/test/journal",
        journal_tail_seq=None,
        flatten_supported=True,
        now_ms=_NOW_MS,
        resume_admission=_admitted_resume(),
        market_pulse=_market_pulse(),
    )


def _projection() -> ClerkProjection:
    return ClerkProjection(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_STRATEGY_INSTANCE_ID,
        authority_generation=4,
        db_identity_token="db-4",
        authority_health="healthy",
        authority_health_reason=None,
        control_revision=17,
        custody_owner="ACCOUNT_CLERK",
        runs=(),
        commands=(),
        operations=(),
        positions=(
            ProjectedPosition(
                strategy_instance_id=_STRATEGY_INSTANCE_ID,
                symbol="SPY",
                attributed_qty=_SUB_EPSILON_QUANTITY,
                updated_at_ms=_NOW_MS - 200,
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
            available_safety_actions=(),
            action_required=False,
            next_step="No recovery action is required.",
        ),
        recovery_actions=(),
        generated_at_ms=_NOW_MS,
    )


def _economics() -> EconomicSnapshot:
    return EconomicSnapshot(
        account_id=_ACCOUNT_ID,
        strategy_instance_id=_STRATEGY_INSTANCE_ID,
        authority_generation=4,
        control_revision=17,
        session_open_ms=_NOW_MS - 3_600_000,
        session_close_ms=_NOW_MS + 3_600_000,
        recent_fills=(),
        fills_today=0,
        exposure={"SPY": _SUB_EPSILON_QUANTITY},
        realized_pnl_today=0.0,
        open_pnl=0.0,
        marks_complete=True,
        mark_observed_at_ms={"SPY": _NOW_MS},
        fee_fidelity="reported",
        execution_coverage="complete",
        last_activity_at_ms=_NOW_MS,
    )


def test_sqlite_panel_omits_sub_epsilon_exposure() -> None:
    adapted = adapt_sqlite_panel(_base_panel(), _projection())

    assert adapted.exposure == {}


def test_sqlite_roster_describes_sub_epsilon_exposure_as_flat() -> None:
    catalog = build_sqlite_catalog(
        [_stopped_status()],
        projections={_STRATEGY_INSTANCE_ID: _projection()},
        economic_rollups={_STRATEGY_INSTANCE_ID: _economics()},
        account_id=_ACCOUNT_ID,
    )

    assert catalog[0].status_explanation == "Off duty and flat."


def test_health_card_uses_flat_resume_copy_for_sub_epsilon_exposure() -> None:
    panel = _base_panel()

    assert panel.health.resume_label == "Flat Resume ready"
