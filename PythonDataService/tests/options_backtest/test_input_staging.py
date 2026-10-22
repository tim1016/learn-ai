"""Concurrent reservation, isolated cleanup and crash leftovers over real files."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.options_backtest.input_staging import InputStaging, cleanup_job_inputs, job_root


def test_concurrent_collectors_cannot_overspend_global_staging_bytes(tmp_path: Path) -> None:
    ready = threading.Barrier(2)

    def collect(job: str) -> bool:
        with InputStaging(tmp_path, job, max_bytes=100) as staging:
            ready.wait(timeout=2)
            try:
                staging.reserve(80)
                (staging.root / "blob").write_bytes(b"retained")
                return True
            except ValueError as exc:
                assert "byte bound" in str(exc)
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(collect, ("one", "two")))
    assert sorted(results) == [False, True]
    assert sum(json.loads((tmp_path / ".reservations.json").read_text()).values()) == 80
    cleanup_job_inputs(tmp_path, "one")
    cleanup_job_inputs(tmp_path, "two")
    with InputStaging(tmp_path, "next", max_bytes=100) as staging:
        staging.reserve(100)


def test_crash_cleanup_releases_only_that_job_and_duplicate_collector_cannot_take_ownership(tmp_path: Path) -> None:
    with InputStaging(tmp_path, "live", max_bytes=100) as live:
        live.reserve(30)
        (live.root / "kept").write_bytes(b"live inputs")
        with pytest.raises(ValueError, match="already has"), InputStaging(tmp_path, "live", max_bytes=100):
            pytest.fail("a second collector must not enter")
        with InputStaging(tmp_path, "abandoned", max_bytes=100) as abandoned:
            abandoned.reserve(60)
            (abandoned.root / "partial").write_bytes(b"uncommitted")
        # A process crash releases the file lock while leaving the durable
        # reservation. The generation recovery supplies this exact job id.
        cleanup_job_inputs(tmp_path, "abandoned")
        assert (live.root / "kept").read_bytes() == b"live inputs"
        assert json.loads((tmp_path / ".reservations.json").read_text()) == {"live": 30}
    assert not job_root(tmp_path, "abandoned").exists()


def test_cleanup_refuses_symlinked_job_directory(tmp_path: Path) -> None:
    target = tmp_path / "unrelated"
    target.mkdir()
    (target / "kept").write_bytes(b"not staging")
    base = tmp_path / "staging"
    base.mkdir()
    (base / "job").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        cleanup_job_inputs(base, "job")
    assert (target / "kept").read_bytes() == b"not staging"


def test_verified_freeze_checkpoint_resumes_without_rereading_or_double_reserving_completed_sessions(tmp_path: Path) -> None:
    from app.engine.data.decoded_cache import DecodedArtifactCache
    from app.options_backtest.frozen_inputs import FrozenRunData, freeze_run_inputs
    from app.options_backtest.run import run_sessions
    from app.research.golden_search.proof import BlobStore, ProofMismatchError
    from tests.options_backtest._run_data import THU, WED, chain_scene
    from tests.options_backtest.setup_fixtures import setup

    given = setup()
    checkpoints = []

    class InterruptedFreeze(BaseException):
        pass

    def interrupt(frozen) -> None:
        checkpoints.append(frozen)
        raise InterruptedFreeze()

    with InputStaging(tmp_path, "resume") as staging:
        with pytest.raises(InterruptedFreeze):
            freeze_run_inputs(given, (WED, THU), chain_scene(), staging.root,
                              reserve_bytes=staging.reserve, on_checkpoint=interrupt)
        checkpoint = checkpoints[0]
        orphan = BlobStore(staging.root).put(b"crash between blob publication and checkpoint")
        staging.reserve(checkpoint.retained_bytes + 100)

    data = chain_scene()

    class RemainingData:
        def underlying_day(self, day):
            assert day != WED, "an already frozen session must not be admitted again"
            return data.underlying_day(day)

        def listing(self, day):
            assert day != WED
            return data.listing(day)

        def option_day(self, contract, day):
            assert day != WED
            return data.option_day(contract, day)

    with InputStaging(tmp_path, "resume") as staging:
        staging.retain_verified(checkpoint.sessions)
        assert not (staging.root / orphan[:2] / orphan).exists()
        assert json.loads((tmp_path / ".reservations.json").read_text())["resume"] == checkpoint.retained_bytes
        frozen = freeze_run_inputs(given, (WED, THU), RemainingData(), staging.root,
                                   reserve_bytes=staging.reserve, resume=checkpoint)
        assert run_sessions(given, (WED, THU), FrozenRunData(frozen, DecodedArtifactCache(max_bytes=1_000_000))) == run_sessions(given, (WED, THU), chain_scene())
        assert json.loads((tmp_path / ".reservations.json").read_text())["resume"] == frozen.retained_bytes
        digest = next(iter(checkpoint.sessions.values()))
        (staging.root / digest[:2] / digest).write_bytes(b"corrupt checkpoint input")
        with pytest.raises(ProofMismatchError):
            freeze_run_inputs(given, (WED, THU), RemainingData(), staging.root, resume=checkpoint)


def test_cleanup_has_a_deadline_and_preserves_reservation_until_collector_drains(tmp_path: Path) -> None:
    with InputStaging(tmp_path, "blocked") as staging:
        staging.reserve(100)
        with pytest.raises(TimeoutError, match="did not drain"):
            cleanup_job_inputs(tmp_path, "blocked", wait_seconds=0.01)
        assert json.loads((tmp_path / ".reservations.json").read_text()) == {"blocked": 100}
    cleanup_job_inputs(tmp_path, "blocked")
    assert json.loads((tmp_path / ".reservations.json").read_text()) == {}


@pytest.mark.parametrize("terminal_status", ["cancelled", "failed"])
def test_terminal_owner_drains_before_cleanup_and_late_receipts_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_status: str,
) -> None:
    from app.config import settings
    from app.jobs import facade, progress
    from app.jobs.runner import prepared_worker
    from app.options_backtest import preparation
    from app.schemas.options_backtest_preparation import OptionsBacktestPreparationRequest
    from app.schemas.options_backtest_runs import OptionsBacktestJobRequest
    from app.utils.session_anchors import et_midnight_ms
    from tests.options_backtest._preparation_jobs_fixture import _Store
    from tests.options_backtest._run_data import THU, WED
    from tests.options_backtest.setup_fixtures import setup

    monkeypatch.setattr(settings, "OPTIONS_BACKTEST_INPUT_ROOT", tmp_path)
    fake = _Store()
    monkeypatch.setattr(progress, "get_redis", lambda: fake)
    parent = OptionsBacktestJobRequest(job_id="stopped-collector", setup=setup(),
                                      start_ms=et_midnight_ms(WED), end_ms=et_midnight_ms(THU))
    request = OptionsBacktestPreparationRequest(job_id="api-collector", run_id=1, generation=1,
                                               identity=preparation.preparation_identity(parent), parent=parent)
    # Ownership is supplied by the isolated caller here; the real HTTP fault
    # tests exercise the PostgreSQL generation fence across both processes.
    monkeypatch.setattr(preparation._Owner, "check", lambda owner, **kwargs: owner.cancel.raise_if_cancelled())

    def stop(*args, **kwargs) -> None:
        if terminal_status == "cancelled":
            raise preparation.service.RunCancelled()
        raise OSError("preparation input is unavailable")

    monkeypatch.setattr(preparation.service, "prepare_run_data", stop)
    reached, proceed = threading.Event(), threading.Event()
    receipts = []
    deactivate = preparation.WorkReceipts.deactivate

    def paused_drain(sink) -> None:
        receipts.append(sink)
        reached.set()
        assert proceed.wait(5), "test must release the paused collector"
        deactivate(sink)
        if terminal_status == "failed":
            raise RuntimeError("receipt publisher could not confirm its final drain")

    monkeypatch.setattr(preparation.WorkReceipts, "deactivate", paused_drain)
    facade._record_queued(request.job_id, preparation.PREPARATION_JOB_TYPE, {}, internal=True)
    root = job_root(tmp_path, parent.job_id)
    with InputStaging(tmp_path, "unrelated") as unrelated:
        unrelated.reserve(100)
        (unrelated.root / "kept").write_bytes(b"other live inputs")
        with prepared_worker(request.job_id, emit_renews_lease=False) as worker:
            worker.cancel.renews_lease = False
            thread = worker.start(lambda emit, cancel: preparation.collect_preparation(request, emit, cancel))
        try:
            assert reached.wait(5)
            with pytest.raises(TimeoutError, match="did not drain"):
                cleanup_job_inputs(tmp_path, parent.job_id, wait_seconds=0)
            with pytest.raises(ValueError, match="already has"), InputStaging(tmp_path, parent.job_id):
                pytest.fail("the first collector still owns staging")
            assert root.exists()
            assert json.loads((tmp_path / ".reservations.json").read_text()) == {parent.job_id: 0, "unrelated": 100}
        finally:
            proceed.set()
            thread.join(5)
        assert not thread.is_alive()
        assert fake.hget(progress.state_key(request.job_id), "status") == terminal_status
        if terminal_status == "failed":
            assert "(OSError)" in fake.hget(progress.state_key(request.job_id), "error_message")
        cleanup_job_inputs(tmp_path, parent.job_id)
        published = list(fake.streams[progress.events_key(request.job_id)])
        receipts[0].work({"kind": "work", "work_kind": "provider_response", "identity": "late"})
        assert fake.streams[progress.events_key(request.job_id)] == published
        assert not root.exists()
        assert json.loads((tmp_path / ".reservations.json").read_text()) == {"unrelated": 100}
        assert (unrelated.root / "kept").read_bytes() == b"other live inputs"


def test_cleanup_keeps_one_lock_inode_through_delete_and_reservation_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.options_backtest import input_staging
    from app.options_backtest.input_staging import CollectorBusy

    with InputStaging(tmp_path, "reconnecting") as original:
        original.reserve(80)
        (original.root / "original").write_bytes(b"original inputs")
    deleted, proceed = threading.Event(), threading.Event()
    remove = input_staging.shutil.rmtree

    def paused_remove(path) -> None:
        remove(path)
        deleted.set()
        assert proceed.wait(5), "test must release cleanup after removing the job directory"

    monkeypatch.setattr(input_staging.shutil, "rmtree", paused_remove)
    with ThreadPoolExecutor(max_workers=1) as executor:
        cleanup = executor.submit(cleanup_job_inputs, tmp_path, "reconnecting")
        try:
            assert deleted.wait(5)
            # The root is absent, but its stable ownership lock still excludes
            # both reconnecting collectors and a second cleanup's ledger pop.
            with pytest.raises(CollectorBusy), InputStaging(tmp_path, "reconnecting"):
                pytest.fail("a removed directory must not create a second ownership inode")
            with pytest.raises(TimeoutError, match="did not drain"):
                cleanup_job_inputs(tmp_path, "reconnecting", wait_seconds=0)
            assert not job_root(tmp_path, "reconnecting").exists()
            assert json.loads((tmp_path / ".reservations.json").read_text()) == {"reconnecting": 80}
        finally:
            proceed.set()
            cleanup.result(timeout=5)
    assert json.loads((tmp_path / ".reservations.json").read_text()) == {}
    with InputStaging(tmp_path, "reconnecting") as successor:
        successor.reserve(100)
        (successor.root / "successor").write_bytes(b"new accounted inputs")
    assert json.loads((tmp_path / ".reservations.json").read_text()) == {"reconnecting": 100}
    assert (successor.root / "successor").read_bytes() == b"new accounted inputs"
