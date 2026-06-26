"""Host-runner-owned raw broker callback WAL.

ADR 0014's 2026-06-25 amendment makes this WAL the first durable
capture point for broker callbacks. It deliberately mirrors the
``IntentWal`` / ``BrokerActivityWal`` JSONL contract: append-only,
monotonic per-run ``seq``, fsync-before-return, and one tolerated
trailing partial line on read.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.broker.ibkr.models import IbkrOrderEvent, OrderEventType
from app.engine.live.live_state_sidecar import _fsync_parent_dir


class BrokerCallbackRecord(BaseModel):
    """One raw broker callback captured by the host runner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    callback_type: OrderEventType
    observed_at_ms: int = Field(gt=0)
    idempotency_key: str
    event: IbkrOrderEvent


class BrokerCallbackWalCorruptError(RuntimeError):
    """Raised on any read malformation other than one trailing partial line."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"broker-callback WAL at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


def broker_callbacks_wal_path(run_dir: Path) -> Path:
    """Canonical per-run raw callback WAL path."""
    return run_dir / "broker_callbacks.jsonl"


class BrokerCallbackWal:
    """Append-only writer/reader for raw IBKR order callbacks."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def append_event(self, event: IbkrOrderEvent) -> BrokerCallbackRecord:
        seq = self._allocate_seq()
        record = BrokerCallbackRecord(
            seq=seq,
            callback_type=event.event_type,
            observed_at_ms=event.ts_ms,
            idempotency_key=broker_callback_idempotency_key(event),
            event=event,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(record.model_dump_json() + "\n")
            self._next_seq = seq + 1
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)
        return record

    def read_all(self) -> list[BrokerCallbackRecord]:
        if not self._path.exists():
            return []
        raw = self._path.read_bytes()
        if not raw:
            return []
        ends_with_newline = raw.endswith(b"\n")
        byte_lines = raw.split(b"\n")
        if byte_lines and byte_lines[-1] == b"":
            byte_lines.pop()

        records: list[BrokerCallbackRecord] = []
        last_seq = 0
        n = len(byte_lines)
        for idx, bline in enumerate(byte_lines):
            if idx == n - 1 and not ends_with_newline:
                break
            try:
                record = BrokerCallbackRecord.model_validate_json(bline)
            except (ValidationError, ValueError) as exc:
                raise BrokerCallbackWalCorruptError(
                    self._path, f"unparseable line {idx + 1}: {exc}"
                ) from exc
            if record.seq <= last_seq:
                raise BrokerCallbackWalCorruptError(
                    self._path,
                    f"non-monotonic seq at line {idx + 1}: "
                    f"{record.seq} after {last_seq}",
                )
            last_seq = record.seq
            records.append(record)
        return records

    def _allocate_seq(self) -> int:
        if self._next_seq is None:
            existing = self.read_all()
            self._next_seq = (existing[-1].seq + 1) if existing else 1
        return self._next_seq


def broker_callback_idempotency_key(event: IbkrOrderEvent) -> str:
    """Return the stable raw-callback dedupe key named by ADR 0014 §4."""
    parts = [
        event.event_type,
        event.exec_id or "",
        str(event.perm_id or ""),
        event.order_ref or "",
        event.status or "",
        str(event.exec_time_ms or ""),
        str(event.ts_ms),
    ]
    return "|".join(parts)


__all__ = [
    "BrokerCallbackRecord",
    "BrokerCallbackWal",
    "BrokerCallbackWalCorruptError",
    "broker_callback_idempotency_key",
    "broker_callbacks_wal_path",
]
