"""A real socket peer that streams preparation journal frames, and the runtime owner a preparation runs under."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.jobs import progress
from app.jobs.checkpoints import GatedLease
from app.options_backtest.preparation import preparation_identity
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.utils.session_anchors import et_midnight_ms
from tests.jobs.conftest import _FakeRedis
from tests.options_backtest._run_data import THU, WED
from tests.options_backtest.setup_fixtures import setup


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
    from app.config import LEGACY_ROOT_ID
    from app.data_lake.root_identity import init_empty_root

    source = tmp_path / "source"
    init_empty_root(source, LEGACY_ROOT_ID)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(source))
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
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
