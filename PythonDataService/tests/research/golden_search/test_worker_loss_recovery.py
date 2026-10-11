"""Worker loss and uncertain-write recovery without double accounting (#3045, PRD #3034).

Fast tier: the evaluator over the in-memory reservation bridge — the five
fault boundaries (before reservation, reserved-before-dispatch, during
compute, delivered-before-persistence, commit-succeeded-ack-lost), the
bounded replacement of a lost dispatch, task-generation supersession
discarding late output in memory, and an uncertain release during
cancellation. Ephemeral-database tier: the store's own fences — a delayed
write cannot follow a generation swap with a duplicate completion, release
or budget increment — plus the study-level kill equivalence and the
checkpoint contract's non-renewal across a replacement. Slow tier: a real
SIGKILLed child mid-stage proves cleanup, replacement and durable reuse.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.config import settings
from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.golden_search import repository as repo
from app.research.golden_search import service
from app.research.golden_search.evaluator import (
    RETRY_ALLOWANCE,
    DeliveredOutcome,
    WorkerLost,
)
from app.research.golden_search.selection import Metrics
from app.research.persistence import lifecycle, ownership
from app.research.persistence.fence import StaleAttemptError
from app.utils.background_loop import CallerStoppedWaitingError
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_search_study import (
    Driver,
    seed_lake,
    unique_symbol,
)
from tests.research.golden_search.test_evaluator import (
    _EngineSpy,
    _key_of,
    _point,
    _ReservationBridge,
    _seamed_evaluator,
)

JAN = (et_midnight_ms(date(2025, 1, 6)), et_midnight_ms(date(2025, 1, 8)))


class _LossyDispatch:
    """A BatchDispatch that loses its worker on the round's first item, a bounded number of times (#3045).

    ``after_begin`` places the loss after the item's start marker — a task
    that was in flight, whose engine call may have begun — and
    ``after_run`` computes the result first, so it is lost with the worker
    after the engine truly ran. The defaults lose the worker before any work
    began, like one that never came up.
    """

    def __init__(self, *, losses: int = 1, after_begin: bool = False, after_run: bool = False) -> None:
        self.losses = losses
        self.after_begin = after_begin
        self.after_run = after_run
        self.rounds = 0

    def __call__(self, run, batch):
        self.rounds += 1
        for item in batch:
            if self.losses > 0:
                self.losses -= 1
                if self.after_begin:
                    item.begin()
                if self.after_run:
                    run(item.request)
                raise WorkerLost("the spawned worker died with a task in flight")
            item.begin()
            yield DeliveredOutcome(key=item.key, attempt=item.attempt, task_gen=item.task_gen, result=run(item.request))


# ── Fast tier: the fault boundaries over the bridge ───────────────────────


def test_a_fault_before_reservation_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)

    def cancelled() -> None:
        raise JobCancelled("cancelled before the first reservation")

    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    evaluator.cancel_check = cancelled

    with pytest.raises(JobCancelled):
        evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")

    assert bridge.reserved == [] and engine.ran == [] and bridge.consumed == 0


def test_a_fault_between_reservation_and_dispatch_reruns_the_whole_batch_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reserved-before-dispatch boundary: an adapter that dies before any item begins (#3045)."""

    def dead_dispatch(run, batch):
        raise RuntimeError("the dispatch runtime exploded before the first task")
        yield  # pragma: no cover - a generator so the raise happens at first next()

    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=dead_dispatch)

    with pytest.raises(RuntimeError, match="exploded"):
        evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert bridge.consumed == 3 and engine.ran == []
    assert all(row["started_at"] is None for row in bridge.rows.values()), "no engine call ever began"

    metrics = _seamed_evaluator(monkeypatch, engine, bridge).evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")
    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3]
    assert bridge.consumed == 3, "the re-run drew no second unit"
    assert all(row["retries"] == 0 for row in bridge.rows.values()), "a never-started row spends no retry (#3045)"


