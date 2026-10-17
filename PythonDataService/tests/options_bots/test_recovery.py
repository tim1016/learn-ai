"""Registry recovery crosses the real Clerk and durable bot binding seam."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from app.schemas.options_deploy import OptionsResumeRequest
from app.schemas.run_admission import ProgramBuildAdmissionFact, StrategyValidationAdmissionFact
from app.services.bot_runner import BotTaskRegistry, RunAdmissionRefusedError
from app.services.bot_runner_errors import ActivationFailedCleanupProvenError
from tests._helpers.bot_runner.resume import _ResumeFeed
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import RUN, SID, _DryRun
from tests.broker.alpaca.clerk.test_options_leg_custody import (
    CALL,
    _enter,
    session_clerk,  # noqa: F401 -- pytest fixture
)

pytestmark = pytest.mark.usefixtures("budgeted_run_registration")


async def test_interrupted_session_no_longer_offers_resume_after_its_end(session_clerk: _DryRun) -> None:  # noqa: F811
    from app.engine.live.desired_state import DesiredState
    from app.schemas.bot_end import BotEnd
    from app.services.options_bot_status import options_session_view

    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    registry = BotTaskRegistry(account.artifacts_root, feed_resolver=lambda: _ResumeFeed(), now_ms=account.clock, boot_recovery_required=False)
    registry._bindings.record_launch(account.binding, launch_reason="deploy")
    registry._desired_repo(SID).set(
        DesiredState.RUNNING, updated_by="owner", now_ms=account.clock(),
        end=BotEnd(end_at_ms=account.clock() + 1_000, end_action="SELL"),
    )
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="options_interrupted:TEST_CRASH")
    registry._terminal.finalize(account.binding, kind="CRASHED", reason_code="TEST_CRASH")
    assert options_session_view(registry, "alpaca", SID).resume_allowed
    account.wait(1_000)

    state = options_session_view(registry, "alpaca", SID)

    assert not state.resume_allowed
    assert "end" in state.resume_explanation.lower()


async def test_registry_resumes_interrupted_options_with_original_custody_and_retries_stay_terminal(
    session_clerk: _DryRun, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import app.services.options_bot_admission as approval_port

    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    await _enter(account, "first", CALL)
    await account.read_account()
    registry = BotTaskRegistry(
        account.artifacts_root, feed_resolver=lambda: _ResumeFeed(),
        now_ms=account.clock, boot_recovery_required=False,
    )
    registry._bindings.record_launch(account.binding, launch_reason="deploy")
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="options_interrupted:TEST_CRASH")
    registry._terminal.finalize(account.binding, kind="CRASHED", reason_code="TEST_CRASH")

    async def unavailable_approval(binding, *, verified_at_ms):
        return (
            ProgramBuildAdmissionFact(state="UNPROVEN", program_key=binding.strategy_key, verified_at_ms=verified_at_ms, explanation="Offline approval store"),
            StrategyValidationAdmissionFact(state="UNVERIFIED", strategy_key=binding.strategy_key, verified_at_ms=verified_at_ms, evidence_status="blocked", explanation="Offline approval store"),
        )

    async def hold_runner(_binding, _feed, gate):
        await gate.wait()
        await asyncio.Event().wait()

    monkeypatch.setattr(approval_port, "options_start_facts", unavailable_approval)
    monkeypatch.setattr(registry, "_supervise", hold_runner)
    request = OptionsResumeRequest(idempotency_key="12345678-1234-1234-1234-123456789abc")
    original_budget = account.repo.deployment_budget(SID)
    db_path = account.repo.db_path

    def still_active() -> bool:
        connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        try:
            return connection.execute("SELECT 1 FROM runs WHERE strategy_instance_id=? AND state='ACTIVE'", (SID,)).fetchone() is not None
        finally:
            connection.close()
    try:
        resumed = await registry.resume_options_bot("alpaca", SID, request)

        assert resumed.active_run_id != RUN
        assert account.repo.active_run(SID).lifecycle_run_id == resumed.active_run_id
        assert account.repo.position(SID, CALL.occ_symbol) == 1
        assert account.repo.deployment_budget(SID)["committed_cents"] == original_budget["committed_cents"]
        assert not account.clerk.options_entries_enabled(SID)
        assert registry._bindings.read(SID).run_id == resumed.active_run_id

        await registry.stop("alpaca", SID)
        assert not still_active()
        replay = await registry.resume_options_bot("alpaca", SID, request)
        assert replay.active_run_id is None
        assert registry._bindings.read(SID).run_id == resumed.active_run_id
        assert not still_active()
        with pytest.raises(RunAdmissionRefusedError, match="deliberately stopped"):
            await registry.resume_options_bot("alpaca", SID, OptionsResumeRequest(idempotency_key="87654321-1234-1234-1234-123456789abc"))
    finally:
        await registry.stop_all()


@pytest.mark.parametrize("failure_stage", ["before_run", "after_run"])
async def test_resume_retry_settles_a_clerk_commit_when_the_run_projection_was_never_written(
    session_clerk: _DryRun, monkeypatch: pytest.MonkeyPatch, failure_stage: str,  # noqa: F811
) -> None:
    import app.services.options_bot_admission as approval_port

    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    registry = BotTaskRegistry(
        account.artifacts_root, feed_resolver=lambda: _ResumeFeed(), now_ms=account.clock, boot_recovery_required=False,
    )
    registry._bindings.record_launch(account.binding, launch_reason="deploy")
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="options_interrupted:TEST_CRASH")
    registry._terminal.finalize(account.binding, kind="CRASHED", reason_code="TEST_CRASH")

    async def approval_unavailable(binding, *, verified_at_ms):
        return (
            ProgramBuildAdmissionFact(state="UNPROVEN", program_key=binding.strategy_key, verified_at_ms=verified_at_ms, explanation="Offline approval store"),
            StrategyValidationAdmissionFact(state="UNVERIFIED", strategy_key=binding.strategy_key, verified_at_ms=verified_at_ms, evidence_status="blocked", explanation="Offline approval store"),
        )

    def broken_projection(*_args, **_kwargs):
        raise OSError("The run projection could not be written")

    monkeypatch.setattr(approval_port, "options_start_facts", approval_unavailable)
    monkeypatch.setattr(
        registry._bindings, "record_launch" if failure_stage == "before_run" else "_ensure_program_build_evidence",
        broken_projection,
    )
    request = OptionsResumeRequest(idempotency_key="12345678-1234-1234-1234-123456789abc")
    with pytest.raises(ActivationFailedCleanupProvenError):
        await registry.resume_options_bot("alpaca", SID, request)
    assert account.repo.active_run(SID) is None
    with pytest.raises(RunAdmissionRefusedError, match="stopped before launch") as retry:
        await registry.resume_options_bot("alpaca", SID, request)
    assert retry.value.reason_code == "OPTIONS_RESUME_NOT_STARTED"
    assert account.repo.active_run(SID) is None


async def test_committed_resume_reconciles_before_refusing_a_retry_when_its_projection_and_cleanup_failed(
    session_clerk: _DryRun, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    import app.services.bot_runner as runner_module
    import app.services.options_bot_admission as approval_port

    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    feed = _ResumeFeed()
    registry = BotTaskRegistry(
        account.artifacts_root, feed_resolver=lambda: feed, now_ms=account.clock, boot_recovery_required=False,
    )
    registry._bindings.record_launch(account.binding, launch_reason="deploy")
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="options_interrupted:TEST_CRASH")
    registry._terminal.finalize(account.binding, kind="CRASHED", reason_code="TEST_CRASH")

    async def approval_unavailable(binding, *, verified_at_ms):
        return (
            ProgramBuildAdmissionFact(state="UNPROVEN", program_key=binding.strategy_key, verified_at_ms=verified_at_ms, explanation="Offline approval store"),
            StrategyValidationAdmissionFact(state="UNVERIFIED", strategy_key=binding.strategy_key, verified_at_ms=verified_at_ms, evidence_status="blocked", explanation="Offline approval store"),
        )

    def broken_projection(*_args, **_kwargs):
        raise OSError("The run projection could not be written")

    async def broken_cleanup(*_args, **_kwargs):
        raise OSError("The Clerk Stop outcome could not be proved")

    monkeypatch.setattr(approval_port, "options_start_facts", approval_unavailable)
    monkeypatch.setattr(registry._bindings, "record_launch", broken_projection)
    monkeypatch.setattr(runner_module, "commit_stop_before_task_cancel", broken_cleanup)
    request = OptionsResumeRequest(idempotency_key="12345678-1234-1234-1234-123456789abc")
    with pytest.raises(OSError, match="run projection"):
        await registry.resume_options_bot("alpaca", SID, request)
    committed = account.repo.active_run(SID)
    assert committed is not None
    assert registry._bindings.read(SID).run_id == RUN
    feed = None

    with pytest.raises(RunAdmissionRefusedError, match="stopped before launch") as retried:
        await registry.resume_options_bot("alpaca", SID, request)
    assert retried.value.reason_code == "OPTIONS_RESUME_NOT_STARTED"
    assert account.repo.active_run(SID) is None
    assert account.clerk.options_resume_run_state(SID, committed.lifecycle_run_id) == "STOPPED"
