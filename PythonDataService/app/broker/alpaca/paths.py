"""Shared account-scoped path safety for Alpaca Clerk stores.

Every per-account store rooted under ``accounts/alpaca/<account_id>/`` (the
JSONL journal, the SQLite clerk database, the established-accounts registry)
needs the same two guarantees: a hostile account id can never escape the
configured artifacts root, and directory-entry creation is durable before any
file inside that directory is trusted. This module is the one place those
guarantees are implemented.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ``sim:`` is the intentionally reserved synthetic-account namespace.  A
# colon is not a path separator on the supported POSIX artifact store, and the
# containment check below remains the authority against traversal.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.:-]+$")
_RESERVED_COMPONENTS = frozenset({".", ".."})


def safe_path_component(value: str, kind: str) -> str:
    """Return a traversal-safe path component or raise ``ValueError``."""
    if value in _RESERVED_COMPONENTS or not _SAFE_COMPONENT.match(value):
        raise ValueError(f"unsafe {kind} path component: {value!r}")
    return value


def resolve_contained_path(root: Path, *parts: str) -> Path:
    """Resolve ``root/parts...`` and guarantee containment within ``root``.

    ``os.path.realpath`` plus a ``rstrip(os.sep) + os.sep``-anchored
    ``startswith`` is the CodeQL-recognised path-injection sanitiser; a bare
    ``str.startswith`` without ``realpath`` is not.
    """
    root_real = os.path.realpath(os.fspath(root))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    candidate = os.path.realpath(os.fspath(Path(root, *parts)))
    if candidate != root_real and not candidate.startswith(root_prefix):
        raise ValueError(f"path escapes root: {candidate!r}")
    return Path(candidate)


def fsync_directory(path: Path) -> None:
    """``fsync`` one POSIX directory, propagating failures fail-closed."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory_chain(directory: Path, root: Path) -> None:
    """Persist newly-created directory entries from ``directory`` up to ``root``.

    Both ends are resolved via ``realpath`` before comparison: ``root`` is
    frequently an unresolved caller-supplied path (e.g. macOS's
    ``/var/folders/...`` symlinking to ``/private/var/folders/...``), while
    ``directory`` typically already comes from :func:`resolve_contained_path`.
    Comparing an unresolved root against a resolved descendant never matches,
    which walks past the filesystem root and spins forever once
    ``Path("/").parent == Path("/")``.
    """
    current = Path(os.path.realpath(os.fspath(directory)))
    root_real = Path(os.path.realpath(os.fspath(root)))
    while True:
        fsync_directory(current)
        if current == root_real:
            return
        parent = current.parent
        if parent == current:
            raise ValueError(
                f"fsync_directory_chain walked past filesystem root without reaching "
                f"{root_real} (started from {directory})"
            )
        current = parent
