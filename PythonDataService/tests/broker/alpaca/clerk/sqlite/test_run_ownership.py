"""A run whose in-process runner is gone is retired by the sweep (#2369).

#2362 made the sweep cancel every working ENTER whose run is not ACTIVE. A run
whose runner ended without committing ``RUN_STOPPED`` (its STOP commit failed)
stayed ACTIVE, so its ENTER stayed working at the broker until the next Clerk
restart. The runner now hands the Clerk an owner at admission; the sweep
retires a run whose owner is done, and the #2362 step then cancels that run's
working ENTERs in the same pass. No clock is involved, so a live owner can
never be retired by a suspend or a wall-clock step.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import EffectPurpose
from app.broker.alpaca.clerk.sqlite import run_ownership
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, ExecutionLeaseLost
from app.broker.alpaca.clerk.sqlite.run_ownership import NO_RUNNER_REASON, RUNNER_GONE_REASON, RunOwnership
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.utils.timestamps import now_ms_utc
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding
from tests.broker.alpaca.clerk.sqlite.test_stopped_run_entries import (
    _OpenOrdersBroker,
    _working_enter,
)

# #2596: these ENTERs run on the default clock; keep it inside a session.
pytestmark = pytest.mark.usefixtures("wall_clock_in_session")

# The execution lease is a different fence; keep it out of the way while the
# Clerk clock jumps.
_EXECUTION_LEASE_TTL_MS = 10**12


class _Clock:
    """The Clerk's own clock: frozen at construction, advanced only by the test."""

    def __init__(self) -> None:
        self._base_ms = now_ms_utc()
        self.offset_ms = 0

    def __call__(self) -> int:
        return self._base_ms + self.offset_ms


def _facade(tmp_path: Path, clock: _Clock | None = None) -> tuple[
    ClerkSqliteRepository, SqliteAlpacaClerkFacade, _OpenOrdersBroker
]:
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
        clock=clock or _Clock(),
        lease_ttl_ms=_EXECUTION_LEASE_TTL_MS,
    )
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    return repo, facade, broker


async def _owned_working_enter(
    facade: SqliteAlpacaClerkFacade, run_owner: asyncio.Future[None]
) -> str:
    binding = _binding()
    await facade.register_strategy_run(binding, run_owner=run_owner)
    receipt = await facade.execute_for_instance(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        decision_id="decision-1",
        purpose=EffectPurpose.ENTER,
        action_plan=binding.action_plan,
        quantity=binding.quantity,
    )
    return receipt.child_order_refs[0]


def _retire_reasons(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.reason
        for record in caplog.records
        if getattr(record, "action", None) == "run_retired_runner_gone"
    ]


