"""Tests for the panel projection (S1, spec §7).

Composes the health/clerk cards + six-station rail + presented actions from
journal fixtures, and pins the revision determinism and the narrowed
desired_state (never PAUSED).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
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
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
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
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import BrokerOrderLeg, OrderSide
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotHealthCard,
    BotPanelView,
    MarketPulseView,
    PanelAction,
    RecentDecisionView,
    RecentFillView,
)
from app.schemas.live_runs import BotDutyOutcomeView
from app.schemas.operator_blocker import AccountOperatorPosture
from app.schemas.run_admission import (
    ProgramBuildAdmissionFact,
    RunAdmissionDecision,
    RunAdmissionFactAges,
)
from app.schemas.signal_program_seal import (
    ConfiguredSignalProgramSeal,
    ExitEligibilityContract,
    NumericalProvenanceContract,
    ResolvedSignalParameter,
    SignalBarIntegrityContract,
    SignalClockContract,
    SignalDataContract,
    SignalSeriesContract,
    seal_bot_program,
)
from app.services.bot_binding_repository import ProgramBuildRunEvidence
from app.services.bot_dry_run import DryRunActivity
from app.services.broker_v2_panel.panel_authority_guard import MixedAuthorityAggregateError
from app.services.broker_v2_panel.panel_projection_service import (
    build_panel,
    compute_revision,
    evaluate_channel_health,
    program_build_view_from_run_evidence,
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

# _status()'s default fixture strategy. Must stay a strategy with no
# registered Signal Program: _default_program_build() below fails closed
# (deliberately, per ADR 0043 Finding 1) the instant a named strategy
# becomes sealed with no explicit program_build evidence supplied, and this
# module never supplies that evidence -- almost every test in this file
# relies on the truthful NOT_APPLICABLE default. "deployment_validation"
# was that strategy until it was promoted through the governed Signal
# Program seam (issue #1730 Slice 5); "spy_orb" is a genuinely still-
# unsealed compatibility strategy (no signal_program_factory) with an
# equally parameter-free default, so it is a clean drop-in.
assert _STRATEGY_REGISTRY["spy_orb"].signal_program_factory is None
_UNSEALED_STRATEGY_KEY = "spy_orb"

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


@pytest.mark.parametrize(
    "row_factory",
    [
        lambda: RecentDecisionView(
            seq=1,
            recorded_at_ms=_NOW,
            outcome="entered",
            reason_code="SIMULATED",
            bar_ref="source-bar:sim:ema-1:bar-1",
            order_ref="order:1",
            simulated=True,
        ),
        lambda: RecentFillView(
            order_ref="order:1",
            symbol="SPY",
            side="buy",
            quantity=1,
            price=500,
            filled_at_ms=_NOW,
            simulated=True,
        ),
    ],
)
def test_simulated_panel_rows_require_synthetic_authority_metadata(
    row_factory: Callable[[], RecentDecisionView | RecentFillView],
) -> None:
    with pytest.raises(ValidationError, match="synthetic authority metadata"):
        row_factory()


def test_simulated_panel_rows_accept_nonempty_synthetic_authority_metadata() -> None:
    row = RecentDecisionView(
        seq=1,
        recorded_at_ms=_NOW,
        outcome="entered",
        reason_code="SIMULATED",
        bar_ref="source-bar:sim:ema-1:bar-1",
        order_ref="order:1",
        simulated=True,
        authority_account_id="sim:ema-1",
        authority_kind="synthetic",
    )

    assert row.authority_account_id == "sim:ema-1"


def _status(
    *,
    desired_state: str | None = None,
    running: bool = True,
    carryover_policy: str = "FORBID",
    carryover_account_policy_enabled: bool = True,
    checkpoint_exposure: dict[str, float] | None = None,
    checkpoint_matches: bool = False,
    mode: Literal["log_only", "dry_run", "trade"] = "log_only",
    strategy_key: str = _UNSEALED_STRATEGY_KEY,
) -> BotStatusView:
    resolved_desired_state = desired_state or ("RUNNING" if running else "STOPPED")
    return BotStatusView(
        strategy_instance_id=SID,
        strategy_key=strategy_key,
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


def _default_program_build(strategy_key: str) -> ProgramBuildAdmissionFact:
    """The truthful ``program_build`` fact for a fixture that never sets one.

    ``build_panel`` requires real evidence for every call — it must never
    guess (see the "require the argument" fix on its docstring). Every
    fixture in this module uses
    ``_status()``'s default ``strategy_key`` (``_UNSEALED_STRATEGY_KEY``,
    "spy_orb"), which has no registered Signal Program
    (``app/engine/strategy/registry.py``), so ``NOT_APPLICABLE`` is the
    actually-true state here, not a fabricated placeholder. A fixture naming
    a strategy that DOES have a registered Signal Program (e.g.
    "ema_crossover_signal") has no truthful default — missing evidence for
    it is ``UNPROVEN``, never ``NOT_APPLICABLE`` — so such a test must pass
    ``program_build`` explicitly instead of relying on this default.
    """
    registration = _STRATEGY_REGISTRY.get(strategy_key)
    if registration is None or registration.signal_program_factory is None:
        return ProgramBuildAdmissionFact(
            state="NOT_APPLICABLE",
            program_key=strategy_key,
            verified_at_ms=_NOW,
            explanation="This compatibility strategy has no registered Signal Program.",
        )
    raise AssertionError(
        f"'{strategy_key}' has a registered Signal Program; _panel() callers must "
        "pass explicit program_build evidence rather than rely on a fabricated default."
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
    sealed_program=None,
    program_build: ProgramBuildAdmissionFact | None = None,
    authority_account_id: str | None = None,
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
            program_build=0,
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
        authority_account_id=authority_account_id,
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
        sealed_program=sealed_program,
        program_build=program_build or _default_program_build(status.strategy_key),
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
    # #1729 AC #6/#7 regression: `base`'s rail selected a *legacy* JSONL
    # transaction that does not exist in the SQLite fold. The adapter must
    # echo back that unresolved ref as explicit, named absence — never
    # silently substitute the newest unrelated SQLite operation
    # ("effect:enter") for it.
    assert adapted.rail.transaction_ref != "effect:enter"
    assert adapted.rail.transaction_ref == base.rail.transaction_ref
    assert all(station.state == "not_applicable" for station in adapted.rail.stations)
    assert adapted.working_orders[0].order_ref == "order:enter"
    assert adapted.working_orders[0].filled_quantity is None
    assert adapted.recent_fills == []


def _monotonic_test_clock(start_ms: int) -> Callable[[], int]:
    """A strictly-increasing fake clock so two operations accepted back-to-back
    get distinct, orderable ``created_at_ms`` values (real wall-clock calls
    can tie at 1ms resolution and make "the newest operation" nondeterministic)."""
    state = {"value": start_ms}

    def clock() -> int:
        state["value"] += 1
        return state["value"]

    return clock


def _real_repo(tmp_path: Path, *, account_id: str = ACCT) -> ClerkSqliteRepository:
    repo = ClerkSqliteRepository.initialize(
        account_id=account_id,
        artifacts_root=tmp_path,
        clock=_monotonic_test_clock(_NOW),
    )
    repo.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    submit_start_run(repo, account_id=account_id, strategy_instance_id=SID, lifecycle_run_id="run-1")
    return repo


def _two_real_operations(repo: ClerkSqliteRepository) -> tuple[str, str]:
    """Accept a real ENTER then immediately EXIT it — two distinct, genuinely
    durable effect operations for one bot with no broker contact needed
    (capture-before-contact commits both before any broker attempt)."""
    entered = accept_enter(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=SID,
        decision_id="dec-old",
        lifecycle_run_id="run-1",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    exited = accept_exit(
        repo,
        account_id=repo.account_id,
        strategy_instance_id=SID,
        decision_id="dec-new",
        lifecycle_run_id="run-1",
        entry_order_ref=entered.order_ref,
    )
    assert exited.effect_operation_id is not None
    return entered.effect_operation_id, exited.effect_operation_id


def test_transaction_rail_resolves_old_transaction_outside_bounded_window(
    tmp_path: Path,
) -> None:
    """#1729 AC #6/#7 regression, end-to-end over a real SQLite repository:
    a ref that is a genuine durable transaction — just not one of the most
    recent operations the bounded projection window carries — must resolve
    to itself via the stored-key join, never to the newest unrelated
    operation."""
    repo = _real_repo(tmp_path)
    try:
        old_ref, newest_ref = _two_real_operations(repo)
        reader = SqliteClerkProjectionReader.from_repository(repo)
        try:
            # A window of 1 only carries the newest operation (EXIT); the
            # ENTER `old_ref` genuinely exists in storage but falls outside it.
            windowed_projection = reader.bot_snapshot(SID, operation_limit=1)
        finally:
            reader.close()
        assert windowed_projection is not None
        assert [op.effect_operation_id for op in windowed_projection.operations] == [newest_ref]

        base = _panel(_status(), _clerk_status(), [])
        base = base.model_copy(
            update={"rail": base.rail.model_copy(update={"transaction_ref": old_ref})}
        )

        adapted = adapt_sqlite_panel(base, windowed_projection, repository=repo)

        assert adapted.rail.transaction_ref == old_ref
        assert adapted.rail.transaction_ref != newest_ref
    finally:
        repo.close()


def test_transaction_rail_reports_explicit_absence_for_a_ref_that_does_not_exist(
    tmp_path: Path,
) -> None:
    """A ref that matches nothing anywhere — not the bounded window, not the
    stored-key join — must render explicit, named absence, never silently
    fall back to the newest operation."""
    repo = _real_repo(tmp_path)
    try:
        _old_ref, newest_ref = _two_real_operations(repo)
        reader = SqliteClerkProjectionReader.from_repository(repo)
        try:
            projection = reader.bot_snapshot(SID)
        finally:
            reader.close()
        assert projection is not None

        base = _panel(_status(), _clerk_status(), [])
        base = base.model_copy(
            update={
                "rail": base.rail.model_copy(update={"transaction_ref": "effect:never-existed"})
            }
        )

        adapted = adapt_sqlite_panel(base, projection, repository=repo)

        assert adapted.rail.transaction_ref == "effect:never-existed"
        assert adapted.rail.transaction_ref != newest_ref
        assert all(station.state == "not_applicable" for station in adapted.rail.stations)
        assert all("effect:never-existed" in station.receipt for station in adapted.rail.stations)
    finally:
        repo.close()


def test_transaction_rail_never_leaks_a_real_ref_from_a_different_bot(
    tmp_path: Path,
) -> None:
    """A real, resolvable effect operation belonging to a *different* bot
    must not leak into this bot's rail via the stored-key join fallback."""
    repo = _real_repo(tmp_path)
    try:
        other_sid = "bot-gamma"
        repo.register_strategy_instance(strategy_instance_id=other_sid, symbol="SPY", config_hash="h2")
        submit_start_run(repo, account_id=ACCT, strategy_instance_id=other_sid, lifecycle_run_id="run-2")
        foreign = accept_enter(
            repo,
            account_id=ACCT,
            strategy_instance_id=other_sid,
            decision_id="dec-foreign",
            lifecycle_run_id="run-2",
            leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        )

        reader = SqliteClerkProjectionReader.from_repository(repo)
        try:
            projection = reader.bot_snapshot(SID)
        finally:
            reader.close()
        assert projection is not None
        assert projection.operations == ()

        base = _panel(_status(), _clerk_status(), [])
        base = base.model_copy(
            update={
                "rail": base.rail.model_copy(
                    update={"transaction_ref": foreign.effect_operation_id}
                )
            }
        )

        adapted = adapt_sqlite_panel(base, projection, repository=repo)

        assert adapted.rail.transaction_ref == foreign.effect_operation_id
        assert all(station.state == "not_applicable" for station in adapted.rail.stations)
    finally:
        repo.close()


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
    # #1729 AC #8: this adapter overwrites whatever `recent_fills` the projector
    # produced, so it owes every row the authority it was read from. Without the
    # stamp a production fill reaches the panel with no authority while its
    # sibling decision row carries one, and the single-authority guard can no
    # longer tell a real-paper fill from a synthetic one.
    assert [
        (fill.authority_account_id, fill.authority_kind, fill.simulated)
        for fill in adapted.recent_fills
    ] == [(ACCT, "real_paper", False)] * 3
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
    assert panel.strategy_key == _UNSEALED_STRATEGY_KEY
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


