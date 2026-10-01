"""Durable-write primitives shared by the retained live artifact stores.

The live-state sidecar envelope and repo that once lived here were retired
with the IBKR runtime; only the parent-directory fsync and the advisory
sibling-file lock remain, used by the bot lifecycle, desired-state, durable
append-log, JSONL WAL and sealed-ledger writers.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path


def fsync_parent_dir(child_path: Path) -> None:
    """Fsync the parent directory entry so a fresh rename survives crash.

    Tempfile fsync flushes the file's own contents, but on POSIX the
    rename's directory entry can be lost on power loss without a
    separate dir fsync. On Windows this is a no-op — ReplaceFile is
    not subject to the same metadata-durability gap.
    """
    if sys.platform == "win32":
        return
    dir_fd = os.open(child_path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(dir_fd)


@contextlib.contextmanager
def _file_lock(target_path: Path, *, trusted_root: Path | None = None) -> Iterator[None]:
    """Advisory lock on a sibling .lock file for the duration of the write.

    POSIX uses fcntl.flock, Windows uses msvcrt.locking. Concurrent
    processes / threads writing the same path serialise here; the lock window
    is only as long as the atomic write.
    """
    root = target_path.parent if trusted_root is None else trusted_root
    root_real = os.path.realpath(os.fspath(root))
    candidate = os.path.realpath(os.fspath(target_path))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if not candidate.startswith(root_prefix):
        raise ValueError(f"lock path {candidate} escapes root {root_real}")
    safe_target_path = Path(candidate)
    lock_path = safe_target_path.with_suffix(safe_target_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")  # noqa: SIM115
    fh.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()
