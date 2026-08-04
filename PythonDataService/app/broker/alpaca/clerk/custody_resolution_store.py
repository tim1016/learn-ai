"""Restart-safe idempotency receipts for account custody resolution.

The order journal remains the custody/audit authority. This small sidecar only
answers the command question that the append-only journal cannot answer by
itself: whether an HTTP retry with the same idempotency key may execute again.
An ``in_flight`` record is persisted before the first mutation; after restart it
is deliberately treated as outcome-unknown rather than replayed blindly.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.broker.alpaca.clerk.diagnosis import CustodyResolutionReceipt

CUSTODY_RESOLUTION_RECEIPTS_FILENAME = "custody_resolution_receipts.json"


class CustodyResolutionOutcomeUnknownError(Exception):
    """A prior request started but has no durable terminal receipt."""

    def __init__(self) -> None:
        super().__init__("This custody-resolution request has an unknown prior outcome.")
        self.detail = (
            "Inspect the Clerk journal before issuing a new idempotency key; the prior "
            "request may have applied one or more custody mutations."
        )


class CustodyResolutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["in_flight", "succeeded", "failed"]
    operator: str
    reason: str
    receipt: CustodyResolutionReceipt | None = None
    error_detail: str | None = None


class CustodyResolutionStore:
    """Atomic file-backed records keyed by the request idempotency key."""

    def __init__(self, account_dir: Path) -> None:
        self._path = account_dir / CUSTODY_RESOLUTION_RECEIPTS_FILENAME
        self._loaded = False
        self._records: dict[str, CustodyResolutionRecord] = {}

    async def replay(self, key: str) -> CustodyResolutionReceipt | None:
        """Return a completed replay, ``None`` for a new key, or fail closed."""
        await asyncio.to_thread(self._load)
        existing = self._records.get(key)
        if existing is None:
            return None
        if existing.state == "succeeded" and existing.receipt is not None:
            return existing.receipt
        raise CustodyResolutionOutcomeUnknownError()

    async def reserve(self, *, key: str, operator: str, reason: str) -> None:
        """Persist the key before the first mutation."""
        if await self.replay(key) is not None:
            raise RuntimeError("cannot reserve a completed custody-resolution key")
        self._records[key] = CustodyResolutionRecord(
            state="in_flight", operator=operator, reason=reason
        )
        await asyncio.to_thread(self._persist)

    async def complete(self, key: str, receipt: CustodyResolutionReceipt) -> None:
        record = self._records[key]
        self._records[key] = record.model_copy(
            update={"state": "succeeded", "receipt": receipt, "error_detail": None}
        )
        await asyncio.to_thread(self._persist)

    async def fail(self, key: str, error_detail: str) -> None:
        record = self._records.get(key)
        if record is None:
            return
        self._records[key] = record.model_copy(
            update={"state": "failed", "error_detail": error_detail}
        )
        await asyncio.to_thread(self._persist)

    def _load(self) -> None:
        if self._loaded:
            return
        if not self._path.is_file():
            self._loaded = True
            return
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        records = {
            key: CustodyResolutionRecord.model_validate(value)
            for key, value in payload.items()
        }
        self._records = records
        self._loaded = True

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: record.model_dump(mode="json")
            for key, record in sorted(self._records.items())
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=self._path.parent, text=True
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            directory_descriptor = os.open(
                self._path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