def _sealed_bot_program():
    configured = ConfiguredSignalProgramSeal(
        program_key="ema_crossover_signal",
        program_version="ema-crossover-signal/v1",
        protocol_version="signal-session-protocol/v1",
        parameter_schema_version="ema-crossover-signal-params/v1",
        golden_trace_root="a" * 64,
        parameters={
            "fast_period": ResolvedSignalParameter(value=12, unit="bars", origin="registered_default"),
        },
        parameters_match_validated_settings=True,
        data=SignalDataContract(
            provider="polygon",
            symbol="SPY",
            base_timeframe_ms=60_000,
            decision_timeframe_ms=60_000,
        ),
        clock=SignalClockContract(use_rth=True, warmup_lookback_days=5),
        signals=(SignalSeriesContract(name="fast", indicator="ema", field="close", period=12, warmup_bars=12),),
        decision_streams=("ENTER", "EXIT"),
        bar_integrity=SignalBarIntegrityContract(),
        exit_eligibility=ExitEligibilityContract(countdown_decision_clocks=5, countdown_state_persistable=False),
        numerical_provenance=NumericalProvenanceContract(
            formula="test formula",
            reference="test reference",
            canonical_implementation="test canonical implementation",
            validated_against="test validated against",
            equivalence_level="bit_exact",
        ),
    )
    return seal_bot_program(
        strategy_instance_id=SID,
        configured_signal=configured,
        configured_signal_hash=configured.semantic_hash(),
        broker="alpaca",
        sealed_account_id=ACCT,
        mode="trade",
        action_plan={"on_enter": [], "on_exit": []},
        quantity=1,
        carryover_policy="FORBID",
        validation_event_id="event-1",
        validation_snapshot_sha256="d" * 64,
        sealed_at_ms=_NOW,
    )


