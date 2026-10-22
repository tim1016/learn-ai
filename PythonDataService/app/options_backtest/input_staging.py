"""Bounded per-job frozen-input staging shared by API and research runtime.

The filesystem lock protects a durable global byte reservation before each
blob publication. Job directories never share blobs. Cleanup takes the job's
collection lock before releasing its reservation, so an interrupted collector
cannot recreate unaccounted bytes after cleanup. Startup removes only jobs
the existing generation authority has already declared abandoned.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
from collections.abc import Collection, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from app.engine.live.identity import confine_path_to_root
from app.utils.advisory_lock import advisory_file_lock, lock_file_for, try_advisory_file_lock
from app.utils.atomic_file import atomic_write_bytes

MAX_STAGED_BYTES = 8 * 1024**3
_JOB = re.compile(r"[A-Za-z0-9_-]{1,128}")
logger = logging.getLogger(__name__)


class CollectorBusy(ValueError):
    """Another live collector already owns this exact preparation."""


def job_root(base: Path, job_id: str) -> Path:
    if not _JOB.fullmatch(job_id):
        raise ValueError("invalid frozen input job identity")
    base = base.resolve()
    path = base / job_id
    if path.is_symlink():
        raise ValueError("a frozen input job directory cannot be a symlink")
    return path


def _collector_target(base: Path, job_id: str) -> Path:
    """The collector lock's target; its lock inode outlives the removable job directory and every cleanup."""
    job_root(base, job_id)
    locks = base.resolve() / ".collector-locks"
    if locks.is_symlink():
        raise ValueError("collector lock directory cannot be a symlink")
    target = confine_path_to_root(locks, base, label="collector lock directory") / job_id
    if lock_file_for(target).is_symlink():
        raise ValueError("a collector lock file cannot be a symlink")
    return target


@contextmanager
def _ledger(base: Path) -> Iterator[dict[str, int]]:
    path = base / ".reservations.json"
    with advisory_file_lock(path):
        entries = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(entries, dict) or any(not _JOB.fullmatch(key) or type(size) is not int or size < 0
                                                for key, size in entries.items()):
            raise ValueError("frozen input reservation ledger is invalid")
        yield entries
        atomic_write_bytes(path, json.dumps(entries, sort_keys=True, separators=(",", ":")).encode())


class InputStaging:
    def __init__(self, base: Path, job_id: str, *, max_bytes: int = MAX_STAGED_BYTES) -> None:
        self.base, self.job_id = base.resolve(), job_id
        self.root = job_root(base, job_id)
        self.max_bytes = max_bytes
        self._lock: ExitStack | None = None

    def __enter__(self) -> InputStaging:
        with ExitStack() as held:
            if not held.enter_context(try_advisory_file_lock(_collector_target(self.base, self.job_id))):
                raise CollectorBusy("that job already has a frozen input collector")
            self.root = job_root(self.base, self.job_id)
            self.root.mkdir(parents=True, exist_ok=True)
            with _ledger(self.base) as entries:
                entries.setdefault(self.job_id, 0)
            self._lock = held.pop_all()
        return self

    def reserve(self, retained_bytes: int) -> None:
        if self._lock is None:
            raise ValueError("input staging reservation requires its collection lock")
        with _ledger(self.base) as entries:
            previous = entries.get(self.job_id)
            if previous is None or retained_bytes < previous:
                raise ValueError("staging reservation is missing or moved backwards")
            if sum(entries.values()) - previous + retained_bytes > self.max_bytes:
                raise ValueError("global frozen input staging byte bound exceeded")
            entries[self.job_id] = retained_bytes

    def retain_verified(self, inventory: Collection[str]) -> None:
        """Before replay is dispatched, remove only this interrupted freeze's orphan blobs."""
        from app.research.golden_search.proof import BlobStore

        if self._lock is None:
            raise ValueError("checkpoint reconciliation requires the collection lock")
        blobs = BlobStore(self.root)
        kept = set(inventory)
        size = sum(len(blobs.get(digest)) for digest in kept)
        for directory in self.root.iterdir():
            if directory.is_dir() and re.fullmatch(r"[a-f0-9]{2}", directory.name):
                if directory.is_symlink():
                    raise ValueError("staging blob directories cannot be symlinks")
                for path in directory.iterdir():
                    if path.is_symlink():
                        raise ValueError("staging blobs cannot be symlinks")
                    if re.fullmatch(r"[a-f0-9]{64}", path.name) and path.name not in kept:
                        path.unlink()
        with _ledger(self.base) as entries:
            if sum(value for key, value in entries.items() if key != self.job_id) + size > self.max_bytes:
                raise ValueError("global frozen input staging byte bound exceeded")
            entries[self.job_id] = size

    def __exit__(self, *args: object) -> None:
        if self._lock is not None:
            self._lock.close()
            self._lock = None


def cleanup_job_inputs(base: Path, job_id: str, *, wait_seconds: float = 75.0) -> None:
    target = _collector_target(base, job_id)
    deadline = time.monotonic() + wait_seconds
    while True:
        with try_advisory_file_lock(target) as acquired:
            if acquired:
                root = job_root(base, job_id)
                if root.exists():
                    # Delete bytes before releasing capacity. The stable lock
                    # excludes new collectors through the reservation release.
                    shutil.rmtree(root)
                with _ledger(base.resolve()) as entries:
                    entries.pop(job_id, None)
                return
        if time.monotonic() >= deadline:
            raise TimeoutError("input collector did not drain; its reservation remains held")
        time.sleep(0.1)


def release_job_inputs(base: Path, job_id: str) -> None:
    """Cleanup failures stay visible without reversing a committed run."""
    try:
        cleanup_job_inputs(base, job_id)
    except (OSError, ValueError):
        logger.error("frozen input cleanup failed; its byte reservation remains held",
                     extra={"action": "options_backtest_staging_cleanup_failed", "job_id": job_id}, exc_info=True)
