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