def test_a_worker_lost_during_compute_is_replaced_and_only_the_started_item_spends_a_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    lossy = _LossyDispatch(losses=1, after_begin=True, after_run=True)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=lossy)

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3], "the batch completes through the replacement"
    assert lossy.rounds == 2, "the dispatch was invoked again after the loss"
    assert engine.ran == [0.1, 0.1, 0.2, 0.3], "only the lost item re-ran"
    assert bridge.consumed == 3, "no second new-evaluation unit for the same reservation"
    assert [bridge.rows[_key_of(evaluator, _point(gap))]["retries"] for gap in (0.1, 0.2, 0.3)] == [1, 0, 0], (
        "the run that began and died spent one retry; the never-started members spent none"
    )


def test_repeated_worker_loss_resolves_the_item_as_retry_exhausted_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    lossy = _LossyDispatch(losses=RETRY_ALLOWANCE + 1, after_begin=True)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=lossy)

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    lost_key = _key_of(evaluator, _point(0.1))
    assert [m.status for m in metrics] == ["failed", "completed", "completed"]
    assert metrics[0].error == "retry allowance exhausted"
    assert bridge.consumed == 3, "the exhausted item still drew exactly one unit, once"
    assert bridge.rows[lost_key]["status"] == "failed" and bridge.rows[lost_key]["retries"] == RETRY_ALLOWANCE
    assert engine.ran == [0.2, 0.3], "the batch continued without the spent item"


def test_consecutive_replacements_without_progress_stop_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime that never comes up is not retried forever (#3045)."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    lossy = _LossyDispatch(losses=99, after_begin=False)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=lossy)

    with pytest.raises(WorkerLost):
        evaluator.evaluate([_point(0.1)], window=JAN, stage="search")

    assert lossy.rounds == RETRY_ALLOWANCE + 1, "the last replacement re-raised instead of looping"
    assert engine.ran == [] and bridge.consumed == 1, "the honest pending row keeps its single spent unit"
    assert bridge.rows[_key_of(evaluator, _point(0.1))]["status"] == "pending"


def test_an_uncertain_completion_that_later_commits_is_never_re_run_or_double_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The commit-succeeded-ack-lost boundary: the caller stopped waiting, the write landed anyway (#3045)."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    stalled = _key_of(evaluator, _point(0.2))
    bridge.uncertain[(stalled, 1)] = "commit"

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3]
    assert engine.ran == [0.1, 0.2, 0.3], "the committed result was reused, never recomputed"
    assert bridge.completed.count(stalled) == 1, "no duplicate completion followed the delayed commit"
    assert bridge.consumed == 3 and bridge.rows[stalled]["retries"] == 0, "no release, no retry, no budget increment"
    assert bridge.released == []


def test_an_uncertain_completion_that_rolled_back_lands_under_a_new_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivered-before-persistence boundary with an unknown write outcome (#3045)."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    stalled = _key_of(evaluator, _point(0.2))
    bridge.uncertain[(stalled, 1)] = "rollback"

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3]
    assert engine.ran == [0.1, 0.2, 0.3], "the result was still in hand: the engine never re-ran"
    row = bridge.rows[stalled]
    assert row["status"] == "completed" and row["task_gen"] == 2, "the same result persisted under the swapped generation"
    assert row["retries"] == 0 and bridge.consumed == 3


def test_a_stale_delivery_from_a_replaced_dispatch_is_discarded_and_the_owed_outcome_lands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    middle = 0.2

    def stale_then_fresh(run, batch):
        for item in batch:
            item.begin()
            result = run(item.request)
            if float(item.request.point["gap"]) == middle:
                # The replaced dispatch's output arrives late, under the generation it was fenced out of.
                yield DeliveredOutcome(key=item.key, attempt=item.attempt, task_gen=item.task_gen - 1, result=result)
            yield DeliveredOutcome(key=item.key, attempt=item.attempt, task_gen=item.task_gen, result=result)

    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=stale_then_fresh)

    metrics = evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2, 0.3]
    stale_key = _key_of(evaluator, _point(middle))
    assert bridge.completed.count(stale_key) == 1, "the stale delivery was discarded, not persisted"
    assert evaluator.new_runs == 3


