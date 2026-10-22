"""A fresh internal job closes only when its failed binding is known not to own it."""
from __future__ import annotations

from typing import Any

import pytest

from app.jobs import facade, progress
from app.options_backtest import preparation_binding, preparation_jobs
from app.options_backtest.preparation import PREPARATION_JOB_TYPE, preparation_identity
from app.research.persistence.ownership import GenerationFencedError
from app.utils.background_loop import CallerStoppedWaitingError
from tests.options_backtest._preparation_jobs_fixture import owner as owner


@pytest.mark.parametrize("error_type", [ValueError, GenerationFencedError])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_definitive_binding_failure_closes_only_the_new_job_and_preserves_original_error(
    owner: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    error_type: type[Exception], cleanup_fails: bool,
) -> None:
    req, fake, _, lease, binding = owner
    old_id = binding.job_id
    facade._record_queued(old_id, PREPARATION_JOB_TYPE, {}, internal=True)
    fake.set(progress.lease_key(old_id), "old-owner")
    old_state = fake.hgetall(progress.state_key(old_id))
    failure = error_type("preparation binding was refused")
    created = []

    async def refuse(conn: Any, run_id: int, parent: str, generation: int, identity: str,
                     job_id: str, **kwargs: Any) -> str:
        created.append(job_id)
        raise failure

    monkeypatch.setattr(preparation_binding, "bind_preparation", refuse)
    if cleanup_fails:
        def unavailable(*args: Any, **kwargs: Any) -> None:
            raise OSError("job store unavailable during cleanup")

        monkeypatch.setattr(progress.ProgressEmitter, "cancelled", unavailable)
    with pytest.raises(error_type) as caught:
        await preparation_jobs._bind_fresh(7, req, lease, preparation_identity(req), old_id)
    assert caught.value is failure
    [new_id] = created
    state = fake.hgetall(progress.state_key(new_id))
    assert state["status"] == ("queued" if cleanup_fails else "cancelled")
    assert (new_id in fake.smembers(progress.active_set_key())) is cleanup_fails
    assert not fake.exists(progress.lease_key(new_id))
    assert fake.hgetall(progress.state_key(old_id)) == old_state
    assert fake.get(progress.lease_key(old_id)) == "old-owner"
    if cleanup_fails:
        assert "unbound preparation job could not be closed" in caplog.text


@pytest.mark.parametrize("error_type", [CallerStoppedWaitingError, OSError])
@pytest.mark.parametrize("reconciled", ["bound", "different", "unavailable"])
async def test_lost_binding_ack_closes_only_reconciled_unbound_job(
    owner: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception], reconciled: str,
) -> None:
    req, fake, _, lease, binding = owner
    old_id = binding.job_id
    facade._record_queued(old_id, PREPARATION_JOB_TYPE, {}, internal=True)
    old_state = fake.hgetall(progress.state_key(old_id))
    failure = error_type("binding acknowledgement was lost")
    created = []

    async def lost_ack(conn: Any, run_id: int, parent: str, generation: int, identity: str,
                       job_id: str, **kwargs: Any) -> str:
        created.append(job_id)
        if reconciled == "bound":
            binding.job_id = job_id
        raise failure

    async def read_binding(*args: Any, **kwargs: Any) -> str:
        if reconciled == "unavailable":
            raise OSError("binding cannot currently be read")
        return binding.job_id

    monkeypatch.setattr(preparation_binding, "bind_preparation", lost_ack)
    monkeypatch.setattr(preparation_binding, "current_binding", read_binding)
    if reconciled == "bound":
        returned = await preparation_jobs._bind_fresh(7, req, lease, preparation_identity(req), old_id)
        assert returned == created[0]
    else:
        with pytest.raises(error_type) as caught:
            await preparation_jobs._bind_fresh(7, req, lease, preparation_identity(req), old_id)
        assert caught.value is failure
    [new_id] = created
    state = fake.hgetall(progress.state_key(new_id))
    assert state["status"] == ("cancelled" if reconciled == "different" else "queued")
    assert state["cancel_requested"] == "0"
    assert (new_id in fake.smembers(progress.active_set_key())) is (reconciled != "different")
    assert not fake.exists(progress.lease_key(new_id))
    assert fake.hgetall(progress.state_key(old_id)) == old_state
