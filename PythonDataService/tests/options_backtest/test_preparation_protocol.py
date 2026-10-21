"""Actual socket framing, quiet bounded work, reconnect and lease ownership regressions."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from app.config import settings
from app.jobs.progress import JobCancelled, ProgressEmitter
from app.options_backtest import preparation_stream
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.preparation import preparation_identity
from app.options_backtest.timings import RunTimings
from app.research.persistence.ownership import GenerationFencedError
from app.utils.timestamps import now_ms_utc
from tests.options_backtest._preparation_peer import owner as owner
from tests.options_backtest._preparation_peer import send, socket_peer
from tests.options_backtest._run_data import THU, WED, chain_scene


@pytest.mark.slow
async def test_quiet_socket_past_ten_seconds_waits_for_verified_completion_without_poll_renewals(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req, fake, _, lease, state = owner
    frozen = freeze_run_inputs(req.setup, (WED, THU), chain_scene(), tmp_path / req.job_id)
    state["wait"] = {"id": 1, "deadline_ms": now_ms_utc() + 20_000}
    idle_renewals = []

    async def peer(writer):
        await send(writer, state)
        await asyncio.sleep(0.5)
        before = len(fake.expires)
        await asyncio.sleep(10.1)
        idle_renewals.append(len(fake.expires) - before)
        await send(writer, {**state, "sequence": 2, "status": "complete", "wait": None,
                            "inputs": frozen.model_dump(mode="json")})

    async with socket_peer(peer, monkeypatch):
        actual = await preparation_stream.prepare_over_api(req, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                           lease=lease, timings=RunTimings())
    assert actual == frozen
    assert idle_renewals == [0]


@pytest.mark.parametrize("cancel", [False, True])
async def test_silent_socket_stalls_or_cancels_with_no_timer_checkpoint(owner, monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    req, fake, _, lease, state = owner
    monkeypatch.setattr(preparation_stream, "STALL_SECONDS", 0.3)
    accepted = []

    async def peer(writer):
        await send(writer, state)
        await asyncio.sleep(0.1)
        accepted.append(len(fake.expires))
        if cancel:
            fake.hset(f"job:{req.job_id}:state", mapping={"cancel_requested": "1"})
        await asyncio.sleep(30)

    started = time.monotonic()
    async with socket_peer(peer, monkeypatch):
        with pytest.raises(JobCancelled if cancel else TimeoutError):
            await preparation_stream.prepare_over_api(req, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                     lease=lease, timings=RunTimings())
        assert time.monotonic() - started < 1
    assert time.monotonic() - started < 1
    assert len(fake.expires) == accepted[0]


async def test_lost_socket_rejoins_same_preparation_and_repeated_receipt_renews_nothing(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req, fake, _, lease, state = owner
    frozen = freeze_run_inputs(req.setup, (WED, THU), chain_scene(), tmp_path / req.job_id)
    calls = 0
    first = []

    async def peer(writer):
        nonlocal calls
        calls += 1
        await send(writer, state)
        if calls == 1:
            await asyncio.sleep(0.1)
            first.append(len(fake.expires))
            return  # Truncated chunk stream, as an API process restart does.
        await asyncio.sleep(0.1)
        assert len(fake.expires) == first[0]
        await send(writer, {**state, "sequence": 2, "status": "complete", "inputs": frozen.model_dump(mode="json")})

    async with socket_peer(peer, monkeypatch):
        actual = await preparation_stream.prepare_over_api(req, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                           lease=lease, timings=RunTimings())
    assert actual == frozen
    assert calls == 2


def test_bounded_io_deadline_is_fixed_and_neither_expired_nor_stale_owner_can_extend(owner, monkeypatch: pytest.MonkeyPatch) -> None:
    req, fake, authority, lease, _ = owner
    deadline = now_ms_utc() + 1_800_000
    lease.bounded_external_wait(1, deadline)
    first = len(fake.expires)
    lease.bounded_external_wait(1, deadline + 1000)
    assert len(fake.expires) == first
    lease.child_checkpoint({"cursor": 1})
    assert fake.expires[-1][1] >= 1799
    fake.delete(f"job:{req.job_id}:lease")
    with pytest.raises(JobCancelled):
        lease.bounded_external_wait(2, deadline)
    assert not fake.exists(f"job:{req.job_id}:lease")
    fake.lease(req.job_id)
    authority.connected = False
    first = len(fake.expires)
    with pytest.raises(GenerationFencedError):
        lease.bounded_external_wait(3, deadline)
    assert len(fake.expires) == first


async def test_runtime_without_api_env_file_uses_shared_nonlegacy_root_identity(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import UUID

    from app.data_lake.root_identity import init_empty_root

    req, _, _, lease, state = owner
    root_id = UUID("04fc9ead-1b02-4a3b-a0c1-18bb4cc33ea7")
    source = tmp_path / "nonlegacy-source"
    init_empty_root(source, root_id)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(source))
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", str(root_id))
    state["identity"] = preparation_identity(req)
    frozen = freeze_run_inputs(req.setup, (WED, THU), chain_scene(), tmp_path / req.job_id)
    # API reads the UUID from its env_file; the runtime receives no provider
    # env file and resolves the same identity from their shared lake mount.
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")

    async def peer(writer):
        await send(writer, {**state, "status": "complete", "inputs": frozen.model_dump(mode="json")})

    async with socket_peer(peer, monkeypatch):
        actual = await preparation_stream.prepare_over_api(req, emit=ProgressEmitter(req.job_id, renews_lease=False),
                                                           lease=lease, timings=RunTimings())
    assert actual.source_root_id == str(root_id)


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
