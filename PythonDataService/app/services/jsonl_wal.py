"""Shared append-only JSONL WAL primitive for sequenced Pydantic records."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.engine.live.live_state_sidecar import _fsync_parent_dir

RecordT = TypeVar("RecordT", bound=BaseModel)


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
    ) -> None:
        self._path = path
        self._record_model = record_model
        self._corrupt_error = corrupt_error
        self._seq_of = seq_of
        self._label = label
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def allocate_seq(self) -> int:
        if self._next_seq is None:
            existing = self.read_all()
            self._next_seq = (self._seq_of(existing[-1]) + 1) if existing else 1
        return self._next_seq

    def append(self, record: RecordT) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._truncate_tolerated_tail()
        expected_seq = self.allocate_seq()
        record_seq = self._seq_of(record)
        if record_seq != expected_seq:
            raise ValueError(
                f"{self._label} WAL append got seq={record_seq} but next "
                f"available seq is {expected_seq}"
            )
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
            self._next_seq = record_seq + 1
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)

    def read_all(self) -> list[RecordT]:
        return self.read_from(after_seq=0, limit=None)

    def read_from(self, *, after_seq: int, limit: int | None = None) -> list[RecordT]:
        if after_seq < 0:
            raise ValueError(f"after_seq must be >= 0; got {after_seq}")
        if not self._path.exists():
            return []
        raw = self._path.read_bytes()
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
                    self._path, f"unparseable line {idx + 1}: {exc}"
                ) from exc
            record_seq = self._seq_of(record)
            if record_seq <= last_seq:
                raise self._corrupt_error(
                    self._path,
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
        if not self._path.exists():
            return
        raw = self._path.read_bytes()
        if not raw or raw.endswith(b"\n"):
            return
        last_newline = raw.rfind(b"\n")
        truncate_at = last_newline + 1 if last_newline >= 0 else 0
        with open(self._path, "rb+") as fh:
            fh.truncate(truncate_at)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)
