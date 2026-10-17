"""Explicit Options recovery keeps money, new-run fencing and mirror replay."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.models import EffectOperationState
from app.broker.alpaca.clerk.sqlite.options_resume import submit_options_resume
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.schemas.deployment_budget import DeploymentBudgetInput
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.options_bot_resume_budget import preview_options_resume_budget, resolve_options_resume_consent
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import RUN, SID, _DryRun
from tests.broker.alpaca.clerk.test_options_leg_custody import (
    CALL,
    OTHER,
    _close,
    _enter,
    session_clerk,  # noqa: F401 -- pytest fixture
)

pytestmark = pytest.mark.usefixtures("budgeted_run_registration")


async def _resume(account: _DryRun, *, funded: bool = True, run_id: str = "after-interruption") -> BrokerBotBinding:
    binding = account.binding.model_copy(update={
        "run_id": run_id,
        "budget_consent": account.binding.budget_consent if funded else None,
    })
    async with account.clerk.start_admission_snapshot(SID) as snapshot:
        await account.clerk.resume_options_strategy_run(binding, admission_snapshot=snapshot)
    await account.clerk.record_options_resume_launch(binding)
    return binding


async def test_resume_keeps_lifetime_profit_and_original_consent_with_a_new_run(session_clerk: _DryRun) -> None:  # noqa: F811
    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    assert (await _enter(account, "first", CALL)).state is EffectOperationState.SUBMITTED
    account.wait(1000)
    assert (await _close(account, "first", CALL)).state is EffectOperationState.FLAT
    await account.read_account()
    before = account.repo.account_budget(cash=10_000, seen_before_ms=account.clock() + 1).deployments[0]
    original = account.repo.deployment_budget(SID)
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="INTERRUPTED_BY_RESTART")

    resumed = await _resume(account)

    after = account.repo.account_budget(cash=10_000, seen_before_ms=account.clock() + 1).deployments[0]
    assert after.active
    assert (after.balance, after.realized_gross, after.fees) == (before.balance, before.realized_gross, before.fees)
    assert after.realized_gross == Decimal("6")
    budget = account.repo.deployment_budget(SID)
    for name in ("command_id", "run_id", "committed_cents", "configuration_hash", "actor", "committed_at_ms"):
        assert budget[name] == original[name]
    assert budget["active_run_id"] != budget["run_id"]
    assert account.repo.active_run(SID).lifecycle_run_id == resumed.run_id
    assert account.clerk.options_entries_enabled(SID)
    assert (await _enter(account, "second", OTHER, run_id=resumed.run_id)).state is EffectOperationState.SUBMITTED


async def test_manage_only_resume_closes_held_allocations_and_refuses_every_entry(session_clerk: _DryRun) -> None:  # noqa: F811
    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    assert (await _enter(account, "first", CALL)).state is EffectOperationState.SUBMITTED
    await account.read_account()
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="INTERRUPTED_BY_RESTART")

    resumed = await _resume(account, funded=False)

    assert not account.clerk.options_entries_enabled(SID)
    assert not account.repo.account_budget(cash=10_000, seen_before_ms=account.clock() + 1).deployments[0].active
    refused = await _enter(account, "second", OTHER, run_id=resumed.run_id)
    assert refused.state is EffectOperationState.REJECTED
    assert "OPTIONS_RESUME_MANAGE_ONLY" in refused.explanation
    assert (await _close(account, "first", CALL, run_id=resumed.run_id)).state is EffectOperationState.FLAT
    assert account.held() == {}


async def test_resume_transport_retry_does_not_reopen_a_stopped_new_run(session_clerk: _DryRun) -> None:  # noqa: F811
    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="INTERRUPTED_BY_RESTART")
    resumed = await _resume(account)
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=resumed.run_id, reason="owner_stop")

    retry = submit_options_resume(account.repo, resumed, envelope=account.clerk.live_envelope)

    assert not retry.created
    assert account.repo.active_run(SID) is None
    assert account.repo.deployment_budget(SID)["released_at_ms"] is not None


async def test_resume_is_rebuilt_from_the_same_custody_mirror(session_clerk: _DryRun, tmp_path: Path) -> None:  # noqa: F811
    import shutil

    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    await _enter(account, "first", CALL)
    await account.read_account()
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="INTERRUPTED_BY_RESTART")
    resumed = await _resume(account, funded=False)
    expected = account.repo.deployment_budget(SID)
    snapshot_root = tmp_path.parent / (tmp_path.name + "-rebuild")
    shutil.copytree(account.artifacts_root, snapshot_root)
    copied_db = snapshot_root / account.repo.db_path.relative_to(account.artifacts_root)
    copied_db.rename(copied_db.with_suffix(".preserved"))
    for suffix in ("-wal", "-shm"):
        Path(str(copied_db) + suffix).unlink(missing_ok=True)

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=account.repo.account_id, artifacts_root=snapshot_root, clock=account.clock,
    )
    try:
        assert rebuilt.deployment_budget(SID) == expected
        assert rebuilt.active_run(SID).lifecycle_run_id == resumed.run_id
        assert rebuilt.position(SID, CALL.occ_symbol) == 1
    finally:
        rebuilt.close()


async def test_resume_review_reclaims_only_remaining_cash_and_consent_is_guard_safe(
    session_clerk: _DryRun, monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import app.services.options_bot_resume_budget as review_module

    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    await _enter(account, "first", CALL)
    await account.read_account()
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="INTERRUPTED_BY_RESTART")
    runtime = get_clerk_runtime(account.repo.account_id)

    @asynccontextmanager
    async def connected_runtime(_broker: str, _sid: str):
        yield runtime

    monkeypatch.setattr(review_module, "bot_clerk_runtime", connected_runtime)
    monkeypatch.setattr(review_module, "get_broker_configuration_service", lambda: SimpleNamespace(
        owner=lambda: SimpleNamespace(owner_id="test-owner"),
    ))
    budget = DeploymentBudgetInput(amount_usd="10000", risk_revision=0)

    preview = await preview_options_resume_budget(account.binding, budget)

    assert preview.budget_usd == "10000.00"
    assert "9805.94" in preview.detail
    async with account.clerk.start_admission_snapshot(SID):
        consent = await resolve_options_resume_consent(account.binding, budget.model_copy(update={"review_token": preview.review_token}))
    assert consent.committed_cents == 1_000_000
    assert consent.actor == "test-owner"
    with pytest.raises(BudgetUnavailable, match="Review"):
        await resolve_options_resume_consent(account.binding, budget.model_copy(update={"review_token": "different"}))


async def test_resume_spending_reports_missing_account_evidence_as_a_budget_refusal(
    session_clerk: _DryRun,  # noqa: F811
) -> None:
    account = session_clerk
    await account.clerk.record_deployment_launch(account.binding)
    await account.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN, reason="INTERRUPTED_BY_RESTART")
    runtime = get_clerk_runtime(account.repo.account_id)
    assert runtime is not None and runtime.envelope_sync is not None
    runtime.envelope_sync.envelope.withdraw(reason="The fresh account read is unavailable.")
    with pytest.raises(BudgetUnavailable, match="LIVE_ENVELOPE_UNOBSERVED"):
        await resolve_options_resume_consent(account.binding, DeploymentBudgetInput(amount_usd="10000", risk_revision=0))
    assert account.repo.active_run(SID) is None
