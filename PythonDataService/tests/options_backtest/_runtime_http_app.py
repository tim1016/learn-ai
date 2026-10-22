"""Separate real HTTP processes with acknowledgement-loss and crash-point faults."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import respx
from fastapi import Depends, FastAPI, Request

from app.options_backtest import preparation, saved_runs
from app.routers import options_backtest_runs
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.background_loop import CallerStoppedWaitingError

_FAULTS = Path(os.environ["OPTIONS_HTTP_TEST_FAULTS"])
_RUNTIME = os.environ.get("RESEARCH_RUNTIME") == "1"


def _pause_after_durable_checkpoint() -> None:
    actual = preparation.freeze_run_inputs

    def freeze(*args: Any, on_checkpoint: Any, **kwargs: Any) -> Any:
        def checkpoint(frozen: Any) -> None:
            on_checkpoint(frozen)
            marker = _FAULTS / "checkpoint"
            if marker.exists():
                return
            marker.write_text(frozen.model_dump_json())
            deadline = time.monotonic() + 90
            while not (_FAULTS / "release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test did not release its durable checkpoint")
                time.sleep(0.05)

        return actual(*args, on_checkpoint=checkpoint, **kwargs)

    preparation.freeze_run_inputs = freeze


if _RUNTIME:
    from app.research_runtime import app

    if (_FAULTS / "kill-chain").exists():
        from app.config import settings
        from app.jobs.checkpoints import GatedLease
        from app.research.golden_search.shared_pool import shared_worker_pool

        actual_checkpoint = GatedLease.child_checkpoint
        killed_chains: list[dict[str, Any]] = []

        def kill_same_chain(self: Any, message: Any) -> None:
            actual_checkpoint(self, message)
            if message.get("phase") != "preparation":
                return
            pool = shared_worker_pool(workers=settings.RESEARCH_WORKER_COUNT,
                submission_bound=2 * settings.RESEARCH_WORKER_COUNT,
                payload_bound_bytes=settings.RESEARCH_POOL_PAYLOAD_BOUND_BYTES,
                result_bound_bytes=settings.RESEARCH_POOL_RESULT_BOUND_BYTES)
            for lane in pool._lanes:
                if lane.current is None or lane.worker is None:
                    continue
                item = lane.current.item
                if killed_chains and item.key != killed_chains[0]["key"]:
                    continue
                killed_chains.append({"key": item.key, "task_gen": item.task_gen})
                (_FAULTS / "killed-chains.json").write_text(json.dumps(killed_chains))
                lane.worker._child.kill()
                return

        GatedLease.child_checkpoint = kill_same_chain

    if (_FAULTS / "lose-preparation-ack").exists():
        from app.broker.fleet.internal_http import build_internal_client
        from app.options_backtest import preparation_jobs

        def short_preparation_client(**kwargs: Any) -> Any:
            return build_internal_client(timeout_s=5, read_timeout_s=0.5)

        preparation_jobs.build_internal_client = short_preparation_client

    if (_FAULTS / "lose-launch-ack").exists():
        actual_start = threading.Thread.start
        _launch_ack_lost = False

        def lose_launch_ack(thread: threading.Thread) -> None:
            global _launch_ack_lost
            actual_start(thread)
            if not _launch_ack_lost and thread.name.startswith("options-backtest-"):
                _launch_ack_lost = True
                (_FAULTS / "launch-ack-lost").touch()
                raise OSError("test lost acknowledgement after actual worker thread start")

        threading.Thread.start = lose_launch_ack

    if (_FAULTS / "bind-commit-fault").exists():
        from asyncpg.transaction import Transaction

        actual_exit = Transaction.__aexit__
        _bind_faulted = False

        async def fault_bind_exit(self: Any, exc_type: Any, exc: Any, tb: Any) -> Any:
            global _bind_faulted
            from app.research.persistence import ownership

            authority = ownership.process_authority()
            target = (not _bind_faulted and exc_type is None and authority is not None
                      and self._connection is authority.connection
                      and await self._connection.fetchval("SELECT count(*) FROM research_job_owners") > 0)
            if target:
                _bind_faulted = True
                (_FAULTS / "bind-faulted").touch()
                if (_FAULTS / "bind-rollback").exists():
                    error = OSError("test rollback before actual worker launch")
                    await actual_exit(self, type(error), error, None)
                    raise error
                await actual_exit(self, exc_type, exc, tb)
                raise OSError("test lost binding acknowledgement after actual commit")
            return await actual_exit(self, exc_type, exc, tb)

        Transaction.__aexit__ = fault_bind_exit

    if (_FAULTS / "lose-save-ack").exists():
        actual_save = saved_runs.run_on_background_loop
        _lost = False

        def lose_committed_ack(coroutine: Any, *, timeout: float) -> Any:
            global _lost
            result = actual_save(coroutine, timeout=timeout)
            if not _lost:
                _lost = True
                (_FAULTS / "save-ack-lost").touch()
                raise CallerStoppedWaitingError("test lost acknowledgement after the real save committed")
            return result

        saved_runs.run_on_background_loop = lose_committed_ack

    @app.post("/test/release-authority", dependencies=[Depends(require_data_plane_control_secret)])
    async def release_authority() -> dict[str, bool]:
        from app.research.persistence import ownership

        held = ownership.process_authority()
        assert held is not None
        await held.release()
        return {"released": True}

    @app.middleware("http")
    async def delayed_dispatch_ack(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path.endswith("/options-backtest") and (_FAULTS / "lose-dispatch-ack").exists():
            (_FAULTS / "dispatch-ack-delayed").touch()
            await asyncio.sleep(1.5)
        return response
else:
    from app.broker.fleet.internal_http import build_internal_client
    from app.services import research_runtime_client

    _pause_after_durable_checkpoint()
    vendor_guard = respx.mock(assert_all_called=False, assert_all_mocked=False)
    vendor_guard.start()

    def forbid_provider_request(request: httpx.Request) -> httpx.Response:
        (_FAULTS / "unexpected-provider-request").touch()
        raise AssertionError("fully committed fixture inputs must not acquire provider data again")

    vendor_guard.route(host="api.polygon.io").mock(side_effect=forbid_provider_request)
    vendor_guard.route().pass_through()
    if (_FAULTS / "lose-dispatch-ack").exists():
        def short_dispatch_client(*, timeout_s: float) -> Any:
            return build_internal_client(timeout_s=timeout_s, read_timeout_s=0.5)

        research_runtime_client.build_internal_client = short_dispatch_client
        actual_dispatch = research_runtime_client._post_runtime_dispatch

        async def observe_dispatch_timeout(path: str, payload: dict[str, Any]) -> httpx.Response:
            try:
                return await actual_dispatch(path, payload)
            except httpx.ReadTimeout:
                (_FAULTS / "dispatch-ack-lost").touch()
                raise

        research_runtime_client._post_runtime_dispatch = observe_dispatch_timeout
    @asynccontextmanager
    async def api_lifespan(app: FastAPI):
        from app.config import settings
        from app.jobs.progress import fail_jobs_without_a_worker
        from app.research.persistence.ownership import startup_protection_only

        protection = await startup_protection_only(dsn=settings.POSTGRES_URL)
        protected = await protection() if protection is not None else set()
        await asyncio.to_thread(fail_jobs_without_a_worker, protected=lambda: protected)
        yield

    app = FastAPI(lifespan=api_lifespan)
    app.include_router(options_backtest_runs.jobs_router, prefix="/api/jobs-internal",
                       dependencies=[Depends(require_data_plane_control_secret)])
    app.include_router(options_backtest_runs.router, prefix="/api/options-backtest")

    @app.middleware("http")
    async def delayed_preparation_ack(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        if request.url.path.endswith("/prepare") and (_FAULTS / "lose-preparation-ack").exists():
            (_FAULTS / "preparation-ack-delayed").touch()
            await asyncio.sleep(1.5)
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}