def test_program_build_view_from_run_evidence_replays_the_proven_verdict() -> None:
    """#1728 Gap 2: the frozen per-run record maps losslessly onto the same
    ``ProgramBuildAdmissionFact`` shape a fresh proof would produce."""
    seal = _sealed_bot_program()
    evidence = ProgramBuildRunEvidence(
        strategy_instance_id=SID,
        run_id="run-1",
        sealed_program_hash=seal.bot_configuration_hash,
        program_version=seal.configured_signal.program_version,
        golden_trace_root=seal.configured_signal.golden_trace_root,
        running_artifact_digest="b" * 64,
        qualification_receipt_hash="c" * 64,
        verified_at_ms=1_000,
    )

    fact = program_build_view_from_run_evidence("ema_crossover_signal", evidence)

    assert fact.state == "PROVEN"
    assert fact.program_key == "ema_crossover_signal"
    assert fact.program_version == evidence.program_version
    assert fact.golden_trace_root == evidence.golden_trace_root
    assert fact.running_artifact_digest == evidence.running_artifact_digest
    assert fact.qualification_receipt_hash == evidence.qualification_receipt_hash
    # The frozen verified_at_ms is replayed verbatim — never today's clock.
    assert fact.verified_at_ms == 1_000
    assert fact.evidence_refs == (
        f"signal-program-seal:{seal.bot_configuration_hash}",
        "program-build-receipt:" + "c" * 64,
        "program-build-digest:" + "b" * 64,
    )


