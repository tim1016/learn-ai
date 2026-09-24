"""A run whose runner is gone is retired by the Clerk on its own clock (#2369).

#2362 made the sweep cancel every working ENTER whose run is not ACTIVE. A run
whose runner died without committing ``RUN_STOPPED`` (a crash whose STOP
commit failed, a task that vanished) stayed ACTIVE, so its ENTER stayed
working at the broker until the next Clerk restart. The Clerk now keeps a
liveness lease per ACTIVE run, stamped on the Clerk's own clock whenever the
run's runner renews it; the sweep retires a run whose lease has lapsed, and
the #2362 step then cancels that run's working ENTERs in the same pass.
"""

from __future__ import annotations

from pathlib import Path

from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.run_liveness import RUN_LIVENESS_TTL_MS
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.utils.timestamps import now_ms_utc
from tests.broker.alpaca.clerk.sqlite.test_runtime import _binding
from tests.broker.alpaca.clerk.sqlite.test_stopped_run_entries import (
    _OpenOrdersBroker,
    _working_enter,
)

# The execution lease is a different fence; keep it out of the way while the
# Clerk clock is advanced past the run-liveness TTL.
_EXECUTION_LEASE_TTL_MS = 10**12


class _Clock:
    """The Clerk's own clock: frozen at construction, advanced only by the test."""

    def __init__(self) -> None:
        self._base_ms = now_ms_utc()
        self.offset_ms = 0

    def __call__(self) -> int:
        return self._base_ms + self.offset_ms


def _facade(
    tmp_path: Path, broker: _OpenOrdersBroker, clock: _Clock
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade]:
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path,
        clock=clock,
        lease_ttl_ms=_EXECUTION_LEASE_TTL_MS,
    )
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    return repo, facade


async def test_sweep_retires_a_run_whose_runner_stopped_renewing_and_cancels_its_enter(
    tmp_path: Path,
) -> None:
    """#2369: the runner died without RUN_STOPPED; the sweep still cancels its ENTER."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    clock = _Clock()
    repo, facade = _facade(tmp_path, broker, clock)
    order_ref = await _working_enter(facade)
    sid = _binding().strategy_instance_id

    # The runner is gone: nobody renews and nobody commits RUN_STOPPED.
    clock.offset_ms += 10 * 60_000
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(sid) is None
    assert broker.cancellations == [f"broker-{order_ref}"]
    assert repo.order(order_ref).broker_state == "canceled"
    repo.close()


async def test_a_renewing_runner_keeps_its_run_and_its_enter_past_the_ttl(
    tmp_path: Path,
) -> None:
    """A slow-but-alive runner that renews inside the TTL is never retired."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    clock = _Clock()
    repo, facade = _facade(tmp_path, broker, clock)
    order_ref = await _working_enter(facade)
    binding = _binding()

    for _ in range(3):
        clock.offset_ms += RUN_LIVENESS_TTL_MS - 1
        assert await facade.renew_run_lease(
            strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id
        )
        await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(binding.strategy_instance_id) is not None
    assert broker.cancellations == []
    assert repo.order(order_ref).broker_state == "accepted"
    repo.close()


async def test_sweep_retires_a_run_exactly_past_the_ttl_not_before(tmp_path: Path) -> None:
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    clock = _Clock()
    repo, facade = _facade(tmp_path, broker, clock)
    await _working_enter(facade)
    sid = _binding().strategy_instance_id

    clock.offset_ms += RUN_LIVENESS_TTL_MS
    await facade.reconcile_account(trigger="AUTOMATIC")
    assert repo.active_run(sid) is not None

    clock.offset_ms += 1
    await facade.reconcile_account(trigger="AUTOMATIC")
    assert repo.active_run(sid) is None
    repo.close()


async def test_a_retired_run_cannot_be_renewed_so_its_runner_learns_it_is_gone(
    tmp_path: Path,
) -> None:
    """The slow-runner race resolves closed: the late renewal is refused."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    clock = _Clock()
    repo, facade = _facade(tmp_path, broker, clock)
    await _working_enter(facade)
    binding = _binding()

    clock.offset_ms += RUN_LIVENESS_TTL_MS + 1
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert not await facade.renew_run_lease(
        strategy_instance_id=binding.strategy_instance_id, run_id=binding.run_id
    )
    assert repo.active_run(binding.strategy_instance_id) is None
    repo.close()


async def test_renewal_names_the_run_it_holds_not_just_the_bot(tmp_path: Path) -> None:
    """A renewal carrying another run's identity does not keep this run alive."""
    broker = _OpenOrdersBroker()
    broker.release_submit.set()
    clock = _Clock()
    repo, facade = _facade(tmp_path, broker, clock)
    await _working_enter(facade)
    sid = _binding().strategy_instance_id

    clock.offset_ms += RUN_LIVENESS_TTL_MS - 1
    assert not await facade.renew_run_lease(strategy_instance_id=sid, run_id="run-other")
    clock.offset_ms += 2
    await facade.reconcile_account(trigger="AUTOMATIC")

    assert repo.active_run(sid) is None
    repo.close()
