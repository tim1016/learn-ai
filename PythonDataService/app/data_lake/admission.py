"""Catalog admission for exact lake bytes (#2456).

The filesystem rename precedes the database commit. Existence is therefore
not evidence of publication. Every consumer of a managed lake file verifies
its bytes against a committed, root- and mode-scoped receipt before parsing.
There is deliberately no positive cache: a refresh revokes admission even
when the file's inode and timestamps have not changed yet.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import asyncpg

from app.data_lake import catalog_client
from app.data_lake.path_policy import lake_root_for
from app.data_lake.root_identity import LakeRootIdentityError, read_marker
from app.utils.background_loop import CallerStoppedWaitingError, run_on_background_loop


class LakeAdmissionError(RuntimeError):
    """The bytes have no verifiable committed lake receipt; retry capture."""


def read_committed_bytes(path: Path) -> bytes:
    """Read once, then admit those exact bytes. Call from a worker thread.

    Plain LEAN reference fixtures retain their file-only contract. Managed
    lake paths require an identity marker even when Postgres is unavailable.
    """
    payload = path.read_bytes()
    root = lake_root_for(path) or lake_root_for(path.resolve())
    if root is None:
        return payload
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True
    if running_loop:
        raise RuntimeError("lake admission requires a worker thread; use asyncio.to_thread")
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(root.resolve()).as_posix()
        marker = read_marker(root.parent.parent)
        if marker is None:
            raise LakeAdmissionError(f"{path}: no root identity for committed lake admission")
        digest = hashlib.sha256(payload).hexdigest()

        async def admitted() -> bool:
            await catalog_client.init_pool()
            return await catalog_client.has_committed_file_receipt(
                marker.data_root_id, root.name, relative, digest, len(payload),
            )

        if not run_on_background_loop(admitted(), timeout=35):
            raise LakeAdmissionError(f"{path}: no committed catalog receipt for these bytes; retry capture")
    except (LakeRootIdentityError, ValueError) as exc:
        raise LakeAdmissionError(f"{path}: cannot verify committed lake root identity") from exc
    except (catalog_client.CatalogUnavailableError, asyncpg.PostgresError,
            OSError, TimeoutError, CallerStoppedWaitingError) as exc:
        # An outage is not a per-file gap. Propagate it so a range probe
        # stops after one failure instead of paying a timeout for every day.
        raise catalog_client.CatalogUnavailableError(
            f"{path}: cannot verify committed lake receipt ({type(exc).__name__})"
        ) from exc
    return payload