def test_panel_exposes_sealed_program_build_proof_and_decision_causal_links() -> None:
    """PRD Sec 11.1-11.4, 19: seal + build proof + FR-030 causal links reach the panel."""
    decision = decision_receipt(
        seq=3,
        ts_ms=_NOW - 500,
        outcome="entered",
        reason_code="CROSS_UP",
        decision_id="e" * 64,
        effect_operation_id="effect-op-3",
    )
    seal = _sealed_bot_program()
    program_build = ProgramBuildAdmissionFact(
        state="PROVEN",
        program_key="ema_crossover_signal",
        program_version=seal.configured_signal.program_version,
        golden_trace_root=seal.configured_signal.golden_trace_root,
        running_artifact_digest="b" * 64,
        qualification_receipt_hash="c" * 64,
        verified_at_ms=_NOW,
        evidence_refs=(f"program-build-digest:{'b' * 64}",),
        explanation="The running Signal Program build matches its golden qualification receipt.",
    )
    panel = _panel(
        _status(mode="trade"),
        _clerk_status(),
        [],
        decision,
        sealed_program=seal,
        program_build=program_build,
    )

    # Seal content reused verbatim — never re-derived.
    assert panel.sealed_program == seal
    assert panel.sealed_program.configured_signal.parameters["fast_period"].value == 12
    assert panel.sealed_program.configured_signal.parameters["fast_period"].origin == "registered_default"
    assert panel.sealed_program.configured_signal.parameters_match_validated_settings is True

    # Build proof is dynamic run evidence, distinct from the seal above.
    assert panel.program_build == program_build
    assert panel.program_build.state == "PROVEN"

    # Admission policy verdict reaches the panel.
    assert panel.resume_admission is not None

    # Durable causal links (decision_id/effect_operation_id) attach to the
    # exact receipt that carries them; order_ref was already present.
    linked = next(row for row in panel.recent_decisions if row.seq == 3)
    assert linked.decision_id == "e" * 64
    assert linked.effect_operation_id == "effect-op-3"


