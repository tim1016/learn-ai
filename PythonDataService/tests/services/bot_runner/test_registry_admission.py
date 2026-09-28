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

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.engine.live.bot_lifecycle_state import BotDutyOutcome, BotLifecyclePhase
from app.schemas.exit_terms import ExitTermsInput
from app.services.bot_binding_repository import (
    RunOutcomeConflictError,
)
from app.services.bot_runner import (
    CarryoverPolicyRefusedError,
    RunAdmissionRefusedError,
    UnknownBotError,
)
from tests._helpers.bot_runner.custody import (
    _SID,
    _T0,
    _custody_proof,
    _flat_custody_snapshot,
    _lifecycle_json,
    _registry,
    admission_guard_for,
)
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeFeed
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _current_run_json, _OrderingClerk


@asynccontextmanager
async def _fixed_start_guard(sid: str):
    yield _flat_custody_snapshot(sid, observed_at_ms=_T0), ProgramLegPolicy.regular_only(), ExitTermsInput(exit_allowance_bps=20, band_multiple=2, spread_cap_bps=50).seal()


@pytest.mark.asyncio
async def test_list_bots_filters_by_broker_tag(tmp_path: Path) -> None:
    feed = _FakeFeed([], mode="hold")
    registry = _registry(tmp_path, feed)
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

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
            exit_terms=DEPLOY_EXIT_TERMS, broker="ibkr",
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
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
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
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
    try:
        with pytest.raises(UnknownBotError):
            registry.status("ibkr", _SID)
    finally:
        await registry.stop("alpaca", _SID)


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
    registry = _registry(tmp_path, feed, start_custody_guard=admission_guard_for(clerk))
    set_alpaca_clerk(clerk)
    try:
        original_record_launch = registry._bindings.record_launch

        def crash_after_launch(*args: object, **kwargs: object) -> None:
            original_record_launch(*args, **kwargs)
            raise RuntimeError("injected crash, cleanup will fail too")

        monkeypatch.setattr(registry._bindings, "record_launch", crash_after_launch)

        with pytest.raises(RuntimeError, match="injected crash"):
            await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

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
        await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")

    assert registry.any_running() is False


@pytest.mark.asyncio
async def test_superseded_terminal_projection_keeps_the_run_receipt(tmp_path: Path) -> None:
    clerk = _CustodyClerk(_custody_proof(exposure={}))
    set_alpaca_clerk(clerk)
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
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
    await registry.deploy(exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca", strategy_instance_id=_SID, symbol="SPY")
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
async def test_carryover_rejects_a_new_deploy_without_an_enablement_switch(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))

    # Named for the broker, not the mode: carryover is disabled on every
    # Alpaca world, and the live one reaches this same refusal (slice 7).
    with pytest.raises(CarryoverPolicyRefusedError, match=r"globally disabled for Alpaca bots\."):
        await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            carryover_policy="ALLOW",
        )

    assert not (tmp_path / "live_state" / _SID).exists()


@pytest.mark.asyncio
async def test_fresh_deployment_forbids_carryover(tmp_path: Path) -> None:
    registry = _registry(tmp_path, _FakeFeed([], mode="hold"))
    deployed = await registry.deploy(
        exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
        strategy_instance_id=_SID,
        symbol="SPY",
        mode="dry_run",
    )

    assert deployed.carryover_policy == "FORBID"
    await registry.stop("alpaca", _SID)
