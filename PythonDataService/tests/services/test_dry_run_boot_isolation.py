"""A Dry Run that cannot be restored at boot is that bot's failure, never the lane's (#2582).

2026-09-29: a quick ``podman restart`` of the Live clerk crash-looped the
real-money process five times. Boot recovery reopened a Dry Run bot's own
``sim:`` account while the dead process's execution lease on it was still
live, and the ``ExecutionLeaseHeld`` it raised aborted the whole application
-- after the Live account authority had already installed.

These tests drive the runner's real boot recovery against a real synthetic
authority and a real account authority. Only the other process is simulated:
a second handle on the Dry Run's account, opened under a foreign lease owner.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    close_synthetic_clerk_runtimes,
    get_alpaca_clerk,
    get_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import bot_binding_authority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import BotTaskRegistry
from app.services.broker_v2_panel.bot_custody import binding_clerk_runtime
from app.services.broker_v2_panel.panel_errors import PanelUnavailableError
from app.utils.timestamps import now_ms_utc
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.test_active_authority import _Broker

SID = "live-dry-dv-spy-0928"
SIM_ACCOUNT = f"sim:{SID}"


def _dry_run_binding() -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID, strategy_key="deployment_validation", broker="alpaca", symbol="SPY",
        mode="dry_run", quantity=1, action_plan=alpaca_v1_action_plan("SPY"), run_id="run-1", created_at_ms=0,
        sealed_account_id=SIM_ACCOUNT, exit_terms=TERMS, budget_consent=DeployBudgetConsent(
            committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed",
            world="synthetic",
        ),
    )


@dataclass(frozen=True)
class _Lane:
    registry: BotTaskRegistry
    account: SqliteAlpacaClerkFacade
    artifacts_root: Path


@pytest.fixture
async def restarted_lane(tmp_path: Path) -> AsyncIterator[_Lane]:
    """A lane whose account authority installed, restarted with one deployed Dry Run.

    The Deploy happened in the process that died: it recorded the bot's
    binding and activated its ``sim:`` account. The restarted process has
    installed its account authority and has not yet run boot recovery.
    """
    broker = _Broker()
    account_repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "account")
    account = SqliteAlpacaClerkFacade(repo=account_repo, read=broker, trade=broker, account_mode="paper")  # type: ignore[arg-type]
    binding = _dry_run_binding()
    previous = BotTaskRegistry(tmp_path, feed_resolver=lambda: None, boot_recovery_required=False)
    previous._bindings.record_launch(binding, launch_reason="deploy")
    deployed = previous._authority_for(binding)
    await deployed.ensure_recoverable()
    await deployed.release_if_unused()
    assert get_clerk_runtime(SIM_ACCOUNT) is None
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=account, _sqlite_repository=account_repo)
    )
    try:
        yield _Lane(
            registry=BotTaskRegistry(tmp_path, feed_resolver=lambda: None),
            account=account,
            artifacts_root=tmp_path,
        )
    finally:
        await close_synthetic_clerk_runtimes()
        set_active_clerk_runtime(None)
        account_repo.close()


def _another_process_holds_the_dry_run(artifacts_root: Path, *, lease_ttl_ms: int) -> ClerkSqliteRepository:
    return ClerkSqliteRepository.open(
        account_id=SIM_ACCOUNT,
        artifacts_root=artifacts_root,
        lease_owner="boot:another-process",
        lease_ttl_ms=lease_ttl_ms,
    )


async def test_a_dry_run_whose_lease_another_process_holds_leaves_the_lane_started(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: the Dry Run's lease outlives boot's wait, and the lane still starts."""
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S", 0.01)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        report = await restarted_lane.registry.run_boot_recovery(
            recover=restarted_lane.account.recover,
            reconcile=restarted_lane.account.reconcile_once,
        )
    finally:
        holder.close()

    # The account authority installed before boot recovery still serves.
    assert get_alpaca_clerk() is restarted_lane.account
    # The Dry Run is named as its own failure, with the cause.
    assert [failure.strategy_instance_id for failure in report.unrecovered_dry_runs] == [SID]
    assert "execution lease is held by another live process" in report.unrecovered_dry_runs[0].detail
    # Nothing else is blamed on it: no bot is left waiting for the account authority.
    assert report.authority_unavailable_instances == ()


async def test_the_unrecovered_dry_run_refuses_start_with_its_own_cause(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surfaced on the bot: its Start names why its own simulated account was not restored."""
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S", 0.01)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        await restarted_lane.registry.run_boot_recovery(
            recover=restarted_lane.account.recover,
            reconcile=restarted_lane.account.reconcile_once,
        )
    finally:
        holder.close()

    preview = await restarted_lane.registry.preview_start_admission(
        broker="alpaca", strategy_instance_id=SID, symbol="SPY", mode="dry_run", exit_terms=TERMS,
    )

    assert preview.allowed is False
    assert preview.reason_code == "BOOT_RECOVERY_INCOMPLETE"
    assert "simulated account could not be restored" in preview.explanation
    assert "execution lease is held by another live process" in preview.explanation


async def test_the_unrecovered_dry_runs_panel_says_why_its_account_did_not_open(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surfaced on the bot: while the cause stands, the bot's own panel names it."""
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S", 0.01)
    holder = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=60_000)
    try:
        await restarted_lane.registry.run_boot_recovery(
            recover=restarted_lane.account.recover,
            reconcile=restarted_lane.account.reconcile_once,
        )
        with pytest.raises(PanelUnavailableError) as refused:
            async with binding_clerk_runtime(restarted_lane.registry, _dry_run_binding()):
                pass
    finally:
        holder.close()

    assert refused.value.http_status == 503
    assert "execution lease is held by another live process" in str(refused.value.detail)


async def test_boot_waits_out_its_dead_predecessors_lease_on_a_dry_run(
    restarted_lane: _Lane, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A quick restart: the dead process's lease expires inside boot's wait, and the Dry Run is restored."""
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_WAIT_TIMEOUT_S", 5.0)
    monkeypatch.setattr(bot_binding_authority, "BOOT_EXECUTION_LEASE_RETRY_INTERVAL_S", 0.02)
    # The dead process renewed its lease a moment before it died; nothing renews it now.
    predecessor = _another_process_holds_the_dry_run(restarted_lane.artifacts_root, lease_ttl_ms=200)
    started_ms = now_ms_utc()

    try:
        report = await restarted_lane.registry.run_boot_recovery(
            recover=restarted_lane.account.recover,
            reconcile=restarted_lane.account.reconcile_once,
        )

        assert report.unrecovered_dry_runs == ()
        assert get_clerk_runtime(SIM_ACCOUNT) is not None
        assert now_ms_utc() - started_ms >= 150
    finally:
        # The restored authority lets go first; the dead handle's close only
        # tidies a connection that no longer holds anything.
        await close_synthetic_clerk_runtimes()
        predecessor.close()
