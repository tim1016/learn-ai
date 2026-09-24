"""The runner holds its run's Clerk liveness lease for exactly as long as it lives (#2369).

The Clerk retires an ACTIVE run whose lease lapses (see
``tests/broker/alpaca/clerk/sqlite/test_run_liveness.py``). These tests pin
the runner's half: a live bot renews; a bot whose task ended stops renewing,
so the Clerk's own clock can retire its run; and a bot the Clerk has already
retired (a slow runner that lost the race) stops instead of running on.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.services import bot_clerk_lifecycle
from app.services.bot_runner import BotTaskRegistry
from tests._helpers.bot_runner.custody import _SID, _custody_proof, _registry
from tests._helpers.canary_admission import admit_canary_pairing

from ._support import _OrderingClerk, _wait_for
from .test_registry_shutdown_ordering import _StopOrderingFeed


class _LeaseCountingClerk(_OrderingClerk):
    def __init__(self) -> None:
        super().__init__(_custody_proof(exposure={}))
        self.renewals: list[str] = []

    async def renew_run_lease(self, *, strategy_instance_id: str, run_id: str) -> bool:
        self.renewals.append(run_id)
        return await super().renew_run_lease(
            strategy_instance_id=strategy_instance_id, run_id=run_id
        )


@pytest.fixture
def _fast_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot_clerk_lifecycle, "RUN_LEASE_RENEW_INTERVAL_S", 0.01)


async def _deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clerk: _LeaseCountingClerk
) -> tuple[BotTaskRegistry, _StopOrderingFeed, str]:
    feed = _StopOrderingFeed(clerk)
    registry = _registry(tmp_path, feed, start_custody_guard=clerk.start_admission_snapshot)
    set_alpaca_clerk(clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    deployed = await registry.deploy(
        broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade"
    )
    return registry, feed, deployed.active_run_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_lease")
async def test_a_live_bot_renews_its_run_lease_and_a_stopped_one_stops_renewing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clerk = _LeaseCountingClerk()
    try:
        registry, _feed, run_id = await _deploy(tmp_path, monkeypatch, clerk)
        await _wait_for(lambda: len(clerk.renewals) >= 3)
        assert set(clerk.renewals) == {run_id}

        await registry.stop("alpaca", _SID)
        renewed_by_stop = len(clerk.renewals)
        await asyncio.sleep(0.05)

        assert len(clerk.renewals) == renewed_by_stop
        assert not [
            task for task in asyncio.all_tasks() if task.get_name() == f"run-lease:{_SID}"
        ]
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_lease")
async def test_a_crashed_bot_whose_stop_commit_failed_stops_renewing_its_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2369's trigger: the run stays ACTIVE, but nobody holds it any more."""
    clerk = _LeaseCountingClerk()
    try:
        registry, _feed, run_id = await _deploy(tmp_path, monkeypatch, clerk)
        await _wait_for(lambda: len(clerk.renewals) >= 1)
        clerk.fail_stop = True

        registry._bots[_SID].task.cancel()  # a kill: the STOP commit then fails
        await _wait_for(lambda: _SID not in registry._bots)
        renewed = len(clerk.renewals)
        await asyncio.sleep(0.05)

        assert clerk.active_runs[_SID] == run_id  # the Clerk still has it ACTIVE
        assert len(clerk.renewals) == renewed  # so only its lease can retire it
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_lease")
async def test_a_bot_whose_run_the_clerk_retired_stops_with_run_lease_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slow runner that lost the race stops rather than trading on a dead run."""
    clerk = _LeaseCountingClerk()
    try:
        registry, _feed, _run_id = await _deploy(tmp_path, monkeypatch, clerk)
        await _wait_for(lambda: len(clerk.renewals) >= 1)

        clerk.active_runs.pop(_SID)  # the sweep retired the run
        await _wait_for(lambda: _SID not in registry._bots)

        view = registry.status("alpaca", _SID)
        assert view.running is False
        assert view.duty_outcome is not None
        assert view.duty_outcome.kind == "EXITED_UNVERIFIED"
        assert view.duty_outcome.reason_code == "RUN_LEASE_EXPIRED"
        assert view.desired_state == "STOPPED"
        assert clerk.stopped_runs == []  # the Clerk's STOP is the only STOP
    finally:
        set_alpaca_clerk(None)
