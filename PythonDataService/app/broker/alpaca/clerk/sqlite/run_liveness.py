"""Retire an ACTIVE run whose runner is gone, on the Clerk's own clock (#2369).

The #2362 sweep step cancels every working ENTER whose run is not ACTIVE, so
it is only as good as ``runs.state``. A runner that dies without committing
``RUN_STOPPED`` -- a crash whose STOP commit failed, a task that vanished --
leaves its run ACTIVE, and that run's resting ENTER used to stay working at
the broker until the next Clerk restart retired every pre-restart run.

The evidence that a run's runner is alive is a liveness lease the Clerk holds
per ACTIVE run:

* The Clerk stamps it, with **its own clock**, when it admits the run and
  each time the run's runner renews it (:func:`renew_run_lease`). The runner
  supplies only the run identity it holds, which must name the ACTIVE run; it
  supplies no time (owner rule: never key evidence on a value the subject
  supplies).
* An ACTIVE run the Clerk has no stamp for -- one admitted by a path with no
  runner, or one it first sees after its in-memory book was rebuilt -- starts
  its lease at the first sweep that sees it. Every ACTIVE run must be held.
* The reconciliation sweep retires a run whose lease is older than
  :data:`RUN_LIVENESS_TTL_MS` by committing ``RUN_STOPPED``
  (:func:`retire_runs_whose_runner_is_gone`); the #2362 step later in the
  same pass cancels its working ENTERs. EXITs are never touched here.

The lease lives in memory, per repository, because it only has to outlive a
runner, never the Clerk: a Clerk restart already retires every pre-restart
run in ``recover()``.

Slow is not dead. The TTL is four renewal intervals, and the check and the
STOP run under the account intake fence that every renewal also takes, so a
renewal either lands before the check or finds the run already retired. A
runner that renews late onto a retired run is refused (``False``) and stops
its bot; the retired run's ENTERs are refused too, since ENTER admission
requires the ACTIVE run. Losing that race costs a stopped bot, never an
unmanaged order.
"""

from __future__ import annotations

import logging
import threading
from weakref import WeakKeyDictionary

from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

logger = logging.getLogger(__name__)

RUN_LIVENESS_RENEW_INTERVAL_S = 15.0
"""How often a live runner renews its run's lease (one sweep interval)."""

RUN_LIVENESS_TTL_MS = 60_000
"""A run unheard from for longer than this, by the Clerk's clock, is retired."""

RUNNER_LEASE_EXPIRED_REASON = "runner_lease_expired"
"""The ``operator_reason`` on the ``RUN_STOPPED`` this module commits."""


class _RunLivenessBook:
    """When the Clerk last heard from each ACTIVE run's runner, by its clock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamped_at_ms: dict[str, int] = {}

    def stamp(self, run_id: str, now_ms: int) -> None:
        with self._lock:
            self._stamped_at_ms[run_id] = now_ms

    def lapsed(self, active_run_ids: list[str], now_ms: int) -> list[str]:
        """The ACTIVE runs whose lease is past the TTL; forget every other run.

        A run with no stamp starts its lease now.
        """
        with self._lock:
            self._stamped_at_ms = {
                run_id: self._stamped_at_ms.get(run_id, now_ms) for run_id in active_run_ids
            }
            return [
                run_id
                for run_id, stamped_at_ms in self._stamped_at_ms.items()
                if now_ms - stamped_at_ms > RUN_LIVENESS_TTL_MS
            ]


_BOOKS: WeakKeyDictionary[ClerkSqliteRepository, _RunLivenessBook] = WeakKeyDictionary()
_BOOKS_GUARD = threading.Lock()


def _book(repo: ClerkSqliteRepository) -> _RunLivenessBook:
    with _BOOKS_GUARD:
        book = _BOOKS.get(repo)
        if book is None:
            book = _RunLivenessBook()
            _BOOKS[repo] = book
        return book


def renew_run_lease(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str,
    lifecycle_run_id: str,
) -> bool:
    """Stamp the run's lease now; ``False`` when that run is not ACTIVE.

    Call under the account intake fence, so the answer cannot interleave with
    :func:`retire_runs_whose_runner_is_gone`.
    """
    active = repo.active_run(strategy_instance_id)
    if active is None or active.lifecycle_run_id != lifecycle_run_id:
        return False
    _book(repo).stamp(active.run_id, repo.clock())
    return True


def retire_runs_whose_runner_is_gone(repo: ClerkSqliteRepository) -> None:
    """Commit ``RUN_STOPPED`` for every ACTIVE run whose lease has lapsed.

    Runs under the account intake fence, as one step of the reconciliation
    pass, before the #2362 step that cancels a non-ACTIVE run's ENTERs.
    """
    active_runs = [
        run
        for run in (
            repo.active_run(instance["strategy_instance_id"])
            for instance in repo.strategy_instances()
        )
        if run is not None
    ]
    lapsed = frozenset(_book(repo).lapsed([run.run_id for run in active_runs], repo.clock()))
    for run in active_runs:
        if run.run_id not in lapsed:
            continue
        submit_stop_run(
            repo,
            account_id=repo.account_id,
            strategy_instance_id=run.strategy_instance_id,
            lifecycle_run_id=run.lifecycle_run_id,
            operator_reason=RUNNER_LEASE_EXPIRED_REASON,
            clock=repo.clock,
        )
        logger.warning(
            "retired an ACTIVE run whose runner stopped renewing its lease",
            extra={
                "action": "run_liveness_lease_expired",
                "account_id": repo.account_id,
                "strategy_instance_id": run.strategy_instance_id,
                "run_id": run.run_id,
                "ttl_ms": RUN_LIVENESS_TTL_MS,
            },
        )


__all__ = [
    "RUNNER_LEASE_EXPIRED_REASON",
    "RUN_LIVENESS_RENEW_INTERVAL_S",
    "RUN_LIVENESS_TTL_MS",
    "renew_run_lease",
    "retire_runs_whose_runner_is_gone",
]
