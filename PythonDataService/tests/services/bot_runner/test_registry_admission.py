"""``BotTaskRegistry`` admission and resume: restart intensity, listing,
broker tags, resume/activation failures, run history, pause/continue, and
carryover policy.

Split from ``tests/services/test_bot_runner.py`` (issue #1737).
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import app.services.bot_runner as bot_runner
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.engine.live.bot_lifecycle_state import BotDutyOutcome, BotLifecyclePhase
from app.engine.live.desired_state import DesiredState
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    RunOutcomeConflictError,
    alpaca_v1_action_plan,
)
from app.services.bot_clerk_lifecycle import commit_stop_before_task_cancel
from app.services.bot_run_evidence import PROVISIONAL_STOP_REASON_CODE
from app.services.bot_runner import (
    BotTaskRegistry,
    CarryoverPolicyRefusedError,
    RestartIntensityRefusedError,
    RunAdmissionRefusedError,
    UnknownBotError,
)
from app.services.bot_runner_errors import (
    ActivationFailedCleanupProvenError,
    InvalidRunHistoryCursorError,
)
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import (
    _SID,
    _T0,
    _custody_proof,
    _flat_custody_snapshot,
    _lifecycle_json,
    _registry,
)
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed

from ._support import _current_run_json, _OrderingClerk


@asynccontextmanager
async def _fixed_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_T0)


@pytest.mark.asyncio
async def test_restart_intensity_refuses_thresholdth_start(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    policy = RestartIntensityPolicy(threshold=3, window_ms=300_000)
    registry = _registry(tmp_path, feed, policy=policy)
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))

    try:
        # Starts 1 and 2 pass (projected 1, 2 < 3); start 3 projects to the
        # threshold and is refused — mirrors project_restart_intensity_gate.
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await registry.stop("alpaca", _SID)
        await registry.resume_existing("alpaca", _SID)
        await registry.stop("alpaca", _SID)

        with pytest.raises(RestartIntensityRefusedError):
            await registry.resume_existing("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_restart_intensity_window_expiry_allows_restart(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold", observed_at_ms=_T0)
    policy = RestartIntensityPolicy(threshold=2, window_ms=1_000)
    clock = {"now": _T0}
    registry = BotTaskRegistry(
        tmp_path,
        feed_resolver=lambda: feed,
        restart_policy=policy,
        now_ms=lambda: clock["now"],
        boot_recovery_required=False,
        start_custody_guard=_fixed_start_guard,
    )
    set_alpaca_clerk(_CustodyClerk(_custody_proof(exposure={})))

    try:
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
        await registry.stop("alpaca", _SID)
        with pytest.raises(RestartIntensityRefusedError):
            await registry.resume_existing("alpaca", _SID)

        clock["now"] = _T0 + 2_000  # window has passed
        view = await registry.resume_existing("alpaca", _SID)
        assert view.running is True
        await registry.stop("alpaca", _SID)
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_list_bots_filters_by_broker_tag(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert [v.strategy_instance_id for v in registry.list_bots("alpaca")] == [_SID]
    assert registry.list_bots("ibkr") == []

    await registry.stop("alpaca", _SID)
    # Stopped bots remain on the roster (artifact-derived), just not running.
    listed = registry.list_bots("alpaca")
    assert len(listed) == 1
    assert listed[0].running is False


@pytest.mark.asyncio
async def test_runner_refuses_ibkr_binding_before_any_duty_artifact(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(RunAdmissionRefusedError, match="Alpaca"):
        await registry.deploy(
            broker="ibkr",
            strategy_instance_id=_SID,
            symbol="SPY",
        )

    assert not (tmp_path / "live_state" / _SID).exists()


@pytest.mark.asyncio
async def test_version_one_alpaca_binding_is_read_without_rewriting_audit_artifact(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    await registry.stop("alpaca", _SID)

    binding_path = tmp_path / "live_state" / _SID / "broker_binding.json"
    legacy = registry.binding_for_control("alpaca", _SID).model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy.pop("action_plan")
    original = json.dumps(legacy, separators=(",", ":"), sort_keys=True)
    instance_dir = tmp_path / "live_state" / _SID
    run_id = _current_run_json(tmp_path)["run_id"]
    (instance_dir / "strategy_instance.json").unlink()
    (instance_dir / "current_run.json").unlink()
    (instance_dir / "runs" / f"{run_id}.json").unlink()
    (instance_dir / "runs").rmdir()
    binding_path.write_text(original, encoding="utf-8")

    restarted = _registry(tmp_path, feed)
    listed = restarted.list_bots("alpaca")
    migrated = restarted.binding_for_control("alpaca", _SID)

    assert [view.strategy_instance_id for view in listed] == [_SID]
    assert migrated.schema_version == 2
    assert migrated.action_plan.on_enter[0].instrument.underlying == "SPY"
    assert migrated.action_plan.on_exit[0].entry_leg_id == "primary"
    assert binding_path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_status_for_wrong_broker_is_404(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    try:
        with pytest.raises(UnknownBotError):
            registry.status("ibkr", _SID)
    finally:
        await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_resume_existing_creates_new_run_and_preserves_action_plan(
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
        quantity=3,
    )
    original = registry.binding_for_control("alpaca", _SID)
    original_run_path = (
        tmp_path / "live_state" / _SID / "runs" / f"{original.run_id}.json"
    )
    original_run_bytes = original_run_path.read_bytes()
    await registry.stop("alpaca", _SID)
    resumed = await registry.resume_existing("alpaca", _SID)
    rebound = registry.binding_for_control("alpaca", _SID)
    current = registry.current_run("alpaca", _SID)
    history = registry.run_history("alpaca", _SID, cursor=None, limit=1)

    assert resumed.running is True
    assert resumed.active_run_id != deployed.active_run_id
    assert rebound.run_id == resumed.active_run_id
    assert rebound.mode == "log_only"
    assert rebound.quantity == 3
    assert rebound.action_plan == original.action_plan
    assert original_run_path.read_bytes() == original_run_bytes
    assert sorted(
        path.stem for path in (tmp_path / "live_state" / _SID / "runs").glob("*.json")
    ) == sorted([original.run_id, rebound.run_id])
    assert current.run_id == rebound.run_id
    assert current.is_current is True
    assert current.process is not None
    assert current.process.state == "RUNNING"
    assert [run.run_id for run in history.runs] == [original.run_id]
    assert history.runs[0].is_current is False
    assert history.runs[0].process is None
    assert history.runs[0].terminal_outcome is not None
    assert history.runs[0].terminal_outcome.kind == "STOPPED"
    assert history.next_cursor is None
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_resume_preserves_prior_outcome_before_current_pointer_advances(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    prior_run_id = registry.binding_for_control("alpaca", _SID).run_id
    await registry.stop("alpaca", _SID)

    original_record_launch = registry._bindings.record_launch

    def crash_after_current_pointer_write(*args: object, **kwargs: object) -> None:
        original_record_launch(*args, **kwargs)
        raise RuntimeError("injected crash after current run pointer write")

    monkeypatch.setattr(registry._bindings, "record_launch", crash_after_current_pointer_write)
    # PRD #1716 FR-6: this crash occurs after Clerk registration, and the
    # autouse _CustodyClerk fixture's stop succeeds, so it's reclassified as
    # a known, resolved failure rather than the raw RuntimeError.
    with pytest.raises(ActivationFailedCleanupProvenError) as exc_info:
        await registry.resume_existing("alpaca", _SID)
    assert "injected crash" in (exc_info.value.detail or "")

    restarted_registry = _registry(tmp_path, feed)
    history = restarted_registry.run_history("alpaca", _SID, cursor=None, limit=1)

    assert history.runs[0].run_id == prior_run_id
    assert history.runs[0].terminal_outcome is not None
    assert history.runs[0].terminal_outcome.reason_code == "OPERATOR_STOP"


@pytest.mark.asyncio
async def test_activation_failure_with_unproven_cleanup_keeps_raw_propagation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD #1716 FR-6: when the Clerk stop cannot be proven, the original
    exception propagates unchanged and is never reported as a resolved
    (known) failure."""
    clerk = _OrderingClerk(_custody_proof(exposure={}))
    clerk.fail_stop = True
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed, start_custody_guard=clerk.start_admission_snapshot)
    set_alpaca_clerk(clerk)
    try:
        original_record_launch = registry._bindings.record_launch

        def crash_after_launch(*args: object, **kwargs: object) -> None:
            original_record_launch(*args, **kwargs)
            raise RuntimeError("injected crash, cleanup will fail too")

        monkeypatch.setattr(registry._bindings, "record_launch", crash_after_launch)

        with pytest.raises(RuntimeError, match="injected crash"):
            await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

        assert registry.any_running() is False
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_activation_cancellation_is_never_reported_as_a_resolved_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PRD #1716 FR-6: asyncio.CancelledError is not Exception-typed, so it
    keeps raw propagation even when the Clerk stop cleanup succeeds."""
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)

    original_record_launch = registry._bindings.record_launch

    def cancel_after_launch(*args: object, **kwargs: object) -> None:
        original_record_launch(*args, **kwargs)
        raise asyncio.CancelledError()

    monkeypatch.setattr(registry._bindings, "record_launch", cancel_after_launch)

    with pytest.raises(asyncio.CancelledError):
        await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert registry.any_running() is False


@pytest.mark.asyncio
async def test_resume_does_not_preserve_provisional_stop_outcome(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    binding = registry.binding_for_control("alpaca", _SID)
    managed = registry._bots[_SID]
    managed.finalized = True
    managed.task.cancel()
    await asyncio.wait({managed.task})
    registry._terminal.reap(_SID, binding.run_id)
    registry._desired_repo(_SID).set(
        DesiredState.STOPPED,
        updated_by="test",
        now_ms=_T0,
        reason="operator_stop",
    )
    await commit_stop_before_task_cancel(binding, reason="operator_stop")
    registry._run_evidence.record_terminal(
        _SID,
        BotDutyOutcome(
            kind="STOPPED",
            reason_code=PROVISIONAL_STOP_REASON_CODE,
            recorded_at_ms=_T0,
            run_id=binding.run_id,
        ),
        updated_by="test",
        reason=PROVISIONAL_STOP_REASON_CODE,
        expected_active_run_id=binding.run_id,
        persist_receipt=False,
    )

    await registry.resume_existing("alpaca", _SID)
    history = registry.run_history("alpaca", _SID, cursor=None, limit=1)

    assert history.runs[0].run_id == binding.run_id
    assert history.runs[0].terminal_outcome is None
    assert not (
        tmp_path / "live_state" / _SID / "run_outcomes" / f"{binding.run_id}.json"
    ).exists()
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_superseded_terminal_projection_keeps_the_run_receipt(tmp_path: Path) -> None:
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    binding = registry.binding_for_control("alpaca", _SID)
    clerk.active_runs[_SID] = "run-new"
    clerk.known_runs.add((_SID, "run-new"))
    outcome = BotDutyOutcome(
        kind="CRASHED",
        reason_code="PROCESS_CRASHED",
        recorded_at_ms=_T0 + 2,
        run_id=binding.run_id,
    )

    result = registry._run_evidence.record_terminal(
        _SID,
        outcome,
        updated_by="test",
        reason="process.crashed",
        expected_active_run_id=binding.run_id,
    )

    assert result.status == "AUTHORITY_EXPECTATION_SUPERSEDED"
    assert registry._bindings.read_outcome(_SID, binding.run_id) is not None
    lifecycle = registry._lifecycle_repo(_SID).read()
    assert lifecycle is not None
    assert lifecycle.phase is BotLifecyclePhase.ON_DUTY
    assert lifecycle.active_run_id == "run-new"
    managed = registry._bots[_SID]
    managed.finalized = True
    managed.task.cancel()
    await asyncio.wait({managed.task})
    set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_conflicting_terminal_outcome_does_not_mutate_lifecycle(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    binding = registry.binding_for_control("alpaca", _SID)
    managed = registry._bots[_SID]
    managed.finalized = True
    managed.task.cancel()
    await asyncio.wait({managed.task})
    registry._terminal.reap(_SID, binding.run_id)
    recorded = BotDutyOutcome(
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=_T0,
        run_id=binding.run_id,
    )
    registry._run_evidence.record_terminal(
        _SID,
        recorded,
        updated_by="test",
        reason="OPERATOR_STOP",
    )
    lifecycle_before = _lifecycle_json(tmp_path)

    with pytest.raises(RunOutcomeConflictError):
        registry._run_evidence.record_terminal(
            _SID,
            recorded.model_copy(update={"kind": "CRASHED", "reason_code": "RuntimeError"}),
            updated_by="test",
            reason="RuntimeError",
        )

    assert _lifecycle_json(tmp_path) == lifecycle_before


@pytest.mark.asyncio
async def test_run_history_pages_previous_runs_without_changing_current_target(
    tmp_path: Path,
) -> None:
    ticks = iter(range(10_000))
    registry = _registry(
        tmp_path,
        _FakeFeed([], mode="hold"),
        now_ms=lambda: now_ms_utc() + next(ticks),
    )
    first = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )
    await registry.stop("alpaca", _SID)
    second = await registry.resume_existing("alpaca", _SID)
    await registry.stop("alpaca", _SID)
    third = await registry.resume_existing("alpaca", _SID)

    first_page = registry.run_history("alpaca", _SID, cursor=None, limit=1)
    second_page = registry.run_history(
        "alpaca",
        _SID,
        cursor=first_page.next_cursor,
        limit=1,
    )

    assert [run.run_id for run in first_page.runs] == [second.active_run_id]
    assert first_page.next_cursor == second.active_run_id
    assert [run.run_id for run in second_page.runs] == [first.active_run_id]
    assert second_page.next_cursor is None
    assert registry.current_run("alpaca", _SID).run_id == third.active_run_id
    other_sid = "alpaca-skeleton-2"
    await registry.deploy(broker="alpaca", strategy_instance_id=other_sid, symbol="SPY")
    await registry.stop("alpaca", other_sid)
    await registry.resume_existing("alpaca", other_sid)
    await registry.stop("alpaca", other_sid)
    await registry.resume_existing("alpaca", other_sid)
    foreign_page = registry.run_history("alpaca", other_sid, cursor=None, limit=1)

    assert foreign_page.next_cursor is not None
    with pytest.raises(InvalidRunHistoryCursorError):
        registry.run_history("alpaca", _SID, cursor=foreign_page.next_cursor, limit=1)
    await registry.stop("alpaca", other_sid)
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_pause_and_continue_keep_the_same_live_run_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    paused = await registry.pause("alpaca", _SID)
    continued = await registry.continue_paused("alpaca", _SID)

    assert paused.running is True
    assert paused.desired_state == "PAUSED"
    assert paused.active_run_id == deployed.active_run_id
    assert continued.running is True
    assert continued.desired_state == "RUNNING"
    assert continued.active_run_id == deployed.active_run_id
    assert registry.current_run("alpaca", _SID).run_id == deployed.active_run_id
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_continue_refuses_a_live_run_that_is_not_paused(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
    )

    with pytest.raises(RunAdmissionRefusedError, match=r"requires.*paused run"):
        await registry.continue_paused("alpaca", _SID)

    assert registry.status("alpaca", _SID).active_run_id == deployed.active_run_id
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_carryover_rejects_a_new_deploy_without_an_enablement_switch(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    with pytest.raises(CarryoverPolicyRefusedError, match="globally disabled"):
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            carryover_policy="ALLOW",
        )

    assert not (tmp_path / "live_state" / _SID).exists()


@pytest.mark.asyncio
async def test_default_start_status_exposes_carryover_as_disabled(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
        mode="dry_run",
    )

    assert deployed.carryover_account_policy_enabled is False
    await registry.stop("alpaca", _SID)


@pytest.mark.asyncio
async def test_resume_admission_wiring_persists_legacy_migration_clone_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the ``legacy_migration_repository=self._bindings``
    argument in ``BotTaskRegistry.__init__``'s construction of
    ``self._resume_admission`` (``app/services/bot_runner.py`` ~line 276).

    ``BotResumeAdmission`` computes the correct ``PROGRAM_BUILD_UNPROVEN``
    decision and clone id for a legacy instance whose persisted parameters no
    longer validate -- with or without that argument. Without it, the
    ``if mutating and self._legacy_migration_repository is not None:`` guard
    in ``app/services/bot_resume_admission.py`` silently skips the durable
    clone-lineage sidecar write (`bot_resume_admission_repository
    .ensure_legacy_migration_clone_lineage`), even though the decision itself
    still looks right. This drives Resume through the REAL registry-
    constructed ``self._resume_admission`` -- the exact object
    ``bot_runner.py.__init__`` builds -- so it fails if that argument is ever
    dropped and passes only because it is wired through.
    """
    from app.services.bot_start_admission import StartAdmissionDenied
    from app.services.signal_program_admission import legacy_migration_clone_instance_id

    # The autouse `_fresh_live_market_liveness` fixture patches
    # `current_strategy_validation_fact` to always report strategy_key
    # "deployment_validation" -- override it here so the validation evidence
    # matches this test's own strategy_key, which
    # `reconstruct_legacy_program_seal` checks.
    monkeypatch.setattr(
        bot_runner,
        "current_strategy_validation_fact",
        lambda binding, observed_at_ms: StrategyValidationAdmissionFact(
            state="VERIFIED",
            strategy_key=binding.strategy_key,
            evidence_status="accepted",
            event_id="test-validation-event",
            evidence_snapshot_sha256="a" * 64,
            verified_at_ms=observed_at_ms,
            explanation="Test validation evidence is current.",
        ),
    )
    registry = _registry(tmp_path, None)

    # A "legacy" prior binding: no v2 seal, and a parameter set (`gap`
    # negative) that no longer validates against the registered
    # `ema_crossover_signal` param schema -- unreconstructible, forcing the
    # clone path rather than an ordinary append-a-seal Resume.
    prior = BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        strategy_params={"gap": -1.0, "rsi_min": 50.0, "rsi_max": 70.0},
        sealed_account_id="paper-account",
        run_id="run-prior",
        created_at_ms=_T0,
    )
    registry._bindings.record_launch(prior, launch_reason="deploy")
    clone_id = legacy_migration_clone_instance_id(prior.strategy_instance_id)
    status = registry.status("alpaca", _SID)

    with pytest.raises(StartAdmissionDenied) as exc_info:
        await registry._resume_admission.resume(prior, status)

    assert exc_info.value.decision.reason_code == "PROGRAM_BUILD_UNPROVEN"
    assert clone_id in (exc_info.value.decision.next_step or "")

    lineage = registry._bindings.read_legacy_migration_lineage(clone_id)
    assert lineage is not None
    assert lineage.migrated_from_strategy_instance_id == _SID
