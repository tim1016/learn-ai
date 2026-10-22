"""A shared job store and a live research owner for preparation protocol tests."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import LEGACY_ROOT_ID, settings
from app.data_lake.root_identity import init_empty_root
from app.jobs import facade, progress
from app.jobs.checkpoints import GatedLease
from app.options_backtest import preparation_binding, preparation_jobs
from app.research.persistence import lifecycle
from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
from app.utils.session_anchors import et_midnight_ms
from tests.jobs.conftest import _FakeRedis
from tests.jobs.test_facade import _JobStore
from tests.options_backtest._run_data import THU, WED
from tests.options_backtest.setup_fixtures import setup


class _Store(_JobStore):
    eval = _FakeRedis.eval

    def expire(self, key: str, seconds: int) -> int:
        self.expires.append((key, seconds))
        return self.exists(key)


@pytest.fixture
def owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "source"
    init_empty_root(source, LEGACY_ROOT_ID)
    monkeypatch.setattr(settings, "LEAN_DATA_WRITE_ROOT", str(source))
    monkeypatch.setattr(settings, "DATA_LAKE_ROOT_ID", "")
    monkeypatch.setattr(settings, "OPTIONS_BACKTEST_INPUT_ROOT", tmp_path)
    monkeypatch.setattr(settings, "OPTIONS_BACKTEST_DATA_SERVICE_URL", "http://127.0.0.1:1")
    fake = _Store()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    monkeypatch.setattr(lifecycle, "get_redis", lambda: fake)
    req = OptionsBacktestJobRequest(job_id="quiet-io", setup=setup(),
        start_ms=et_midnight_ms(WED), end_ms=et_midnight_ms(THU + timedelta(days=1)))
    fake.set(progress.lease_key(req.job_id), "owner")
    authority = SimpleNamespace(boot_generation=1, connected=True)
    lease = GatedLease(req.job_id, task_id="prep", authority=authority)
    # job_id is the bound child; queue_as hands out children a test created before the parent binds them.
    binding = SimpleNamespace(job_id=None, queue_as=[])
    queue_fresh_job = facade.queue_internal_job

    def queue_internal_job(job_type, body):
        return binding.queue_as.pop(0) if binding.queue_as else queue_fresh_job(job_type, body)

    async def current(*args, **kwargs):
        return binding.job_id

    async def bind(conn, run_id, parent_job_id, generation, identity, job_id, **kwargs):
        binding.job_id = job_id
        return job_id

    async def connection(fn, *args, **kwargs):
        return await fn(None, *args, **kwargs)

    async def writer(coroutine):
        return await coroutine

    monkeypatch.setattr(facade, "queue_internal_job", queue_internal_job)
    monkeypatch.setattr(preparation_binding, "current_binding", current)
    monkeypatch.setattr(preparation_binding, "bind_preparation", bind)
    monkeypatch.setattr(preparation_jobs, "with_connection", connection)
    monkeypatch.setattr(preparation_jobs, "writer_loop_call", writer)
    return req, fake, authority, lease, binding
