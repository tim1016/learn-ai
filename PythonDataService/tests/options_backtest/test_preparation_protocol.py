"""Canonical job observation, crash recovery and real-work-only lease renewal."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.jobs import facade, progress
from app.jobs.external_work import ExternalJobObserver, WorkReceipts
from app.jobs.progress import ORPHANED_JOB_CODE, JobCancelled, ProgressEmitter
from app.options_backtest import preparation_jobs
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.preparation import (
    COMPLETED_RESULT,
    PREPARATION_JOB_TYPE,
    PreparationCheckpoint,
    preparation_identity,
    runtime_preparation_identity,
)
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.research.persistence.ownership import GenerationFencedError
from app.utils.timestamps import now_ms_utc
from tests.options_backtest._archived_data import archived_scene
from tests.options_backtest._preparation_jobs_fixture import owner as owner
from tests.options_backtest._run_data import THU, WED, chain_scene


def _child(fake, job_id: str) -> ProgressEmitter:
    facade._record_queued(job_id, PREPARATION_JOB_TYPE, {}, internal=True)
    fake.set(progress.lease_key(job_id), "child")
    emit = ProgressEmitter(job_id, renews_lease=False)
    emit.started()
    emit.phase("downloading")
    return emit


def _live_child(fake, binding) -> ProgressEmitter:
    """A running child that the parent's first bind adopts, as if its worker had raced ahead of the bind."""
    binding.queue_as.append("api-preparation")
    return _child(fake, "api-preparation")


def _complete(req, tmp_path: Path, timings: tuple[PhaseTiming, ...] = ()) -> PreparationCheckpoint:
    frozen = freeze_run_inputs(req.setup, (WED, THU), archived_scene(chain_scene(), tmp_path / "source"), tmp_path / req.job_id)
    checkpoint = PreparationCheckpoint(identity=preparation_identity(req), inputs=frozen, timings=timings, complete=True)
    (tmp_path / req.job_id / "freeze-checkpoint.json").write_text(checkpoint.model_dump_json())
    return checkpoint


async def test_quiet_bounded_job_wait_has_no_poll_renewals_and_completed_checkpoint_survives_redis_ttl(
    owner, tmp_path: Path,
) -> None:
    req, fake, _, lease, binding = owner
    child = _live_child(fake, binding)
    child.emit_event("job.external_wait", {"deadline_ms": now_ms_utc() + 1_800_000})
    observed = []

    async def finish():
        await asyncio.sleep(0.25)
        before = len(fake.expires)
        await asyncio.sleep(0.5)
        observed.append(len(fake.expires) - before)
        checkpoint = _complete(req, tmp_path)
        # The durable inventory is authoritative even after ordinary job
        # metadata and its result expired; no old child id executes again.
        fake.delete(progress.state_key(binding.job_id))
        return checkpoint

    finishing = asyncio.create_task(finish())
    actual = await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                    lease=lease, timings=RunTimings())
    assert actual == (await finishing).inputs
    assert observed == [0]
    assert max(ttl for key, ttl in fake.expires if key == progress.lease_key(req.job_id)) >= 1799
    assert await facade.list_active_jobs() == []


@pytest.mark.parametrize("cancel", [False, True])
async def test_silent_job_stalls_or_cancels_without_timer_renewal(owner, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    from app.jobs import external_work

    req, fake, _, lease, binding = owner
    _live_child(fake, binding)
    monkeypatch.setattr(external_work, "STALL_SECONDS", 0.3)
    renewals = []

    async def steer():
        await asyncio.sleep(0.1)
        renewals.append(len(fake.expires))
        if cancel:
            fake.hset(progress.state_key(req.job_id), mapping={"cancel_requested": "1"})

    steering = asyncio.create_task(steer())
    with pytest.raises(JobCancelled if cancel else TimeoutError):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=RunTimings())
    await steering
    assert len(fake.expires) == renewals[0]
    assert fake.hget(progress.state_key(binding.job_id), "cancel_requested") == "1"


