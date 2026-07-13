"""Filesystem-path containment guard for the shared bar store.

The policy-keyed store and the LEAN-format writers build paths from a
caller-supplied ``symbol``. Callers validate the symbol against the ticker
alphabet (``validate_symbol``), but the filesystem sink is additionally
confined here: rebuilding with ``realpath`` and checking a root prefix is the
path-injection sanitizer CodeQL recognizes (a bare ``commonpath`` is not), and
it catches symlink escapes at runtime. Mirrors the idiom already used in
``app/services/jsonl_wal.py`` and ``app/engine/live/account_registry.py``.
"""

from __future__ import annotations

import os
from pathlib import Path


def ensure_within_root(root: Path, candidate: Path) -> Path:
    """Return ``candidate`` resolved, after proving it stays under ``root``.

    Raises ``ValueError`` when the resolved candidate escapes ``root`` — via a
    ``..`` traversal or a symlink pointing outside the tree. The returned path
    is the ``realpath``-normalised form, so callers should use it (not the raw
    input) for the subsequent filesystem operation.
    """
    root_real = os.path.realpath(os.fspath(root))
    resolved = os.path.realpath(os.fspath(candidate))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if resolved != root_real and not resolved.startswith(root_prefix):
        raise ValueError(f"path {resolved!r} escapes root {root_real!r}")
    return Path(resolved)