def test_cancellation_during_an_uncertain_release_leaves_an_honest_pending_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, bridge = _EngineSpy(cancel_on_gap=0.2), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    uncertain_key = _key_of(evaluator, _point(0.2))
    bridge.uncertain_release = {uncertain_key}

    with pytest.raises(JobCancelled, match="cancelled mid-batch"):
        evaluator.evaluate([_point(0.1), _point(0.2), _point(0.3)], window=JAN, stage="search")

    assert bridge.rows[_key_of(evaluator, _point(0.1))]["status"] == "completed"
    # The uncertain release did not block cancellation: the row stays honestly
    # pending for Finish, its fate fenced by the generation (#3045).
    assert bridge.rows[uncertain_key]["status"] == "pending" and "cancelled" not in bridge.rows[uncertain_key]
    assert bridge.rows[_key_of(evaluator, _point(0.3))].get("cancelled") is True


# ── Ephemeral-database tier: the store's own fences ───────────────────────


@pytest.fixture
def db_symbol() -> str:
    return unique_symbol()


@pytest.fixture
def db_lake(tmp_path: Path, db_symbol: str) -> Path:
    return seed_lake(tmp_path, db_symbol)


@pytest.fixture
def db_driver(db_lake: Path, monkeypatch: pytest.MonkeyPatch, conn: asyncpg.Connection) -> Driver:
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [db_lake])
    return Driver(roots=[db_lake])


