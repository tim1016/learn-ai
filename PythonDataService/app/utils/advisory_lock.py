"""Cross-process advisory locks for same-host artifact transactions."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _advisory_file_lock(target: Path, *, blocking: bool) -> Iterator[bool]:
    """Acquire one sibling lock, or report contention for a non-blocking caller."""

    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f".{target.name}.lock")
    handle = open(lock_path, "a+b")  # noqa: SIM115
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(
                    handle.fileno(),
                    msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,
                    1,
                )
            except OSError:
                if blocking:
                    raise
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(handle.fileno(), flags)
            except OSError:
                if blocking:
                    raise
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def advisory_file_lock(target: Path) -> Iterator[None]:
    """Serialize a transaction by a stable sibling lock file."""

    with _advisory_file_lock(target, blocking=True):
        yield


@contextmanager
def try_advisory_file_lock(target: Path) -> Iterator[bool]:
    """Try to serialize a transaction without blocking an async event loop.

    A caller that receives ``False`` must treat the related work as owned by
    another local process.  The lock remains held for the entire context on a
    ``True`` result, and is released automatically if the owning process
    crashes.  That makes it suitable for a durable action record whose
    callback may await a slow external system.
    """

    with _advisory_file_lock(target, blocking=False) as acquired:
        yield acquired
