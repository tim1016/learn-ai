"""The one write-and-read discipline every sealed isolated-authority ledger uses.

The activation fences (``synthetic_activation.py``) and the shadow receipts
(``shadow_receipt.py``) are different records with different error types, but
they are the same file on disk: an append-only JSONL ledger whose rows are
sha256-sealed over their canonical JSON. Sealing, the symlink refusal and the
durable-append recipe live here once, so a correction to any of them -- adding
``O_NOFOLLOW``, changing when the parent directory is fsynced -- lands in one
place instead of protecting only one ledger.

Each caller keeps what is genuinely its own: its record type, its
``from_payload`` and its error semantics, passed in as ``invalid`` and
``label``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.engine.live.live_state_sidecar import _fsync_parent_dir


def canonical_sha256(payload: dict[str, Any]) -> str:
    """sha256 over the canonical JSON of ``payload`` -- the one sealing function
    every isolated-authority record uses."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def require_regular_ledger_file(path: Path, *, invalid: type[ValueError], label: str) -> None:
    """Refuse a symlinked or otherwise non-regular ledger before touching it."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise invalid(f"{label} ledger must be a regular file")


def append_canonical_jsonl_line(
    path: Path, payload: Mapping[str, Any], *, invalid: type[ValueError], label: str
) -> None:
    """Append one canonical JSON row and make it durable.

    The caller holds the advisory lock: an activation append must run its
    prior-generation check and this write as one transaction, so the lock
    cannot be taken here.
    """
    require_regular_ledger_file(path, invalid=invalid, label=label)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    with path.open("a", encoding="utf-8") as handle:
        # Byte-identical to ``canonical_sha256`` above, ensure_ascii included:
        # a row whose encoder disagreed with the hasher would seal to a digest
        # of bytes the file does not contain.
        handle.write(
            json.dumps(
                dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    if not existed:
        _fsync_parent_dir(path)


def read_canonical_jsonl_objects(path: Path, *, invalid: type[ValueError], label: str) -> list[Mapping[str, Any]]:
    """Every JSON object in the ledger, in file order; ``[]`` when it is absent.

    Rows are returned as raw payloads: mapping them to records is each store's
    own business, and so is what its ``from_payload`` raises.
    """
    require_regular_ledger_file(path, invalid=invalid, label=label)
    if not path.exists():
        return []
    payloads: list[Mapping[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw:
                continue
            payload = json.loads(raw)
            if not isinstance(payload, Mapping):
                raise invalid(f"{label} record has an invalid JSON payload shape")
            payloads.append(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise invalid(f"{label} ledger cannot be read") from exc
    return payloads


__all__ = [
    "append_canonical_jsonl_line",
    "canonical_sha256",
    "read_canonical_jsonl_objects",
    "require_regular_ledger_file",
]
