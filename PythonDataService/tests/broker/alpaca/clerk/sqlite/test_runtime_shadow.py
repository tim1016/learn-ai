"""The Clerk runs on the shadow authority: every synthesized fill is bound to the decision bar (ADR 0059 D2)."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
from app.broker.alpaca.clerk.account_authority import AccountAuthorityIdentityError
from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger, ShadowSessionRecorder
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.lean_sidecar.trading_calendar import session_open_ms_utc
from app.services.session_authority import declared_session_bounds
from app.services.source_bar_ledger import SourceBarLedger
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.alpaca.clerk.sqlite.test_runtime_program_leg import (
    _EXTENDED_POLICY,
    _binding,
    _closed_clock,
    _stream_health,
)
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, _Clock, _LiveRead, _retain

ACCOUNT_ID = "shadow:9LIVE0001"
SID = "spy-bot"
RUN_ID = "run-1"


def test_shadow_facade_requires_a_shadow_id_and_a_live_account(tmp_path: Path) -> None:
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    try:
        with pytest.raises(AccountAuthorityIdentityError, match="reads a live account"):
            SqliteAlpacaClerkFacade(
                repo=repo,
                read=_FakeReadPort(),
                trade=_FakeTradePort(),
                authority_kind="shadow",
                account_mode="paper",
            )
    finally:
        repo.close()
    paper = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    try:
        with pytest.raises(AccountAuthorityIdentityError, match="shadow: account identity"):
            SqliteAlpacaClerkFacade(
                repo=paper,
                read=_FakeReadPort(),
                trade=_FakeTradePort(),
                authority_kind="shadow",
                account_mode="live",
            )
    finally:
        paper.close()


async def test_shadow_entry_and_exit_are_synthesized_from_their_bound_decision_bars(
    tmp_path: Path,
) -> None:
    ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path
    )
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=600, close="100.25")
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    try:
        unproven = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-0",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=True,
            retained_source_bar=None,
        )
        assert unproven.state == "rejected" and "SIMULATED_SOURCE_BAR_UNPROVEN" in unproven.explanation

        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-1",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=True,
            retained_source_bar=decision,
        )
        assert receipt.state == "submitted", receipt.explanation
        # A bar-bound no-submit adapter is the source of this deterministic
        # execution, so its response must leave no missing-evidence work for a
        # later sweep or restart to discover.
        assert await facade.unresolved_effect_count(subject_id=f"bot:{SID}") == 0
        [order] = await ports.read.list_orders()
        assert (order.status, order.filled_avg_price, order.filled_at_ms) == (
            "filled",
            100.25,
            decision.end_ms,
        )
        assert order.client_order_id is not None
        assert order.client_order_id.startswith("learn-ai/spy-bot/v1:")
        assert [position.symbol for position in await ports.read.list_positions()] == ["SPY"]

        exit_decision = _retain(evidence, minute=601, close="100.50")
        exit_receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-2",
            purpose=EffectPurpose.EXIT,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=True,
            retained_source_bar=exit_decision,
        )

        assert exit_receipt.state is EffectOperationState.FLAT, exit_receipt.explanation
        assert await facade.unresolved_effect_count(subject_id=f"bot:{SID}") == 0
        assert await ports.read.list_positions() == []
    finally:
        repo.close()
        evidence.close()


async def test_shadow_fill_survives_restart_and_reconciles_to_attributed_exposure(
    tmp_path: Path,
) -> None:
    ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path
    )
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=600, close="100.25")
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    receipt = await facade.execute_for_instance(
        strategy_instance_id=SID,
        run_id=RUN_ID,
        decision_id="decision-restart",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
        use_rth=True,
        retained_source_bar=decision,
    )
    assert receipt.state == "submitted", receipt.explanation
    repo.close()
    evidence.close()

    restarted_ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path
    )
    restarted_repo = ClerkSqliteRepository.open(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    restarted = SqliteAlpacaClerkFacade(
        repo=restarted_repo,
        read=restarted_ports.read,
        trade=restarted_ports.trade,
        authority_kind="shadow",
        account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(restarted_ports.read),
    )
    try:
        await restarted.recover()
        proof = await restarted.prove_instance_custody(SID)

        assert proof.reconciliation_verdict == "clean"
        assert proof.exposure == {"SPY": 1.0}
    finally:
        restarted_repo.close()


async def test_shadow_safe_flatten_binds_retained_evidence_and_finishes_flat(
    tmp_path: Path,
) -> None:
    ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path
    )
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=600, close="100.25")
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
        program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
    )
    binding = _binding(use_rth=True).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    try:
        entered = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-before-recovery",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=True,
            retained_source_bar=decision,
        )
        assert entered.state == "submitted", entered.explanation
        await facade.stop_strategy_run(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            reason="test-safe-flatten",
        )
        await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            context = reader.recovery_context(strategy_instance_id=SID)
        finally:
            reader.close()
        assert context is not None
        capability = {
            item.action_id: item for item in build_recovery_catalog(context)
        }["execute_safe_flatten"]
        assert capability.available, capability.unavailable_reason
        assert capability.reduction_plan is not None

        result = await facade.execute_safe_flatten(plan=capability.reduction_plan)
        proof = await facade.prove_instance_custody(SID)

        assert len(result.orders) == 1
        assert proof.reconciliation_verdict == "clean"
        assert proof.exposure == {}
        assert await ports.read.list_positions() == []
    finally:
        repo.close()
        evidence.close()


async def _extended_enter_on_a_closed_clock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream_health: StreamHealthGate | None,
) -> tuple[EffectOperationState, str]:
    """Drive one POST-session shadow ENTER while the broker clock reports CLOSED."""
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _closed_clock)
    evidence = SourceBarLedger(artifacts_root=tmp_path, account_id="shadow-evidence:spy-bot")
    decision = _retain(evidence, minute=17 * 60, close="100.00", phase="POST")
    ports = compose_shadow_ports(
        live_read=_LiveRead(),
        live_account_id="9LIVE0001",
        artifacts_root=tmp_path,
        clock=lambda: decision.end_ms,
    )
    # The runtime proves the extended phase at the *repository's* clock, so
    # pinning it to the decision bar's close is what keeps this verdict
    # independent of the hour the suite happens to run at.
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=lambda: decision.end_ms
    )
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
        stream_health=stream_health,
        # The extended allowances are deployment configuration the shadow
        # read port cannot supply here (ALPACA_LIVE_XH_* is unset in tests),
        # so the sqlite case's own policy is reused verbatim: the two paths
        # must be compared on the liveness gate, nothing else.
        program_leg_policy=_EXTENDED_POLICY,
    )
    binding = _binding(use_rth=False).model_copy(update={"sealed_account_id": ACCOUNT_ID})
    await facade.register_strategy_run(binding)
    try:
        receipt = await facade.execute_for_instance(
            strategy_instance_id=SID,
            run_id=RUN_ID,
            decision_id="decision-liveness",
            purpose=EffectPurpose.ENTER,
            action_plan=binding.action_plan,
            quantity=binding.quantity,
            use_rth=False,
            retained_source_bar=decision,
        )
    finally:
        repo.close()
        evidence.close()
    return receipt.state, receipt.explanation


async def test_a_closed_clock_refuses_a_shadow_extended_enter_with_no_live_feed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shadow world reproduces the live Clerk's own liveness refusal (ADR 0059 D2).

    Mirrors ``test_runtime_program_leg.py``'s sqlite case: the declared window
    resolves POST, so the schedule alone would admit this ENTER, and with no
    stream-health gate installed nothing proves the venue is printing. A
    refusal the real Clerk would make must appear in the paper twin too, or
    the two stop reconciling trade-for-trade.
    """
    state, explanation = await _extended_enter_on_a_closed_clock(
        tmp_path, monkeypatch, stream_health=None
    )

    assert state is EffectOperationState.REJECTED
    assert explanation.startswith("MARKET_LIVENESS_BLOCKED:")