async def test_lost_dispatch_reply_observes_same_child_and_only_startup_interruption_binds_fresh_id(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req, fake, _, lease, binding = owner
    calls = []

    async def dispatch(request: httpx.Request):
        child_id = json.loads(request.content)["job_id"]
        calls.append(child_id)
        child = _child(fake, child_id)
        if len(calls) == 1:
            child.failed(code=ORPHANED_JOB_CODE, message="the API worker stopped")
            raise httpx.ReadError("lost start acknowledgement")
        _complete(req, tmp_path)
        child.completed(COMPLETED_RESULT)
        return httpx.Response(202, json={"id": child_id, "status": "queued"})

    monkeypatch.setattr(preparation_jobs, "build_internal_client",
                        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(dispatch)))
    actual = await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                    lease=lease, timings=RunTimings())
    assert len(calls) == 2 and calls[0] != calls[1]
    assert binding.job_id == calls[1]
    assert len(actual.sessions) == 2


async def test_missing_child_state_does_not_admit_reexecution(owner) -> None:
    req, fake, _, lease, binding = owner
    binding.queue_as.append("api-preparation")  # bound, but its job state is gone
    with pytest.raises(RuntimeError, match="absent or expired"):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=RunTimings())
    assert binding.job_id == "api-preparation"
    assert fake.strings == {progress.lease_key(req.job_id): "owner"}


def test_receipts_are_unique_deadlines_fixed_and_late_callbacks_inert(owner) -> None:
    req, fake, authority, lease, _ = owner
    child_id = "api-preparation"
    child = _child(fake, child_id)
    receipts = WorkReceipts(child, lambda: None)
    observer = ExternalJobObserver(ProgressEmitter(req.job_id, renews_lease=False), lease)
    deadline = now_ms_utc() + 1_800_000
    event = {"type": "job.external_wait", "deadline_ms": deadline}
    observer.accept(child_id, "1-0", event)
    first = len(fake.expires)
    observer.accept(child_id, "1-0", {**event, "deadline_ms": deadline + 1000})
    assert len(fake.expires) == first
    receipts.work({"kind": "work", "work_kind": "provider_response", "identity": "actual-response"})
    receipts.work({"kind": "work", "work_kind": "provider_response", "identity": "actual-response"})
    receipts.deactivate()
    emitted = [json.loads(fields["event"]) for _, fields in fake.streams[progress.events_key(child_id)]]
    assert sum(event["type"] == "job.progress" for event in emitted) == 1
    first = len(fake.expires)
    receipts.work({"kind": "external_wait", "bound_ms": 1_000})
    assert len(fake.expires) == first
    fake.delete(progress.lease_key(req.job_id))
    with pytest.raises(JobCancelled):
        lease.bounded_external_wait("next", deadline)
    assert not fake.exists(progress.lease_key(req.job_id))
    fake.set(progress.lease_key(req.job_id), "owner")
    authority.connected = False
    first = len(fake.expires)
    with pytest.raises(GenerationFencedError):
        lease.bounded_external_wait("stale", deadline)
    assert len(fake.expires) == first


def test_runtime_root_marker_refuses_explicit_mismatch_or_absence(owner, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.data_lake.root_identity import LakeRootIdentityError, marker_path

    req, *_ = owner
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "04fc9ead-1b02-4a3b-a0c1-18bb4cc33ea7")
    with pytest.raises(LakeRootIdentityError, match="mismatch"):
        runtime_preparation_identity(req)
    marker_path(Path(settings.LEAN_DATA_WRITE_ROOT)).unlink()
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
    with pytest.raises(LakeRootIdentityError, match="marker"):
        runtime_preparation_identity(req)