def test_build_panel_requires_program_build_evidence() -> None:
    """Finding 1 regression: ``build_panel`` used to default a missing
    ``program_build`` argument to a fabricated ``NOT_APPLICABLE`` fact — even
    for "ema_crossover_signal", a strategy that DOES have a registered Signal
    Program (``app/engine/strategy/registry.py``). ADR 0043 reserves
    ``NOT_APPLICABLE`` for a strategy with no registered program at all; a
    caller merely omitting evidence must never read as that. ``build_panel``
    now requires the argument outright, so the previously-silent wrong
    answer becomes a loud contract violation instead of a false reading.
    """
    status = _status().model_copy(update={"strategy_key": "ema_crossover_signal"})
    with pytest.raises(TypeError, match="program_build"):
        build_panel(
            status,
            _clerk_status(),
            [],
            account_id=ACCT,
            exposure={"SPY": 100.0},
            fills_today=0,
            realized_pnl_today=0.0,
            open_pnl=None,
            latest_decision=None,
            last_bar_at_ms=None,
            journal_tail_ref=f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/decisions",
            journal_tail_seq=None,
            flatten_supported=True,
            now_ms=_NOW,
            market_pulse=_MARKET_PULSE,
        )


def test_panel_surfaces_unproven_for_a_registered_signal_program_with_no_evidence() -> None:
    """Finding 1: a registered Signal Program strategy with no build evidence
    yet must read as ``UNPROVEN``, never ``NOT_APPLICABLE`` — ``NOT_APPLICABLE``
    is reserved (ADR 0043) for a strategy with no registered Signal Program at
    all. "ema_crossover_signal" has one, so its honest no-evidence state is
    the one ``prove_running_program_build`` itself would produce for e.g. an
    instance with no complete v2 seal yet: ``UNPROVEN``.
    """
    status = _status().model_copy(update={"strategy_key": "ema_crossover_signal"})
    program_build = ProgramBuildAdmissionFact(
        state="UNPROVEN",
        program_key="ema_crossover_signal",
        verified_at_ms=_NOW,
        explanation=(
            "The instance has no complete v2 Signal Program seal. Legacy bytes remain "
            "inspectable, but this instance must be cloned before it can Resume."
        ),
    )

    panel = _panel(status, _clerk_status(), [], program_build=program_build)

    assert panel.strategy_key == "ema_crossover_signal"
    assert panel.program_build.state == "UNPROVEN"


