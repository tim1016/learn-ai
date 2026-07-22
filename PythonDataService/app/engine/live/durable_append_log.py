"""Shared crash-durable append primitives for Clerk-owned artifacts.

This module owns only filesystem ordering: write the complete row, flush the
file, fsync it, then fsync its parent directory.  Callers retain their own
schemas, replay validation, and sequence rules; conflating those domain rules
would let one ledger accidentally define another ledger's authority.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from app.engine.live.identity import confine_path_to_root
from app.engine.live.live_state_sidecar import _fsync_parent_dir


def append_jsonl_record(
    path: Path,
    serialized_record: str,
    *,
    trusted_root: Path,
) -> None:
    """Append exactly one durable JSONL record and fsync its directory.

    The caller must name the durable artifact root independently of ``path``.
    This prevents a path assembled from an operator-supplied identity from
    escaping the intended ledger namespace, including via a leaf symlink.
    """

    _require_single_jsonl_record(serialized_record)
    path = confine_path_to_root(path, trusted_root, label="durable append log")
    path.parent.mkdir(parents=True, exist_ok=True)
    path = confine_path_to_root(path, trusted_root, label="durable append log")
    with path.open("a", encoding="utf-8") as file_handle:
        file_handle.write(serialized_record + "\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    _fsync_parent_dir(path)


def rewrite_jsonl_records(
    path: Path,
    serialized_records: Iterable[str],
    *,
    trusted_root: Path,
) -> None:
    """Atomically replace a confined JSONL log after record validation."""

    records = tuple(serialized_records)
    for record in records:
        _require_single_jsonl_record(record)
    path = confine_path_to_root(path, trusted_root, label="durable append log")
    path.parent.mkdir(parents=True, exist_ok=True)
    path = confine_path_to_root(path, trusted_root, label="durable append log")
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            for record in records:
                file_handle.write(record + "\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        path = confine_path_to_root(path, trusted_root, label="durable append log")
        os.replace(temporary_path, path)
        _fsync_parent_dir(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def create_exclusive_durable_file(
    path: Path,
    serialized_record: str,
    *,
    trusted_root: Path,
    mode: int = 0o600,
) -> None:
    """Durably create one confined claim file or raise when another writer owns it."""

    if "\x00" in serialized_record:
        raise ValueError("durable record must not contain a NUL byte")
    path = confine_path_to_root(path, trusted_root, label="durable append log")
    path.parent.mkdir(parents=True, exist_ok=True)
    path = confine_path_to_root(path, trusted_root, label="durable append log")
    file_descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write(serialized_record)
            file_handle.flush()
            os.fsync(file_handle.fileno())
    except BaseException:
        # A partial exclusive file remains a durable claim.  Deleting it could
        # permit a retry to repeat an effect that escaped before the crash.
        raise
    _fsync_parent_dir(path)


def _require_single_jsonl_record(serialized_record: str) -> None:
    if not serialized_record or "\r" in serialized_record or "\n" in serialized_record:
        raise ValueError("a durable JSONL append must contain exactly one JSONL row")


__all__ = [
    "append_jsonl_record",
    "create_exclusive_durable_file",
    "rewrite_jsonl_records",
]
