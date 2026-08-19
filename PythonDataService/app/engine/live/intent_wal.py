"""Strict read-only parser for historical IBKR intent WAL evidence.

Exactly one anomaly is tolerated: a single trailing line without a newline.
Every other malformed or non-monotonic row raises ``IntentWalCorruptError``.
The retired submit runtime is the only code that authored these files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.intent_events import IntentEvent


class IntentWalCorruptError(RuntimeError):
    """Raised by ``read_tail`` on any malformation other than a single
    tolerated trailing partial line. Routes to a ``Poisoned`` cold-start
    outcome — a corrupt WAL cannot be safely folded."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"intent WAL at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


class IntentWal:
    """Read-only view of one historical run's intent WAL."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def read_tail(self) -> list[IntentEvent]:
        """Parse every complete event in seq order. See the module read contract.

        Splits at the byte level so a torn trailing line (possibly mid-UTF-8)
        is dropped cleanly rather than raising a decode error.
        """
        if not self._path.exists():
            return []
        raw = self._path.read_bytes()
        return self._parse_tail_bytes(raw)

    def read_tail_with_hash(self) -> tuple[list[IntentEvent], str | None]:
        """Parse WAL events and hash the same byte snapshot."""

        if not self._path.exists():
            return [], None
        raw = self._path.read_bytes()
        return self._parse_tail_bytes(raw), hashlib.sha256(raw).hexdigest()

    def _parse_tail_bytes(self, raw: bytes) -> list[IntentEvent]:
        if not raw:
            return []
        ends_with_newline = raw.endswith(b"\n")
        byte_lines = raw.split(b"\n")
        if byte_lines and byte_lines[-1] == b"":
            byte_lines.pop()  # the empty tail produced by a final newline

        events: list[IntentEvent] = []
        last_seq = 0
        n = len(byte_lines)
        for idx, bline in enumerate(byte_lines):
            if idx == n - 1 and not ends_with_newline:
                break  # tolerated: single trailing un-fsynced partial line
            try:
                event = IntentEvent.model_validate_json(bline)
            except (ValidationError, ValueError) as exc:
                raise IntentWalCorruptError(self._path, f"unparseable line {idx + 1}: {exc}") from exc
            if event.seq <= last_seq:
                raise IntentWalCorruptError(
                    self._path,
                    f"non-monotonic seq at line {idx + 1}: {event.seq} after {last_seq}",
                )
            last_seq = event.seq
            events.append(event)
        return events


__all__ = ["IntentWal", "IntentWalCorruptError"]
