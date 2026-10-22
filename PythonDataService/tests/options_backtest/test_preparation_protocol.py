"""Canonical job observation, crash recovery and real-work-only lease renewal."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.jobs import facade, progress
from app.jobs.external_work import ExternalJobObserver, WorkReceipts
from app.jobs.progress import JobCancelled, ProgressEmitter
from app.options_backtest import preparation_jobs
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.preparation import PREPARATION_JOB_TYPE, PreparationCheckpoint, preparation_identity
from app.options_backtest.timings import RunTimings
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


def _complete(req, tmp_path: Path) -> PreparationCheckpoint:
    frozen = freeze_run_inputs(req.setup, (WED, THU), archived_scene(chain_scene(), tmp_path / "source"), tmp_path / req.job_id)
    checkpoint = PreparationCheckpoint(identity=preparation_identity(req), inputs=frozen, complete=True)
    (tmp_path / req.job_id / "freeze-checkpoint.json").write_text(checkpoint.model_dump_json())
    return checkpoint


async def test_quiet_bounded_job_wait_has_no_poll_renewals_and_completed_checkpoint_survives_redis_ttl(
    owner, tmp_path: Path,
) -> None:
    req, fake, _, lease, binding = owner
    child = _child(fake, binding.job_id)
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
    _child(fake, binding.job_id)
    monkeypatch.setattr(external_work, "STALL_SECONDS", 0.3)
    renewals = []

    async def steer():
        await asyncio.sleep(0.1)
        renewals.append(len(fake.expires))
        if cancel:
            fake.hset(progress.state_key(req.job_id), mapping={"cancel_requested": "1"})

    steering = asyncio.create_task(steer())
    started = time.monotonic()
    with pytest.raises(JobCancelled if cancel else TimeoutError):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=RunTimings())
    await steering
    assert time.monotonic() - started < 1
    assert len(fake.expires) == renewals[0]
    assert fake.hget(progress.state_key(binding.job_id), "cancel_requested") == "1"


async def test_lost_dispatch_reply_observes_same_child_and_only_startup_interruption_binds_fresh_id(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req, fake, _, lease, binding = owner
    original = binding.job_id
    facade._record_queued(original, PREPARATION_JOB_TYPE, {}, internal=True)
    calls = []

    async def dispatch(request: httpx.Request):
        payload = json.loads(request.content)
        child_id = payload["job_id"]
        calls.append(child_id)
        child = _child(fake, child_id)
        if child_id == original:
            child.failed(code="DATA_SERVICE_RESTARTED", message="the API worker stopped")
            raise httpx.ReadError("lost start acknowledgement")
        checkpoint = _complete(req, tmp_path)
        child.completed(checkpoint.model_dump(mode="json"))
        return httpx.Response(202, json={"id": child_id, "status": "queued"})

    monkeypatch.setattr(preparation_jobs, "build_internal_client",
                        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(dispatch)))
    actual = await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                    lease=lease, timings=RunTimings())
    assert len(calls) == 2 and calls[0] == original and calls[1] != original
    assert binding.job_id == calls[1]
    assert len(actual.sessions) == 2


async def test_missing_child_state_does_not_admit_reexecution(owner) -> None:
    req, fake, _, lease, binding = owner
    with pytest.raises(RuntimeError, match="absent or expired"):
        await preparation_jobs.prepare_over_api(req, run_id=1, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                               lease=lease, timings=RunTimings())
    assert binding.job_id == "api-preparation"
    assert fake.strings == {progress.lease_key(req.job_id): "owner"}


def test_receipts_are_unique_deadlines_fixed_and_late_callbacks_inert(owner) -> None:
    req, fake, authority, lease, binding = owner
    child = _child(fake, binding.job_id)
    receipts = WorkReceipts(child, lambda: None)
    observer = ExternalJobObserver(ProgressEmitter(req.job_id, renews_lease=False), lease)
    deadline = now_ms_utc() + 1_800_000
    event = {"type": "job.external_wait", "deadline_ms": deadline}
    observer.accept(binding.job_id, "1-0", event)
    first = len(fake.expires)
    observer.accept(binding.job_id, "1-0", {**event, "deadline_ms": deadline + 1000})
    assert len(fake.expires) == first
    receipts.work({"kind": "work", "work_kind": "provider_response", "identity": "actual-response"})
    receipts.work({"kind": "work", "work_kind": "provider_response", "identity": "actual-response"})
    receipts.deactivate()
    emitted = [json.loads(fields["event"]) for _, fields in fake.streams[progress.events_key(binding.job_id)]]
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
    from app.options_backtest.preparation import runtime_preparation_identity

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