async def test_a_closed_clock_admits_a_shadow_extended_enter_when_the_feed_is_printing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ENTER, with live evidence the venue is printing, is admitted."""
    state, explanation = await _extended_enter_on_a_closed_clock(
        tmp_path, monkeypatch, stream_health=_stream_health(market_data_healthy=True)
    )

    assert state is not EffectOperationState.REJECTED, explanation


async def test_a_shadow_sweep_pass_journals_the_trading_day(tmp_path: Path) -> None:
    """The recorder is reached through the sweep's own ``on_result``, not only unit-called.

    Three properties nothing else pins: the listener really is composed into
    ``ReconciliationSweep``; exactly one ``day_opened`` row is appended for a
    trading-day instant; and the publisher runs *before* the listener observes
    it, so the facade carries the verdict the journal recorded.
    """
    ports = compose_shadow_ports(
        live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path
    )
    window = ports.read.capabilities().extended_hours_window
    # Both instants come from the canonical calendar, never the wall clock and
    # never a session-time literal: one minute into a known trading day, then
    # that day's declared close.
    open_ms = session_open_ms_utc(DAY)
    bounds = declared_session_bounds(DAY, window)
    assert bounds is not None
    clock = _Clock(open_ms + 60_000)
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        authority_kind="shadow",
        account_mode="live",
    )
    recorder = ShadowSessionRecorder(
        ledger=ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT_ID),
        window=window,
        clock=clock,
    )
    publish = facade.publish_sweep_reconciliation
    sweep = ReconciliationSweep(
        repo=repo,
        read=ports.read,
        trade=ports.trade,
        intake=facade.intake,
        on_result=lambda result: recorder.record(publish(result)),
    )
    try:
        assert await sweep._run_one_pass() is True
        opened = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT_ID).rows()
        assert [(row.kind, row.verdict) for row in opened] == [("day_opened", "clean")]
        # The sweep-attributed publisher ran before the recorder observed the
        # result: only `publish_sweep_reconciliation` stamps this timestamp.
        assert facade.recovery_evaluation_observation().last_pass_completed_at_ms is not None

        clock.now_ms = bounds.close_ms
        assert await sweep._run_one_pass() is True
    finally:
        await sweep.stop()
        repo.close()

    ledger = ShadowSessionLedger(artifacts_root=tmp_path, account_id=ACCOUNT_ID)
    assert [row.kind for row in ledger.rows()] == ["day_opened", "session_closed_clean"]
    assert ledger.completed_session_opens() == (open_ms,)
