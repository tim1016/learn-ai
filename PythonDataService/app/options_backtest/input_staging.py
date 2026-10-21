"""Bounded per-job frozen-input staging shared by API and research runtime.

The filesystem lock protects a durable global byte reservation before each
blob publication. Job directories never share blobs. Cleanup takes the job's
collection lock before releasing its reservation, so an interrupted collector
cannot recreate unaccounted bytes after cleanup. Startup removes only jobs
the existing generation authority has already declared abandoned.
"""
from __future__ import annotations

import fcntl
import json
import logging
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app.utils.atomic_file import atomic_write_bytes

MAX_STAGED_BYTES = 8 * 1024**3
_JOB = re.compile(r"[A-Za-z0-9_-]{1,128}")
logger = logging.getLogger(__name__)


def job_root(base: Path, job_id: str) -> Path:
    if not _JOB.fullmatch(job_id):
        raise ValueError("invalid frozen input job identity")
    base = base.resolve()
    path = base / job_id
    if path.is_symlink():
        raise ValueError("a frozen input job directory cannot be a symlink")
    return path


@contextmanager
def _ledger(base: Path) -> Iterator[dict[str, int]]:
    base.mkdir(parents=True, exist_ok=True)
    with (base / ".reservations.lock").open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            path = base / ".reservations.json"
            entries = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(entries, dict) or any(not _JOB.fullmatch(key) or type(size) is not int or size < 0
                                                    for key, size in entries.items()):
                raise ValueError("frozen input reservation ledger is invalid")
            yield entries
            atomic_write_bytes(path, json.dumps(entries, sort_keys=True, separators=(",", ":")).encode())
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class InputStaging:
    def __init__(self, base: Path, job_id: str, *, max_bytes: int = MAX_STAGED_BYTES) -> None:
        self.base, self.job_id = base.resolve(), job_id
        self.root = job_root(base, job_id)
        self.max_bytes = max_bytes
        self._lock: BinaryIO | None = None

    def __enter__(self) -> InputStaging:
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = (self.root / ".collector.lock").open("a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise ValueError("that job already has a frozen input collector") from None
        try:
            with _ledger(self.base) as entries:
                entries.setdefault(self.job_id, 0)
        except BaseException:
            self.__exit__()
            raise
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

    def __exit__(self, *args: object) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None


def cleanup_job_inputs(base: Path, job_id: str) -> None:
    root = job_root(base, job_id)
    if root.exists():
        with (root / ".collector.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                # Delete bytes before releasing capacity. A crash can leak
                # a reservation, never allow the cap to be overspent.
                shutil.rmtree(root)
                with _ledger(base.resolve()) as entries:
                    entries.pop(job_id, None)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    else:
        with _ledger(base.resolve()) as entries:
            entries.pop(job_id, None)


def release_job_inputs(base: Path, job_id: str) -> None:
    """Cleanup failures stay visible without reversing a committed run."""
    try:
        cleanup_job_inputs(base, job_id)
    except (OSError, ValueError):
        logger.error("frozen input cleanup failed; its byte reservation remains held",
                     extra={"action": "options_backtest_staging_cleanup_failed", "job_id": job_id}, exc_info=True)