def test_panel_renders_explicit_absence_when_no_seal_or_causal_links_supplied() -> None:
    """PRD Sec 19: an absent link/seal is an explicit state, never inferred."""
    decision = decision_receipt(seq=4, ts_ms=_NOW - 500, outcome="no_action", reason_code="NO_SIGNAL")
    panel = _panel(_status(mode="trade"), _clerk_status(), [], decision)

    assert panel.sealed_program is None
    assert panel.program_build.state == "NOT_APPLICABLE"
    row = next(r for r in panel.recent_decisions if r.seq == 4)
    assert row.decision_id is None
    assert row.effect_operation_id is None


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


def test_evaluate_channel_health_required_streams_narrows_to_market_data() -> None:
    """#1702: Dry Run checks only the market-data stream — it never opens
    the execution channel, so an unhealthy/absent execution stream must not
    fail a Dry-Run-scoped evaluation."""
    now_ms = 1_700_000_000_000
    market_data_only = (ChannelHealth(stream="market_data", healthy=True, observed_at_ms=now_ms),)

    full = evaluate_channel_health(market_data_only, now_ms)
    scoped = evaluate_channel_health(market_data_only, now_ms, required_streams=("market_data",))

    assert full.ready is False
    assert full.missing == ("execution",)
    assert scoped.ready is True
    assert scoped.missing == ()


def test_evaluate_channel_health_required_streams_still_catches_unhealthy_market_data() -> None:
    now_ms = 1_700_000_000_000
    unhealthy = (ChannelHealth(stream="market_data", healthy=False, observed_at_ms=now_ms),)

    scoped = evaluate_channel_health(unhealthy, now_ms, required_streams=("market_data",))

    assert scoped.ready is False
    assert scoped.unhealthy == ("market_data",)


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
        authority_account_id=f"sim:{SID}",
        authority_kind="synthetic",
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
    # #1728 Gap 1: with no SQLite decision-receipt evidence for this Dry Run
    # instance, the causal links render the explicit absence, never a guess.
    assert panel.recent_decisions[0].decision_id is None
    assert panel.recent_decisions[0].effect_operation_id is None
    assert panel.recent_fills[0].simulated is True
    assert panel.recent_fills[0].order_ref.startswith("simulated:")


def test_dry_run_decisions_use_sqlite_causal_links_when_evidence_exists() -> None:
    """#1728 Gap 1: Dry Run runs through the same governed synthetic Clerk
    (PR #1722) and durably records every decision through the identical
    SQLite decision-receipt path Paper uses. Whenever that SQLite evidence
    exists, the panel must expose its real causal links, not the legacy WAL.
    """
    activity = DryRunActivity(
        seq=1,
        strategy_instance_id=SID,
        run_id="run-dry",
        authority_account_id=f"sim:{SID}",
        authority_kind="synthetic",
        recorded_at_ms=_NOW - 100,
        bar_ref="SPY@1699999999900",
        intent="ENTER",
        order_ref="simulated:run-dry:1699999999900:ENTER",
        symbol="SPY",
        side="buy",
        quantity=1,
        fill_price=401.25,
    )
    decision = decision_receipt(
        seq=5,
        ts_ms=_NOW - 50,
        outcome="entered",
        reason_code="CROSS_UP",
        decision_id="f" * 64,
        effect_operation_id="effect-op-5",
    )

    panel = _panel(
        _status(running=True, mode="dry_run"),
        _clerk_status(),
        [],
        decision,
        dry_run_activity=[activity],
    )

    assert panel.mode == "dry_run"
    row = panel.recent_decisions[0]
    assert row.seq == 5
    # Sourced from the SQLite receipt, not the WAL's "SIMULATED_ENTER" — proves
    # the SQLite-backed path was used, not the legacy fallback.
    assert row.reason_code == "CROSS_UP"
    assert row.simulated is True
    assert row.authority_account_id == f"sim:{SID}"
    assert row.authority_kind == "synthetic"
    assert row.decision_id == "f" * 64
    assert row.effect_operation_id == "effect-op-5"
    # Fills remain WAL-sourced — Gap 1 is about decision causal links, and
    # SQLite decision receipts carry no fill quantity/price.
    assert panel.recent_fills[0].simulated is True
    assert panel.recent_fills[0].order_ref.startswith("simulated:")
    assert panel.recent_decisions[0].authority_account_id == f"sim:{SID}"
    assert panel.recent_decisions[0].authority_kind == "synthetic"
    assert panel.recent_fills[0].authority_account_id == f"sim:{SID}"
    assert panel.recent_fills[0].authority_kind == "synthetic"
    assert panel.health.last_decision_at_ms == _NOW - 100
    assert panel.health.last_bar_at_ms == 1_699_999_999_900


