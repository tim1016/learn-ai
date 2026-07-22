"""Shared crash-durable append primitives for Clerk-owned artifacts.

This module owns only filesystem ordering: write the complete row, flush the
file, fsync it, then fsync its parent directory.  Callers retain their own
schemas, replay validation, and sequence rules; conflating those domain rules
would let one ledger accidentally define another ledger's authority.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from app.engine.live.live_state_sidecar import _fsync_parent_dir


def append_jsonl_record(path: Path, serialized_record: str) -> None:
    """Append exactly one durable JSONL record and fsync its directory."""

    _require_single_jsonl_record(serialized_record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file_handle:
        file_handle.write(serialized_record + "\n")
        file_handle.flush()
        os.fsync(file_handle.fileno())
    _fsync_parent_dir(path)


def rewrite_jsonl_records(path: Path, serialized_records: Iterable[str]) -> None:
    """Atomically replace a JSONL log after each supplied record is validated."""

    records = tuple(serialized_records)
    for record in records:
        _require_single_jsonl_record(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as file_handle:
            for record in records:
                file_handle.write(record + "\n")
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
        _fsync_parent_dir(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def create_exclusive_durable_file(path: Path, serialized_record: str, *, mode: int = 0o600) -> None:
    """Durably create one claim file or raise when another writer owns it."""

    if "\x00" in serialized_record:
        raise ValueError("durable record must not contain a NUL byte")
    path.parent.mkdir(parents=True, exist_ok=True)
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
