"""Resumable preparation observer with real-work deadlines and bounded cancellation."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx

from app.broker.fleet.internal_http import build_internal_client, enforce_private_http_target
from app.config import settings
from app.jobs.checkpoints import GatedLease
from app.jobs.progress import JobCancelled, ProgressEmitter
from app.options_backtest import service
from app.options_backtest.frozen_inputs import FrozenRunInputs
from app.options_backtest.input_staging import job_root
from app.options_backtest.preparation import MAX_EXTERNAL_WAIT_MS, runtime_preparation_identity
from app.options_backtest.saved_runs import RunFailure
from app.options_backtest.timings import PhaseTiming, RunTimings
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.timestamps import now_ms_utc

STALL_SECONDS = 65.0
RECONNECT_SECONDS = 60.0
CANCEL_POLL_SECONDS = 0.2
MAX_FRAME_BYTES = 2 * 1024**2


async def _frames(client: httpx.AsyncClient, url: str, req: OptionsBacktestJobRequest) -> AsyncIterator[dict[str, Any]]:
    async with client.stream("POST", url, json=req.model_dump(mode="json"),
            headers={CONTROL_SECRET_HEADER: settings.DATA_PLANE_CONTROL_SECRET}) as response:
        response.raise_for_status()
        buffer = b""
        async for chunk in response.aiter_bytes():
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if len(line) > MAX_FRAME_BYTES:
                    raise ValueError("preparation response exceeds its frame bound")
                yield json.loads(line)
            if len(buffer) > MAX_FRAME_BYTES:
                raise ValueError("preparation response exceeds its frame bound")
        raise httpx.RemoteProtocolError("preparation stream ended before a terminal checkpoint")


class _Observation:
    def __init__(self, identity: str, emit: ProgressEmitter, lease: GatedLease) -> None:
        self.identity, self.emit, self.lease = identity, emit, lease
        self.sequence, self.cursor = -1, 0
        self.phase: str | None = None
        self.wait: dict[str, int] | None = None
        self.deadline = time.monotonic() + STALL_SECONDS

    def accept(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        if frame.get("kind") != "preparation":
            raise ValueError("unknown preparation event")
        state = frame["state"]
        if state["identity"] != self.identity:
            raise ValueError("preparation response binds another request, root, or execution generation")
        if state["sequence"] <= self.sequence:
            return None
        self.sequence = state["sequence"]
        advanced = False
        if state["phase"] != self.phase:
            self.phase = state["phase"]
            self.emit.phase(self.phase)
            self.lease.phase(self.phase)
            advanced = True
        if state["work_cursor"] > self.cursor:
            self.cursor = state["work_cursor"]
            self.lease.child_checkpoint({"cursor": self.cursor, "cursor_kind": "admitted_artifacts", "phase": self.phase})
            self.emit.progress(state["done"], state["total"], unit="sessions")
            advanced = True
        wait = state["wait"]
        if wait != self.wait:
            if wait is None:
                self.lease.external_wait_finished()
            else:
                remaining_ms = wait["deadline_ms"] - now_ms_utc()
                if not 0 < remaining_ms <= MAX_EXTERNAL_WAIT_MS:
                    raise ValueError("preparation wait deadline is expired or exceeds its declared bound")
                self.lease.bounded_external_wait(wait["id"], wait["deadline_ms"])
                self.deadline = time.monotonic() + remaining_ms / 1000 + 5
            self.wait = wait
            advanced = True
        if advanced and self.wait is None:
            self.deadline = time.monotonic() + STALL_SECONDS
        return state


async def prepare_over_api(req: OptionsBacktestJobRequest, *, emit: ProgressEmitter,
                           lease: GatedLease, timings: RunTimings) -> FrozenRunInputs:
    closed: list[PhaseTiming] = []
    try:
        return await _prepare(req, emit, lease, closed)
    finally:
        # Each journal frame carries the cumulative closed spans. Keep the
        # latest once, on interruption too, never once per reconnect.
        timings.adopt(closed)


async def _prepare(req: OptionsBacktestJobRequest, emit: ProgressEmitter, lease: GatedLease,
                   closed: list[PhaseTiming]) -> FrozenRunInputs:
    base = settings.OPTIONS_BACKTEST_DATA_SERVICE_URL.strip().rstrip("/")
    if not base:
        raise RuntimeError("OPTIONS_BACKTEST_DATA_SERVICE_URL is required; replay never acquires provider data")
    await asyncio.to_thread(enforce_private_http_target, base)
    observation = _Observation(await asyncio.to_thread(runtime_preparation_identity, req), emit, lease)
    reconnect_deadline: float | None = None
    attempts = 0
    # Connect/write/pool stay bounded. The observer below owns read liveness,
    # including response headers, and polls cancellation without renewing.
    async with build_internal_client(timeout_s=10, read_timeout_s=None) as client:
        while True:
            frames = _frames(client, f"{base}/api/jobs-internal/options-backtest/prepare", req)
            pending = asyncio.create_task(anext(frames))
            try:
                while True:
                    lease.fenced_cancel()
                    if time.monotonic() > observation.deadline:
                        raise TimeoutError("preparation stopped advancing beyond its bounded work deadline")
                    done, _ = await asyncio.wait({pending}, timeout=CANCEL_POLL_SECONDS)
                    if not done:
                        continue
                    state = observation.accept(pending.result())
                    if state is not None:
                        closed[:] = [PhaseTiming.model_validate(row) for row in state["timings"]]
                        reconnect_deadline = None
                        attempts = 0
                        if state["status"] == "complete":
                            frozen = FrozenRunInputs.model_validate(state["inputs"])
                            if frozen.root != str(job_root(settings.OPTIONS_BACKTEST_INPUT_ROOT, req.job_id)):
                                raise ValueError("prepared input root differs from the runtime's configured shared root")
                            return frozen
                        if state["status"] == "cancelled":
                            raise JobCancelled("input preparation was cancelled")
                        if state["status"] == "failed":
                            raise service.RunFailedError(RunFailure(state["code"], state["reason"]))
                        if state["status"] != "collecting":
                            raise ValueError("unknown preparation lifecycle status")
                    pending = asyncio.create_task(anext(frames))
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (502, 503, 504):
                    raise
                attempts += 1
                if reconnect_deadline is None:
                    reconnect_deadline = time.monotonic() + RECONNECT_SECONDS
                if time.monotonic() >= reconnect_deadline:
                    raise
            finally:
                pending.cancel()
                with suppress(asyncio.CancelledError, httpx.HTTPError, StopAsyncIteration):
                    await pending
                await frames.aclose()
            # A restart does not change preparation identity. Reconnect joins
            # its lock or verifies its completed inventory; it never replays a
            # provider response merely because the socket was lost.
            retry_at = min(time.monotonic() + min(attempts * 0.2, 2), reconnect_deadline)
            while time.monotonic() < retry_at:
                lease.fenced_cancel()
                await asyncio.sleep(CANCEL_POLL_SECONDS)