async def test_sweep_retires_a_run_whose_owner_is_done_and_cancels_its_enter(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """#2369: the runner ended without RUN_STOPPED; the next pass cancels its ENTER."""
    repo, facade, broker = _facade(tmp_path)
    run_owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    order_ref = await _owned_working_enter(facade, run_owner)
    sid = _binding().strategy_instance_id

    run_owner.set_result(None)  # the supervise task ended; its STOP never landed
    with caplog.at_level(logging.WARNING):
        await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(sid) is None
    assert _retire_reasons(caplog) == [RUNNER_GONE_REASON]
    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_a_live_owner_is_never_retired_across_a_clerk_clock_jump(
    tmp_path: Path,
) -> None:
    """M1: a suspend or an NTP step moves the Clerk clock; a live runner stays."""
    clock = _Clock()
    repo, facade, broker = _facade(tmp_path, clock)
    run_owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    order_ref = await _owned_working_enter(facade, run_owner)
    sid = _binding().strategy_instance_id

    for _ in range(3):
        clock.offset_ms += 7 * 24 * 60 * 60 * 1000
        await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(sid) is not None
    assert broker.cancellations == []
    assert repo.order(order_ref).broker_state == "accepted"
    repo.close()


async def test_a_garbage_collected_owner_counts_as_gone(tmp_path: Path) -> None:
    repo, facade, _broker = _facade(tmp_path)
    run_owner: asyncio.Future[None] | None = asyncio.get_running_loop().create_future()
    assert run_owner is not None
    await _owned_working_enter(facade, run_owner)
    sid = _binding().strategy_instance_id

    run_owner = None
    gc.collect()
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(sid) is None
    repo.close()


async def test_an_unowned_run_is_retired_after_one_pass_of_grace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: a run no in-process runner holds is retired."""
    repo, facade, broker = _facade(tmp_path)
    order_ref = await _working_enter(facade)
    sid = _binding().strategy_instance_id

    await facade.reconcile_account(trigger="AUTOMATIC")
    assert repo.active_run(sid) is not None
    assert broker.cancellations == []

    with caplog.at_level(logging.WARNING):
        await facade.reconcile_account(trigger="AUTOMATIC")
    assert repo.active_run(sid) is None
    assert _retire_reasons(caplog) == [NO_RUNNER_REASON]
    assert broker.cancellations == [f"broker-{order_ref}"]
    repo.close()


class _GoneOwner:
    def done(self) -> bool:
        return True


_GONE_OWNER = _GoneOwner()


@dataclass(frozen=True)
class _ActiveRun:
    strategy_instance_id: str
    lifecycle_run_id: str


class _TwoRunsRepo:
    """Two ACTIVE runs, each held by an owner that is done."""

    account_id = "PA-TEST"

    def __init__(self) -> None:
        self.runs = {sid: _ActiveRun(sid, f"run-{sid}") for sid in ("bot-a", "bot-b")}

    def clock(self) -> int:
        return 0

    def strategy_instances(self) -> list[dict[str, str]]:
        return [{"strategy_instance_id": sid} for sid in self.runs]

    def active_run(self, strategy_instance_id: str) -> _ActiveRun:
        return self.runs[strategy_instance_id]


def _retire_with_stop_failing_for_bot_a(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> list[str]:
    stopped: list[str] = []

    def stop(repo: object, *, strategy_instance_id: str, **_: object) -> None:
        if strategy_instance_id == "bot-a":
            raise failure
        stopped.append(strategy_instance_id)

    monkeypatch.setattr(run_ownership, "submit_stop_run", stop)
    ownership = RunOwnership()
    for sid, run in _TwoRunsRepo().runs.items():
        ownership.hold(
            strategy_instance_id=sid, lifecycle_run_id=run.lifecycle_run_id, owner=_GONE_OWNER
        )
    run_ownership.retire_runs_whose_runner_is_gone(_TwoRunsRepo(), ownership)  # type: ignore[arg-type]
    return stopped


def test_one_runs_failing_stop_does_not_starve_the_next_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        stopped = _retire_with_stop_failing_for_bot_a(monkeypatch, ValueError("corrupt budget row"))

    assert stopped == ["bot-b"]
    failed = [r for r in caplog.records if getattr(r, "action", None) == "run_retire_runner_gone_failed"]
    assert [r.strategy_instance_id for r in failed] == ["bot-a"]


def test_a_lost_execution_lease_still_ends_the_retirement_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ExecutionLeaseLost):
        _retire_with_stop_failing_for_bot_a(monkeypatch, ExecutionLeaseLost("lease lost"))


async def test_an_owner_registered_during_the_grace_pass_keeps_the_run(
    tmp_path: Path,
) -> None:
    """Re-registering the ACTIVE run with an owner ends its unowned grace."""
    repo, facade, _broker = _facade(tmp_path)
    await _working_enter(facade)
    sid = _binding().strategy_instance_id

    await facade.reconcile_account(trigger="AUTOMATIC")
    run_owner: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    await facade.register_strategy_run(_binding(), run_owner=run_owner)
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(sid) is not None
    repo.close()
