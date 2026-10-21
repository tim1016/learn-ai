"""Separate real HTTP processes with acknowledgement-loss and crash-point faults."""
from __future__ import annotations

import asyncio
import os
import time
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

    if (_FAULTS / "lose-launch-ack").exists():
        actual_accept = saved_runs.DbRunStore.accept_runtime_run
        _launch_ack_lost = False

        async def lose_launch_ack(self: Any, new: Any, work_for_run: Any) -> int:
            global _launch_ack_lost
            run_id = await actual_accept(self, new, work_for_run)
            if not _launch_ack_lost:
                _launch_ack_lost = True
                (_FAULTS / "launch-ack-lost").touch()
                raise CallerStoppedWaitingError("test lost acknowledgement after actual worker launch")
            return run_id

        saved_runs.DbRunStore.accept_runtime_run = lose_launch_ack

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
    app = FastAPI()
    app.include_router(options_backtest_runs.jobs_router, prefix="/api/jobs-internal",
                       dependencies=[Depends(require_data_plane_control_secret)])
    app.include_router(options_backtest_runs.router, prefix="/api/options-backtest")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}
