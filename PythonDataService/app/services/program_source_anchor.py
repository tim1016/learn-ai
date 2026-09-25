"""Anchor the Signal Program build proof to the bytes this process imports.

#2450 and its review: the anchor used to live only in
``signal_program_admission``, called from the service lifespan -- but by the
time the lifespan ran, ``import app.main`` had already pulled in the
strategy registry and, through it, every registered program module. A
``git pull`` landing in that window meant the anchor hashed the *new* disk
bytes while ``importlib.import_module`` handed back the *cached old*
module, so the recorded digest described code the process was never
running and a receipt minted for the pulled tree proved PROVEN against it.

This module therefore imports nothing beyond the pure declared-path data
in ``app.engine.strategy.program_sources``: ``app.main`` calls
:func:`record_imported_program_sources` at the very top, before the imports
that pull the registry, so the anchor hashes the declared sources before
any of them can be cached. The lifespan keeps calling the re-export in
``signal_program_admission`` as an idempotent no-op safety net.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
import threading
from pathlib import Path

from app.engine.strategy.program_sources import DECLARED_PROGRAM_SOURCE_PATHS

_SERVICE_ROOT = Path(__file__).resolve().parents[2]

_IMPORT_LOCK = threading.Lock()
_IMPORTED_SOURCE_DIGESTS: dict[str, str] = {}


def _resolve_artifact(relative: str) -> Path:
    """The one validated on-disk location for a service-relative source file."""
    candidate = (_SERVICE_ROOT / relative).resolve()
    if _SERVICE_ROOT not in candidate.parents or not candidate.is_file():
        raise ValueError(f"invalid Signal Program artifact path: {relative}")
    return candidate


def _module_name(relative: str) -> str:
    """The importable module name for one service-relative ``.py`` path."""
    if not relative.endswith(".py"):
        raise ValueError(f"Signal Program artifact is not a Python module: {relative}")
    return relative[:-3].replace("/", ".")


def record_imported_program_sources() -> None:
    """Anchor the proof's digests to the bytes this process actually imports (#2450).

    ``_digest_paths`` (in ``signal_program_admission``) hashes files off
    disk, but a clerk bind-mounts ``app/`` and deploys are a ``git pull``
    followed by a restart: between the two, the bytes on disk are not the
    bytes the process imported, and a proof computed from disk would name
    code that is not running -- including modules imported lazily
    (``indicator_state``), which the later pull can silently load
    mid-flight. This hashes every declared source file and then forces the
    import of every declared module, so the recorded digests describe the
    bytes this process loads, and any later divergence between disk and
    those bytes fails the drift check closed.

    #2450 review: a module that is *already cached* when the anchor runs can
    never be anchored honestly -- there is no way to recover the bytes it
    was loaded from, so a disk digest recorded now might describe bytes
    memory does not hold. The anchor refuses instead (fail closed): it is
    called before any program import at the top of ``app.main``, so a cached
    declared module means some import path got ahead of the anchor and the
    process cannot state its own sources.

    Idempotent: the first recording wins, because only it describes what
    this process imported. Raises on an unreadable or invalid path -- a
    process that cannot state its own sources must not start.
    """
    with _IMPORT_LOCK:
        if _IMPORTED_SOURCE_DIGESTS:
            return
        already_imported = sorted(
            relative
            for relative in DECLARED_PROGRAM_SOURCE_PATHS
            if _module_name(relative) in sys.modules
        )
        if already_imported:
            raise ValueError(
                "Signal Program sources were imported before the source anchor ran "
                f"(first: {already_imported[0]}, {len(already_imported) - 1} more); "
                "a digest read off disk now could describe bytes this process is "
                "not running. Restart the service so the anchor runs before any "
                "program import."
            )
        # Hash every declared file first, import second -- two full passes, not
        # one interleaved pass: the imports below load transitive declared
        # modules too, and an interleaved order could hash one of those after
        # it was already loaded. Either way the worst mid-window race fails
        # closed (a false "restart needed", never a false PROVEN): a pull
        # between the two passes leaves the anchor on pre-pull bytes while
        # memory and disk hold post-pull ones, which the drift check refuses.
        candidates = {
            relative: _resolve_artifact(relative) for relative in sorted(DECLARED_PROGRAM_SOURCE_PATHS)
        }
        digests = {
            relative: hashlib.sha256(candidate.read_bytes()).hexdigest()
            for relative, candidate in candidates.items()
        }
        for relative, candidate in candidates.items():
            module = importlib.import_module(_module_name(relative))
            # The import resolves through sys.path, the digest through
            # _SERVICE_ROOT; nothing else reconciles the two. A shadowing
            # copy on sys.path would otherwise anchor bytes the process
            # never executed, failing open forever.
            if Path(module.__file__ or "").resolve() != candidate:
                raise ValueError(
                    f"Signal Program artifact {relative} imported from "
                    f"{module.__file__}, not {_SERVICE_ROOT}; the proof cannot "
                    "name code this process is not running"
                )
        _IMPORTED_SOURCE_DIGESTS.update(digests)
