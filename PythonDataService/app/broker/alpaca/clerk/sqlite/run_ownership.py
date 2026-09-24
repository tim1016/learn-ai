"""Retire an ACTIVE run whose in-process runner is gone (#2369).

The #2362 sweep step cancels every working ENTER whose run is not ACTIVE, so
it is only as good as ``runs.state``. A runner whose supervise task ended
without committing ``RUN_STOPPED`` -- its STOP commit failed and the terminal
path logged and moved on -- left its run ACTIVE, and that run's resting ENTER
stayed working at the broker until the next Clerk restart.

The runner and the Clerk live in one process, so whether a run's runner is
still there is a fact the process can read directly; no timed heartbeat is
needed, and none could be trusted across a suspend or a wall-clock step:

* At admission the runner hands the Clerk an **owner** for the run -- any
  object with a ``done()`` answer, held weakly (the bot registry passes a
  future it resolves once the run's supervise task has ended). The Clerk
  records it under the account intake fence, in the same critical section
  that admits the run, so a run admitted with an owner is never seen unowned.
* Each reconciliation pass, under that same fence and before the #2362 step,
  commits ``RUN_STOPPED`` (reason :data:`RUNNER_GONE_REASON`) for every
  ACTIVE run whose owner is done or has been garbage-collected. A live owner
  is never retired, whatever any clock says.
* An ACTIVE run with **no** owner -- one admitted by a path with no runner,
  such as the published ``/runs/start`` custody route -- is retired fail
  closed after one pass's grace: the first pass that sees it unowned notes
  it, and the next pass still seeing it unowned retires it (reason
  :data:`NO_RUNNER_REASON`). Runs admitted before a Clerk restart never get
  here: ``recover()`` already retired every one of them.

EXITs are never touched here; a retired run's working ENTERs are cancelled by
the #2362 step later in the same pass.

The book is in memory, per Clerk facade, because it only has to outlive a
runner, never the Clerk.
"""

from __future__ import annotations

import logging
import weakref
from typing import Protocol

from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

logger = logging.getLogger(__name__)

RUNNER_GONE_REASON = "runner_gone"
"""The ``operator_reason`` for a run whose registered owner has ended."""

NO_RUNNER_REASON = "no_runner"
"""The ``operator_reason`` for a run that stayed unowned for a whole pass."""

type _RunKey = tuple[str, str]
"""``(strategy_instance_id, lifecycle_run_id)``."""


class RunOwner(Protocol):
    """What holds an ACTIVE run in this process; ``done()`` once it has let go.

    ``done()`` is read off the event loop, under the intake fence, so it must
    be a plain state read (an ``asyncio.Future``'s is).
    """

    def done(self) -> bool: ...


class RunOwnership:
    """Which in-process owner holds each ACTIVE run; read and written under intake."""

    def __init__(self) -> None:
        self._owners: dict[_RunKey, weakref.ReferenceType[RunOwner]] = {}
        self._unowned_last_pass: set[_RunKey] = set()

    def hold(self, *, strategy_instance_id: str, lifecycle_run_id: str, owner: RunOwner) -> None:
        """Record ``owner`` as the run's holder; call under intake at admission."""
        key = (strategy_instance_id, lifecycle_run_id)
        self._owners[key] = weakref.ref(owner)
        self._unowned_last_pass.discard(key)

    def runs_to_retire(self, active: set[_RunKey]) -> list[tuple[_RunKey, str]]:
        """The ACTIVE runs to stop this pass, each with its reason.

        Forgets every run that is no longer ACTIVE. An unowned run is retired
        only when the previous pass already saw it unowned.
        """
        self._owners = {key: ref for key, ref in self._owners.items() if key in active}
        unowned_last_pass = self._unowned_last_pass
        self._unowned_last_pass = set()
        retire: list[tuple[_RunKey, str]] = []
        for key in sorted(active):
            ref = self._owners.get(key)
            if ref is not None:
                owner = ref()
                if owner is None or owner.done():
                    retire.append((key, RUNNER_GONE_REASON))
            elif key in unowned_last_pass:
                retire.append((key, NO_RUNNER_REASON))
            else:
                self._unowned_last_pass.add(key)
        return retire


def retire_runs_whose_runner_is_gone(
    repo: ClerkSqliteRepository,
    ownership: RunOwnership,
) -> None:
    """Commit ``RUN_STOPPED`` for every ACTIVE run whose runner is gone.

    Runs under the account intake fence, as one step of the reconciliation
    pass, before the #2362 step that cancels a non-ACTIVE run's ENTERs.
    """
    active = {
        (run.strategy_instance_id, run.lifecycle_run_id)
        for run in (
            repo.active_run(instance["strategy_instance_id"])
            for instance in repo.strategy_instances()
        )
        if run is not None
    }
    for (strategy_instance_id, lifecycle_run_id), reason in ownership.runs_to_retire(active):
        submit_stop_run(
            repo,
            account_id=repo.account_id,
            strategy_instance_id=strategy_instance_id,
            lifecycle_run_id=lifecycle_run_id,
            operator_reason=reason,
            clock=repo.clock,
        )
        logger.warning(
            "retired an ACTIVE run whose in-process runner is gone",
            extra={
                "action": "run_retired_runner_gone",
                "account_id": repo.account_id,
                "strategy_instance_id": strategy_instance_id,
                "lifecycle_run_id": lifecycle_run_id,
                "reason": reason,
            },
        )


__all__ = [
    "NO_RUNNER_REASON",
    "RUNNER_GONE_REASON",
    "RunOwner",
    "RunOwnership",
    "retire_runs_whose_runner_is_gone",
]