@pytest.mark.parametrize("changed", ["setup", "source", "underlying", "root", "identity", "sessions", "bytes", "blob"])
async def test_completed_checkpoint_refuses_changed_input_binding_without_dispatch(
    owner, tmp_path: Path, changed: str,
) -> None:
    req, fake, _, lease, _ = owner
    checkpoint = _complete(req, tmp_path)
    frozen = checkpoint.inputs
    updates = {
        "setup": {"setup_digest": "0" * 64},
        "source": {"source_root_id": "04fc9ead-1b02-4a3b-a0c1-18bb4cc33ea7"},
        "underlying": {"underlying": "QQQ"},
        "root": {"root": str(tmp_path / "other")},
        "sessions": {"sessions": dict(list(frozen.sessions.items())[:1])},
        "bytes": {"retained_bytes": frozen.retained_bytes + 1},
    }
    if changed == "identity":
        checkpoint = checkpoint.model_copy(update={"identity": "0" * 64})
    elif changed == "blob":
        digest = next(iter(frozen.blob_digests))
        (Path(frozen.root) / digest[:2] / digest).write_bytes(b"changed input bytes")
    else:
        checkpoint = checkpoint.model_copy(update={"inputs": frozen.model_copy(update=updates[changed])})
    (tmp_path / req.job_id / "freeze-checkpoint.json").write_text(checkpoint.model_dump_json())
    from app.research.golden_search.proof import ProofMismatchError

    with pytest.raises(ProofMismatchError if changed == "blob" else ValueError):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=RunTimings())
    assert fake.strings == {progress.lease_key(req.job_id): "owner"}


async def test_restart_then_cancel_keeps_each_attempts_closed_timings_once(
    owner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req, fake, _, lease, binding = owner
    first = _live_child(fake, binding)
    identity = preparation_identity(req)
    before = PhaseTiming(phase="check", started_at_ms=1, finished_at_ms=1, duration_ms=0)
    checked = before.model_copy(update={"started_at_ms": 2, "finished_at_ms": 2})
    downloaded = checked.model_copy(update={"phase": "download"})
    first.emit_event("job.observations", {"identity": identity, "phase_timings": [before.model_dump(mode="json")]})
    first.failed(code=ORPHANED_JOB_CODE, message="the API worker stopped")

    async def dispatch(request: httpx.Request) -> httpx.Response:
        child = _child(fake, json.loads(request.content)["job_id"])
        # Each event is the child's cumulative history; a redelivery adds nothing.
        for rows in ((checked,), (checked, downloaded), (checked, downloaded)):
            child.emit_event("job.observations", {"identity": identity,
                             "phase_timings": [row.model_dump(mode="json") for row in rows]})
        child.cancelled("input preparation was cancelled")
        return httpx.Response(202)

    monkeypatch.setattr(preparation_jobs, "build_internal_client",
                        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(dispatch)))
    timings = RunTimings()
    with pytest.raises(JobCancelled):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=timings)
    assert timings.rows == (before, checked, downloaded)


async def test_complete_checkpoint_carries_the_spans_its_writers_final_event_has_not_published(
    owner, tmp_path: Path,
) -> None:
    req, _, _, lease, _ = owner
    froze = PhaseTiming(phase="input_freeze", started_at_ms=3, finished_at_ms=4, duration_ms=1)
    _complete(req, tmp_path, timings=(froze,))
    timings = RunTimings()
    await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                           lease=lease, timings=timings)
    assert timings.rows == (froze,)


async def test_job_completed_without_a_complete_checkpoint_fails_instead_of_waiting(owner) -> None:
    req, fake, _, lease, binding = owner
    _live_child(fake, binding).completed(COMPLETED_RESULT)
    with pytest.raises(RuntimeError, match="completed without a complete freeze checkpoint"):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=RunTimings())


def test_runtime_without_api_env_file_uses_shared_nonlegacy_root_identity(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import UUID

    from app.data_lake.root_identity import init_empty_root

    req, *_ = owner
    root_id = UUID("04fc9ead-1b02-4a3b-a0c1-18bb4cc33ea7")
    source = tmp_path / "nonlegacy-source"
    init_empty_root(source, root_id)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(source))
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(root_id))
    api_identity = preparation_identity(req)
    # The API reads the UUID from its env_file; the runtime receives no provider
    # env file and resolves the same identity from their shared lake mount.
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
    assert runtime_preparation_identity(req) == api_identity