async def _bare_study(conn: asyncpg.Connection, study_id: str) -> None:
    """A study row the fence accepts: running, on attempt 1, nothing else."""
    await conn.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, status, attempt, created_at_ms, updated_at_ms,
            protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ($1, 'ema_crossover_signal', 'SPY', 'search_running', 'running', 1, 1, 1, '{}'::jsonb, 'h', '{}'::jsonb, 100)
        """,
        study_id,
    )


def _new_eval(key: str) -> repo.NewEvaluation:
    return repo.NewEvaluation(
        evaluation_key=key,
        point_hash="p",
        point={"symbol": "SPY"},
        window_start_ms=1,
        window_end_ms=2,
        scenario="base",
        detail=False,
        stage="search",
        fold_index=None,
    )


def _metrics() -> Metrics:
    return Metrics(status="completed", total_trades=1, net_profit=1.0, total_return_pct=0.01, sharpe_ratio=1.0, max_drawdown_pct=0.01, win_rate=1.0)


async def _row(conn: asyncpg.Connection, study_id: str, key: str) -> asyncpg.Record:
    return await conn.fetchrow(
        """
        SELECT status, task_gen, started_at_ms IS NOT NULL AS started, retries
          FROM research_golden_search_evaluations WHERE study_id = $1 AND evaluation_key = $2
        """,
        study_id,
        key,
    )


async def test_the_generation_fence_rejects_late_writes_independently_of_the_attempt_fence(
    conn: asyncpg.Connection,
) -> None:
    from app.research.persistence import fence

    study = "fence-" + unique_symbol().lower()
    await _bare_study(conn, study)
    first = await repo.reserve_evaluation(conn, study, 1, _new_eval("late-run"), limit=10, retry_allowance=RETRY_ALLOWANCE)
    assert (first.kind, first.task_gen) == ("run", 1)
    await repo.start_evaluation(conn, study, 1, "late-run", task_gen=1)

    # A retry re-admits under an advanced generation while the attempt stays current.
    second = await repo.reserve_evaluation(conn, study, 1, _new_eval("late-run"), limit=10, retry_allowance=RETRY_ALLOWANCE)
    assert (second.kind, second.task_gen) == ("run", 2)
    assert dict(await _row(conn, study, "late-run")) == {"status": "pending", "task_gen": 2, "started": False, "retries": 1}

    # The superseded dispatch cannot start, complete or release: the task fence refuses it alone.
    with pytest.raises(StaleAttemptError):
        await repo.start_evaluation(conn, study, 1, "late-run", task_gen=1)
    with pytest.raises(StaleAttemptError):
        await repo.complete_evaluation(conn, study, 1, "late-run", metrics=_metrics(), detail=None, task_gen=1)
    with pytest.raises(StaleAttemptError):
        await repo.release_evaluation(conn, study, 1, "late-run", task_gen=1)
    assert dict(await _row(conn, study, "late-run")) == {"status": "pending", "task_gen": 2, "started": False, "retries": 1}
    assert await conn.fetchval("SELECT consumed_evaluations FROM research_golden_search_studies WHERE id = $1", study) == 1

    await repo.complete_evaluation(conn, study, 1, "late-run", metrics=_metrics(), detail=None, task_gen=2)

    # The attempt fence refuses independently: after a new attempt claims, the old
    # attempt's writes fail even at the row's current generation.
    pending = await repo.reserve_evaluation(conn, study, 1, _new_eval("attempt-run"), limit=10, retry_allowance=RETRY_ALLOWANCE)
    await repo.start_evaluation(conn, study, 1, "attempt-run", task_gen=pending.task_gen)
    await fence.claim_attempt(conn, table=repo.STUDIES, record_id=study, job_id="job-next")
    with pytest.raises(StaleAttemptError):
        await repo.complete_evaluation(conn, study, 1, "attempt-run", metrics=_metrics(), detail=None, task_gen=pending.task_gen)
    with pytest.raises(StaleAttemptError):
        await repo.release_evaluation(conn, study, 1, "attempt-run", task_gen=pending.task_gen)


async def test_a_delayed_write_cannot_follow_a_generation_swap_with_a_duplicate(conn: asyncpg.Connection) -> None:
    study = "delay-" + unique_symbol().lower()
    await _bare_study(conn, study)
    reserved = await repo.reserve_evaluation(conn, study, 1, _new_eval("delayed"), limit=10, retry_allowance=RETRY_ALLOWANCE)
    await repo.start_evaluation(conn, study, 1, "delayed", task_gen=reserved.task_gen)

    # The caller stopped waiting; the swap decides against the write still in flight.
    claim = await repo.replace_generation(conn, study, 1, "delayed", task_gen=reserved.task_gen)
    assert claim.resolved is None and claim.task_gen == reserved.task_gen + 1

    # The delayed write finally executes: fenced out, and nothing follows it —
    # no duplicate completion, no release, no budget increment.
    with pytest.raises(StaleAttemptError):
        await repo.complete_evaluation(conn, study, 1, "delayed", metrics=_metrics(), detail=None, task_gen=reserved.task_gen)
    assert dict(await _row(conn, study, "delayed")) == {"status": "pending", "task_gen": 2, "started": False, "retries": 0}
    assert await conn.fetchval("SELECT consumed_evaluations FROM research_golden_search_studies WHERE id = $1", study) == 1
    assert await conn.fetchval("SELECT COUNT(*) FROM research_golden_search_trials WHERE study_id = $1", study) == 0

    # The other interleaving: the delayed write committed before the swap ran.
    await repo.complete_evaluation(conn, study, 1, "delayed", metrics=_metrics(), detail=None, task_gen=claim.task_gen)
    resolved = await repo.replace_generation(conn, study, 1, "delayed", task_gen=claim.task_gen)
    assert resolved.resolved is not None and resolved.resolved.status == "completed"
    again = await repo.reserve_evaluation(conn, study, 1, _new_eval("delayed"), limit=10, retry_allowance=RETRY_ALLOWANCE)
    assert again.kind == "cached"
    assert await conn.fetchval("SELECT consumed_evaluations FROM research_golden_search_studies WHERE id = $1", study) == 1


# ── Study-level: replacement, leases, coordinator restart ─────────────────


async def test_killing_a_worker_mid_stage_matches_the_serial_schedule(conn, db_driver: Driver, db_symbol: str) -> None:
    """The finite-accounting kill equivalence: a lost-and-replaced worker changes neither the admitted set nor the books."""
    reference = await db_driver.advance(await db_driver.lock(db_symbol), "continue")

    row = await db_driver.lock(db_symbol)
    outcome = await db_driver.command(row, "continue")
    lossy = _LossyDispatch(losses=1, after_begin=True)
    await db_driver.run(outcome, dispatch_factory=lambda context: lossy)

    done = await service.get_row(row.id)
    assert lossy.rounds >= 2, "the lost dispatch was replaced, not abandoned"
    assert done.state == reference.state == "awaiting_validation"
    assert done.consumed_evaluations == reference.consumed_evaluations
    assert done.results["search"]["procedure"] == reference.results["search"]["procedure"], (
        "the search lands where the serial schedule lands"
    )
    assert done.results["search"]["counts"] == reference.results["search"]["counts"]
    admitted = await conn.fetch(
        "SELECT evaluation_key FROM research_golden_search_evaluations WHERE study_id = $1 ORDER BY evaluation_key", row.id
    )
    reference_admitted = await conn.fetch(
        "SELECT evaluation_key FROM research_golden_search_evaluations WHERE study_id = $1 ORDER BY evaluation_key", reference.id
    )
    assert [item["evaluation_key"] for item in admitted] == [item["evaluation_key"] for item in reference_admitted]
    retries = await conn.fetchval("SELECT MAX(retries) FROM research_golden_search_evaluations WHERE study_id = $1", row.id)
    assert retries == 1, "exactly the lost in-flight run spent a retry, once"


async def test_worker_replacement_renews_no_lease_and_only_durable_completions_do(
    conn, db_driver: Driver, db_symbol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3041's contract across a #3045 replacement: spawning a fresh worker is not progress; only accepted checkpoints renew."""
    from app.jobs import progress as jobs_progress
    from app.jobs.checkpoints import CheckpointGate, ExecutionCheckpoint
    from tests.jobs.conftest import _FakeRedis

    fake = _FakeRedis()
    monkeypatch.setattr(jobs_progress, "get_redis", lambda: fake)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)

    reference = await db_driver.advance(await db_driver.lock(db_symbol), "continue")

    row = await db_driver.lock(db_symbol)
    outcome = await db_driver.command(row, "continue")
    job = "job-replacement-proof"
    await service.bind_dispatch(row.id, stage_token=outcome.dispatch["payload"]["stage_token"], job_id=job)
    fake.job(job, "running")
    fake.lease(job)

    generation, seen = 1, []
    gate = CheckpointGate(job, generation, on_accepted=seen.append)

    def checkpoint(**kwargs: Any) -> None:
        gate.accept(ExecutionCheckpoint(generation=generation, task_id="stage", sequence=len(seen) + 1, occurred_at_ms=time.time_ns() // 1_000_000, transition=True, **kwargs))

    lossy = _LossyDispatch(losses=1, after_begin=True)
    state = await asyncio.to_thread(
        service.run_stage,
        row.id,
        stage_token=outcome.dispatch["payload"]["stage_token"],
        job_id=job,
        cancel_check=jobs_progress.CancellationCheck(job, renews_lease=False).raise_if_cancelled,
        on_phase=lambda phase: checkpoint(cursor=0, cursor_kind="processed_blocks", phase=phase),
        on_progress=lambda done, total: checkpoint(cursor=done, cursor_kind="durable_completions", phase="search"),
        on_log=lambda message: None,
        execute=db_driver.engine,
        approval=db_driver.approval.binding(),
        roots=db_driver.roots,
        dispatch_factory=lambda context: lossy,
    )

    assert lossy.rounds >= 2 and state.state == reference.state
    assert seen, "the stage's real boundaries reached the gate"
    assert all(key == f"job:{job}:lease" for key, _ in fake.expires)
    assert len(fake.expires) == len(seen), "the replacement and its re-admissions renewed nothing; each accepted checkpoint renewed once"
    finished = await service.get_row(row.id)
    assert finished.results["search"]["counts"] == reference.results["search"]["counts"]
    assert finished.consumed_evaluations == reference.consumed_evaluations


async def test_a_coordinator_restart_closes_only_job_records_and_never_resumes(
    conn, db_driver: Driver, db_symbol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3045: startup recovery closes the dead generation's records; the study waits for explicit Finish."""
    from app.jobs import progress as jobs_progress
    from app.jobs.progress import close_orphaned_job_records
    from tests.jobs.conftest import _FakeRedis

    fake = _FakeRedis()
    monkeypatch.setattr(jobs_progress, "get_redis", lambda: fake)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)

    row = await db_driver.lock(db_symbol)
    outcome = await db_driver.command(row, "continue")
    job = "job-abandoned-" + unique_symbol().lower()
    await service.bind_dispatch(row.id, stage_token=outcome.dispatch["payload"]["stage_token"], job_id=job)
    fake.job(job, "running")
    fake.lease(job)
    await conn.execute(
        """
        INSERT INTO research_job_owners (job_id, kind, record_id, attempt, owner_instance_id, boot_generation, dispatched_at_ms)
        VALUES ($1, 'golden_search', $2, NULL, 'research:dead', 1, 1)
        """,
        job,
        row.id,
    )

    def never_resumes(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a coordinator restart must never run a stage by itself")

    monkeypatch.setattr(service, "run_stage", never_resumes)

    # The restart's whole recovery sweep: enumerate the dead generation, close its records.
    abandoned = await ownership.abandoned_job_ids(conn, up_to_generation=2)
    assert job in abandoned
    await asyncio.to_thread(close_orphaned_job_records, abandoned)

    after = await service.get_row(row.id)
    assert (after.status, after.pending_stage, after.attempt, after.consumed_evaluations) == ("queued", "search", 0, 0)
    assert after.stage_token == outcome.study.stage_token, "the authorized token survives; only explicit commands spend it"
    assert fake.hget(f"job:{job}:state", "status") == "failed", "the record closed as orphaned"
    assert fake.exists(f"job:{job}:lease") is False, "the closed job's lease died with it"


# ── Slow tier: a real SIGKILLed child mid-stage ───────────────────────────


@pytest.mark.slow
@pytest.mark.usefixtures("seeded_lake_catalog")
async def test_a_real_sigkill_mid_stage_is_replaced_and_its_durable_evaluations_are_reused(
    conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real SIGKILL — not a mocked exception — proves the whole recovery path (#3045).

    The child is killed with a task in flight mid-search. The coordinator
    must fence it, kill-and-reap it, spawn a replacement (whose decoded cache
    starts cold — a performance loss, never a scientific one) and finish the
    stage: the evaluations recorded before the kill are reused as they stand,
    the interrupted one re-runs under a new task generation spending one
    retry, and the books match the rows.
    """
    from uuid import UUID

    from app.data_lake.root_identity import init_empty_root
    from app.research.golden_search.worker import ResearchWorker, worker_dispatch_factory
    from tests._helpers.lake_fixture import seed_lake_minute_day

    symbol = unique_symbol()
    base = tmp_path / "sigkill-root"
    init_empty_root(base, UUID(int=3045))
    lake = base / "lake" / "polygon_split_adjusted"
    lake.mkdir(parents=True)
    for day in expected_sessions(date(2024, 12, 2), date(2025, 4, 30)):
        seed_lake_minute_day(lake, symbol, day)
    monkeypatch.setattr(lifecycle, "roots_for", lambda row: [lake])
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(base))
    monkeypatch.setenv("LEAN_DATA_WRITE_ROOT", str(base))  # the child reads env, not the object
    driver = Driver(roots=[lake])

    row = await driver.lock(symbol)
    outcome = await driver.command(row, "continue")
    assert outcome.dispatch is not None
    job = "job-sigkill-" + unique_symbol().lower()
    await service.bind_dispatch(row.id, stage_token=outcome.dispatch["payload"]["stage_token"], job_id=job)

    checkpoints: list[dict[str, Any]] = []
    workers: list[ResearchWorker] = []
    real_init = ResearchWorker.__init__

    def tracking_init(self: ResearchWorker, context: Any) -> None:
        real_init(self, context)
        workers.append(self)

    monkeypatch.setattr(ResearchWorker, "__init__", tracking_init)
    factory = lambda context: worker_dispatch_factory(  # noqa: E731 - the factory the stage run receives
        on_checkpoint=checkpoints.append, cancel_check=lambda: None
    )(context)

    def live_child() -> subprocess.Popen | None:
        for worker in workers:
            child = worker._child
            if child is not None and child.poll() is None:
                return child
        return None

    stage = asyncio.create_task(driver.run(outcome, job_id=job, dispatch_factory=factory))
    killed_pid: int | None = None
    completed_before: list[asyncpg.Record] = []
    deadline = time.monotonic() + 600.0
    while time.monotonic() < deadline:
        if killed_pid is None and sum(1 for message in checkpoints if message.get("phase") == "preparation") >= 2:
            child = live_child()
            if child is not None:
                # The second task is in flight: its engine call has begun.
                completed_before = await conn.fetch(
                    "SELECT evaluation_key, completed_at_ms, retries FROM research_golden_search_evaluations WHERE study_id = $1 AND status = 'completed'",
                    row.id,
                )
                os.kill(child.pid, signal.SIGKILL)
                killed_pid = child.pid
        if stage.done():
            break
        await asyncio.sleep(0.02)

    assert killed_pid is not None, "the test never caught a task in flight to kill"
    state = await stage
    assert state.state == "awaiting_validation", "the stage finished through the replacement"

    assert len(workers) >= 2, "the lost child was replaced, not trusted"
    assert all(worker._child is None for worker in workers), "every child was killed and reaped"
    with pytest.raises(OSError):
        os.kill(killed_pid, 0)

    assert await conn.fetchval("SELECT COUNT(*) FROM research_golden_search_evaluations WHERE study_id = $1 AND status = 'pending'", row.id) == 0
    assert await conn.fetchval("SELECT MAX(retries) FROM research_golden_search_evaluations WHERE study_id = $1", row.id) <= 1
    assert await conn.fetchval("SELECT COUNT(*) FROM research_golden_search_evaluations WHERE study_id = $1", row.id) == await conn.fetchval(
        "SELECT consumed_evaluations FROM research_golden_search_studies WHERE id = $1", row.id
    ), "one budget unit per row, the interrupted run included"

    # Durable reuse: the rows the killed child recorded stand exactly as they were.
    for record in completed_before:
        after = await conn.fetchrow(
            "SELECT completed_at_ms, retries, status FROM research_golden_search_evaluations WHERE study_id = $1 AND evaluation_key = $2",
            row.id,
            record["evaluation_key"],
        )
        assert (after["completed_at_ms"], after["retries"], after["status"]) == (record["completed_at_ms"], record["retries"], "completed")


def test_an_uncertain_reservation_that_committed_charges_one_unit_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The before-reservation boundary's write fate: the insert landed after the caller stopped waiting (#3045)."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    stalled = _key_of(evaluator, _point(0.1))
    bridge.uncertain_reserve[stalled] = "commit"

    metrics = evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2]
    assert bridge.consumed == 2, "the committed reservation's unit is the only one charged for the stalled row"
    assert engine.ran == [0.1, 0.2], "each evaluation ran once"
    assert bridge.rows[stalled]["retries"] == 0, "the re-reserve read its own row back: a free re-admission, never a retry"


def test_an_uncertain_reservation_that_rolled_back_inserts_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    bridge.uncertain_reserve[_key_of(evaluator, _point(0.1))] = "rollback"

    metrics = evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")

    assert [m.net_profit for m in metrics] == [0.1, 0.2]
    assert bridge.consumed == 2 and bridge.rows[_key_of(evaluator, _point(0.1))]["retries"] == 0


def test_a_second_reservation_stall_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """One reconciliation is allowed; a database that stalls twice is dead, not uncertain (#3045)."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)
    evaluator = _seamed_evaluator(monkeypatch, engine, bridge)
    bridge.uncertain_reserve[_key_of(evaluator, _point(0.1))] = "stall"

    with pytest.raises(CallerStoppedWaitingError):
        evaluator.evaluate([_point(0.1)], window=JAN, stage="search")
    assert engine.ran == []


def test_an_adapter_that_ends_without_every_outcome_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract's one-outcome-per-admitted-key rule is enforced, not assumed (#3045)."""
    engine, bridge = _EngineSpy(), _ReservationBridge(limit=10)

    def short_dispatch(run, batch):
        for item in batch[:1]:
            item.begin()
            yield DeliveredOutcome(key=item.key, attempt=item.attempt, task_gen=item.task_gen, result=run(item.request))

    evaluator = _seamed_evaluator(monkeypatch, engine, bridge, dispatch=short_dispatch)
    with pytest.raises(RuntimeError, match="without delivering an outcome"):
        evaluator.evaluate([_point(0.1), _point(0.2)], window=JAN, stage="search")
