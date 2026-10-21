"""Same-host exclusion between lake activity and legacy retirement (#3432).

Readers hold shared access until their exact bytes have been admitted;
publishers hold it through the catalog commit. Backup holds the same shared
access for its source snapshot. Retirement alone takes exclusive access.
Lock contention refuses promptly, including on an async event loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.utils.advisory_lock import try_advisory_file_lock


class LakeMaintenanceBusyError(RuntimeError):
    """An exclusive lake maintenance operation or active reader owns the root."""


@contextmanager
def lake_activity(mode_root: Path, *, exclusive: bool = False) -> Iterator[None]:
    """Protect a mode root, sharing one stable lock across all its modes.

    ``mode_root`` is the canonical ``lake/<adjustment-mode>`` directory.
    The lock lives beside those modes, so removing a legacy artifact never
    removes its lock inode. No catalog transaction may wait for this lock.
    """
    with try_advisory_file_lock(mode_root.resolve().parent / "maintenance", shared=not exclusive) as acquired:
        if not acquired:
            raise LakeMaintenanceBusyError("The lake has active readers, publishers, backup, or maintenance; retry later.")
        yield