def test_real_paper_decisions_and_fills_carry_real_paper_account_authority() -> None:
    """Issue #1729 AC #8: ``authority_kind`` must survive to every panel row,
    not only the Dry Run rows the pre-existing tests already covered. Before
    this fix, ``recent_decisions``/``recent_fills`` for a real Paper (mode
    ``trade``) bot carried ``authority_account_id=None``/``authority_kind=
    None`` — the field existed on the contract but was never populated for
    the real-money-adjacent path.
    """
    decision = decision_receipt(seq=1, ts_ms=_NOW, outcome="entered", reason_code="CROSS_UP")
    entries = [
        intent_entry(sid=SID, intent="open", ts_ms=_NOW - 1_000),
        submit_acked_entry(sid=SID, intent="open", ts_ms=_NOW - 900),
        fill_entry(sid=SID, intent="open", ts_ms=_NOW - 800),
    ]

    panel = _panel(_status(mode="trade"), _clerk_status(), entries, decision)

    assert panel.recent_decisions[0].authority_account_id == ACCT
    assert panel.recent_decisions[0].authority_kind == "real_paper"
    assert panel.recent_decisions[0].simulated is False
    assert panel.recent_fills[0].authority_account_id == ACCT
    assert panel.recent_fills[0].authority_kind == "real_paper"


def test_build_panel_honors_the_explicit_authority_account_id_from_the_selected_facade() -> None:
    """Production wiring (``panel_data_source._get_panel_with_entries_from_authority``)
    passes the exact account id of the SQLite facade it actually opened, not
    a recomputed guess — proving Gap 2's "cannot be mislabeled in the read
    path" claim. This pins that ``build_panel`` honors that explicit value
    rather than silently recomputing its own.
    """
    decision = decision_receipt(seq=5, ts_ms=_NOW - 50, outcome="entered", reason_code="CROSS_UP")
    activity = DryRunActivity(
        seq=1,
        strategy_instance_id=SID,
        run_id="run-dry",
        authority_account_id=f"sim:{SID}",
        authority_kind="synthetic",
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
        decision,
        dry_run_activity=[activity],
        authority_account_id=f"sim:{SID}",
    )

    assert panel.recent_decisions[0].authority_account_id == f"sim:{SID}"
    assert panel.recent_decisions[0].authority_kind == "synthetic"


def test_build_panel_rejects_dry_run_activity_from_a_different_synthetic_authority() -> None:
    """Issue #1729 AC #8: "mixed synthetic/real aggregate requests are
    rejected." Before this fix, ``_recent_activity_views`` trusted whatever
    ``dry_run_activity`` it was handed, with no check that its authority
    actually matched the bot being projected. If the WAL evidence resolved
    for a panel ever named a *different* strategy instance's synthetic
    authority (e.g. a resolver bug), the mixed aggregate would have rendered
    silently. This proves the new ``SingleAuthorityAggregate``-backed guard
    refuses to serve it instead.
    """
    foreign_activity = DryRunActivity(
        seq=1,
        strategy_instance_id="other-bot",
        run_id="run-dry",
        authority_account_id="sim:other-bot",
        authority_kind="synthetic",
        recorded_at_ms=_NOW - 100,
        bar_ref="SPY@1699999999900",
        intent="ENTER",
        order_ref="simulated:run-dry:1699999999900:ENTER",
        symbol="SPY",
        side="buy",
        quantity=1,
        fill_price=401.25,
    )

    with pytest.raises(MixedAuthorityAggregateError):
        _panel(
            _status(running=True, mode="dry_run"),  # SID == "bot-alpha"
            _clerk_status(),
            [],
            dry_run_activity=[foreign_activity],
        )


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
