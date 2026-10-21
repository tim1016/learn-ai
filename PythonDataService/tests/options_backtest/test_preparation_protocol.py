"""Actual socket framing, quiet bounded work, reconnect and lease ownership regressions."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.jobs import progress
from app.jobs.checkpoints import GatedLease
from app.jobs.progress import JobCancelled, ProgressEmitter
from app.options_backtest import preparation_stream
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.preparation import preparation_identity
from app.options_backtest.timings import RunTimings
from app.research.persistence.ownership import GenerationFencedError
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.utils.session_anchors import et_midnight_ms
from app.utils.timestamps import now_ms_utc
from tests.jobs.conftest import _FakeRedis
from tests.options_backtest.setup_fixtures import setup
from tests.options_backtest.test_chains import THU, WED, scene


@asynccontextmanager
async def socket_peer(handler: Callable[..., Any], monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    handlers: set[asyncio.Task] = set()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        handlers.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(int(row.split(b":", 1)[1]) for row in header.split(b"\r\n")
                          if row.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nContent-Type: application/x-ndjson\r\n\r\n")
            await writer.drain()
            await handler(writer)
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    monkeypatch.setattr(settings, "OPTIONS_BACKTEST_DATA_SERVICE_URL", f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    try:
        yield
    finally:
        server.close()
        running = list(handlers)
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await server.wait_closed()


async def send(writer: asyncio.StreamWriter, state: dict[str, Any]) -> None:
    payload = json.dumps({"kind": "preparation", "state": state}).encode() + b"\n"
    writer.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
    await writer.drain()


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = _FakeRedis()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    monkeypatch.setattr(settings, "OPTIONS_BACKTEST_INPUT_ROOT", tmp_path)
    req = OptionsBacktestJobRequest(job_id="quiet-io", setup=setup(),
        start_ms=et_midnight_ms(WED), end_ms=et_midnight_ms(THU))
    fake.lease(req.job_id)
    authority = SimpleNamespace(boot_generation=1, connected=True)
    lease = GatedLease(req.job_id, task_id="prep", authority=authority)
    state = {"identity": preparation_identity(req), "status": "collecting", "sequence": 1,
             "work_cursor": 0, "phase": "downloading", "done": 0, "total": 2, "wait": None, "timings": []}
    return req, fake, authority, lease, state


@pytest.mark.slow
async def test_quiet_socket_past_ten_seconds_waits_for_verified_completion_without_poll_renewals(
    owner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    req, fake, _, lease, state = owner
    frozen = freeze_run_inputs(req.setup, (WED, THU), scene(), tmp_path / req.job_id)
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
    frozen = freeze_run_inputs(req.setup, (WED, THU), scene(), tmp_path / req.job_id)
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
