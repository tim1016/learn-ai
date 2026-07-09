"""Shared append-only JSONL WAL primitive for sequenced Pydantic records."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.engine.live.live_state_sidecar import _fsync_parent_dir

RecordT = TypeVar("RecordT", bound=BaseModel)


def confined_wal_path(root: Path, filename: str) -> Path:
    """Return ``root/filename`` after proving it stays below ``root``.

    ``root`` is a service-owned directory selected by a caller that already
    validated the enclosing run or instance id. ``filename`` is a trusted
    literal. Rebuilding with ``realpath`` + a root-prefix check keeps the
    filesystem sink visibly confined for CodeQL and catches symlink escapes at
    runtime.
    """
    if not filename or filename != Path(filename).name:
        raise ValueError(f"WAL filename must be one path segment: {filename!r}")
    root_real = os.path.realpath(root)
    candidate = os.path.realpath(os.path.join(root_real, filename))
    root_prefix = root_real.rstrip(os.sep) + os.sep
    if candidate != root_real and not candidate.startswith(root_prefix):
        raise ValueError(f"WAL path {candidate} escapes root {root_real}")
    return Path(candidate)


class JsonlWal(Generic[RecordT]):  # noqa: UP046 - Python 3.11 runtime; PEP 695 needs 3.12.
    """Canonical append/read discipline for sequenced JSONL WAL files."""

    def __init__(
        self,
        path: Path,
        *,
        record_model: type[RecordT],
        corrupt_error: Callable[[Path, str], RuntimeError],
        seq_of: Callable[[RecordT], int],
        label: str,
        trusted_root: Path | None = None,
    ) -> None:
        self._path = path
        self._trusted_root = trusted_root if trusted_root is not None else path.parent
        self._record_model = record_model
        self._corrupt_error = corrupt_error
        self._seq_of = seq_of
        self._label = label
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"{self._label} WAL path {candidate} escapes root {root_real}")
        return Path(candidate)

    def allocate_seq(self) -> int:
        if self._next_seq is None:
            existing = self.read_all()
            self._next_seq = (self._seq_of(existing[-1]) + 1) if existing else 1
        return self._next_seq

    def append(self, record: RecordT) -> None:
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"{self._label} WAL path {candidate} escapes root {root_real}")
        path = Path(candidate)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._truncate_tolerated_tail()
        expected_seq = self.allocate_seq()
        record_seq = self._seq_of(record)
        if record_seq != expected_seq:
            raise ValueError(
                f"{self._label} WAL append got seq={record_seq} but next "
                f"available seq is {expected_seq}"
            )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
            self._next_seq = record_seq + 1
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(path)

    def read_all(self) -> list[RecordT]:
        return self.read_from(after_seq=0, limit=None)

    def read_from(self, *, after_seq: int, limit: int | None = None) -> list[RecordT]:
        if after_seq < 0:
            raise ValueError(f"after_seq must be >= 0; got {after_seq}")
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"{self._label} WAL path {candidate} escapes root {root_real}")
        path = Path(candidate)
        if not path.exists():
            return []
        raw = path.read_bytes()
        if not raw:
            return []
        ends_with_newline = raw.endswith(b"\n")
        byte_lines = raw.split(b"\n")
        if byte_lines and byte_lines[-1] == b"":
            byte_lines.pop()

        rows: list[RecordT] = []
        last_seq = 0
        n = len(byte_lines)
        for idx, bline in enumerate(byte_lines):
            if idx == n - 1 and not ends_with_newline:
                break
            try:
                record = self._record_model.model_validate_json(bline)
            except (ValidationError, ValueError) as exc:
                raise self._corrupt_error(
                    path, f"unparseable line {idx + 1}: {exc}"
                ) from exc
            record_seq = self._seq_of(record)
            if record_seq <= last_seq:
                raise self._corrupt_error(
                    path,
                    f"non-monotonic seq at line {idx + 1}: {record_seq} after {last_seq}",
                )
            last_seq = record_seq
            if record_seq > after_seq:
                rows.append(record)
                if limit is not None and len(rows) >= limit:
                    break
        return rows

    def last_seq(self) -> int:
        rows = self.read_all()
        return self._seq_of(rows[-1]) if rows else 0

    def _truncate_tolerated_tail(self) -> None:
        root_real = os.path.realpath(os.fspath(self._trusted_root))
        candidate = os.path.realpath(os.fspath(self._path))
        root_prefix = root_real.rstrip(os.sep) + os.sep
        if not candidate.startswith(root_prefix):
            raise ValueError(f"{self._label} WAL path {candidate} escapes root {root_real}")
        path = Path(candidate)
        if not path.exists():
            return
        raw = path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        last_newline = raw.rfind(b"\n")
        truncate_at = last_newline + 1 if last_newline >= 0 else 0
        with open(path, "rb+") as fh:
            fh.truncate(truncate_at)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(path)
