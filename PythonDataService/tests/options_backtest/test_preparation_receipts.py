"""Receipts cross real acquisition threads and the provider/admission boundaries."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.data_lake.work_progress import observe_work
from app.options_backtest import service
from app.options_backtest.frozen_inputs import freeze_run_inputs
from app.options_backtest.lake_data import LakeRunData
from app.options_backtest.timings import RunTimings
from app.utils.timestamps import now_ms_utc
from tests.options_backtest import test_golden_spy_2025_06_10 as golden
from tests.options_backtest._golden_vendor import recorded_vendor
from tests.options_backtest._lake import fake_catalog as fake_catalog
from tests.options_backtest._lake import the_clock_is_fixed as the_clock_is_fixed
from tests.options_backtest._lake import tmp_lake as tmp_lake


async def test_real_provider_and_admission_emit_receipts_across_thread_and_background_loop(
    fake_catalog, tmp_lake: Path, tmp_path: Path,
) -> None:
    receipts = []
    given = golden.a_setup()
    hooks = service.RunHooks(lambda phase: None, lambda done, total: None, lambda message: None, lambda: False)
    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH), observe_work(receipts.append):
        await asyncio.to_thread(service.prepare_run_data, given, (golden.TRADING_DATE,), hooks, now_ms_utc, timings=RunTimings())
        frozen = await asyncio.to_thread(freeze_run_inputs, given, (golden.TRADING_DATE,), LakeRunData("SPY"), tmp_path / "inputs")
    work = [event for event in receipts if event["kind"] == "work"]
    assert {event["work_kind"] for event in work} == {"provider_response", "admitted_artifact"}
    assert len({event["identity"] for event in work if event["work_kind"] == "provider_response"}) >= 3
    waits = [event["bound_ms"] for event in receipts if event["kind"] == "external_wait"]
    assert {700_000, 660_000, 1_860_000} <= set(waits)
    assert len(waits) == sum(event["kind"] == "external_wait_end" for event in receipts)
    assert frozen.retained_bytes > 0


async def test_delayed_real_provider_collection_publishes_off_loop_and_drains_before_completion(
    fake_catalog, tmp_lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from datetime import timedelta

    from app.config import settings
    from app.engine.data.decoded_cache import DecodedArtifactCache
    from app.jobs import facade, progress
    from app.jobs.runner import prepared_worker
    from app.options_backtest import preparation
    from app.options_backtest.frozen_inputs import FrozenRunData
    from app.options_backtest.run import run_sessions
    from app.research.persistence import lifecycle
    from app.schemas.options_backtest_preparation import OptionsBacktestPreparationRequest
    from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
    from app.utils.background_loop import background_loop
    from app.utils.session_anchors import et_midnight_ms
    from tests.options_backtest._golden_vendor import the_recorded_answer
    from tests.options_backtest._preparation_jobs_fixture import _Store

    fake = _Store()
    on_loop = []

    def redis_store():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return fake
        on_loop.append(threading.current_thread().name)
        return fake

    monkeypatch.setattr(progress, "get_redis", redis_store)
    monkeypatch.setattr(preparation, "get_redis", redis_store)
    monkeypatch.setattr(lifecycle, "get_redis", redis_store)
    monkeypatch.setattr(settings, "OPTIONS_BACKTEST_INPUT_ROOT", tmp_path)
    req = OptionsBacktestJobRequest(job_id="provider-parent", setup=golden.a_setup(),
        start_ms=et_midnight_ms(golden.TRADING_DATE), end_ms=et_midnight_ms(golden.TRADING_DATE + timedelta(days=1)))
    request = OptionsBacktestPreparationRequest(job_id="provider-child", run_id=1, generation=1,
        identity=preparation.preparation_identity(req), parent=req)
    checks = []
    blocked, release = threading.Event(), threading.Event()
    actual_run_sync = preparation.run_sync

    async def ownership_check(*args, **kwargs):
        # This test isolates the receipt/event-loop seam; the HTTP fault
        # matrix supplies the real PostgreSQL ownership and generation fence.
        return None

    def checked_run_sync(coroutine):
        thread = threading.current_thread().name
        checks.append(thread)
        if thread.startswith("job-receipts-") and not blocked.is_set():
            blocked.set()
            assert release.wait(5), "test must release the blocked ownership observation"
        return actual_run_sync(coroutine)

    monkeypatch.setattr(preparation, "with_connection", ownership_check)
    monkeypatch.setattr(preparation, "run_sync", checked_run_sync)

    def collect():
        facade._record_queued(req.job_id, "options_backtest", {})
        fake.set(progress.lease_key(req.job_id), "parent")
        progress.ProgressEmitter(req.job_id, renews_lease=False).started()
        facade._record_queued(request.job_id, preparation.PREPARATION_JOB_TYPE, {}, internal=True)
        with prepared_worker(request.job_id, emit_renews_lease=False) as worker:
            worker.cancel.renews_lease = False
            thread = worker.start(lambda emit, cancel: preparation.collect_preparation(request, emit, cancel))
        thread.join(10)
        assert not thread.is_alive()
        assert fake.hget(progress.state_key(request.job_id), "status") == "completed"
        return preparation.read_checkpoint(tmp_path / req.job_id, request.identity)

    async def responsive():
        await asyncio.sleep(0)
        return True

    async def delayed_answer(http_request):
        await asyncio.sleep(0.6)
        return the_recorded_answer(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH, golden.CALL_600_PATH)(http_request)

    with recorded_vendor(golden.RECORDING, golden.RECORDED_FILES, golden.STOCK_PATH) as router:
        router.get(host="api.polygon.io", path=golden.CALL_600_PATH).mock(side_effect=delayed_answer)
        collecting = asyncio.create_task(asyncio.to_thread(collect))
        try:
            assert await asyncio.to_thread(blocked.wait, 5)
            # The publisher is waiting on ownership work, yet the very loop
            # used by acquisition still runs; should_stop also crosses it.
            probe = asyncio.run_coroutine_threadsafe(responsive(), background_loop())
            assert await asyncio.wait_for(asyncio.wrap_future(probe), timeout=0.5)
        finally:
            release.set()
        checkpoint = await collecting
        assert sum(call.request.url.path == golden.CALL_600_PATH for call in router.calls) == 1
    assert checkpoint.complete and checkpoint.inputs.retained_bytes > 0
    assert not on_loop
    assert any(thread.startswith("job-receipts-") for thread in checks)
    assert not any(thread.name.startswith("job-receipts-") for thread in threading.enumerate())
    expected = await asyncio.to_thread(run_sessions, req.setup, (golden.TRADING_DATE,), LakeRunData("SPY"))
    actual = await asyncio.to_thread(run_sessions, req.setup, (golden.TRADING_DATE,), FrozenRunData(checkpoint.inputs, DecodedArtifactCache(max_bytes=1_000_000)))
    assert actual == expected
