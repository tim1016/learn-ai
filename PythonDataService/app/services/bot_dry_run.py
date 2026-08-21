"""Durable simulated activity for zero-broker-write bot runs."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.jsonl_wal import JsonlWal

DRY_RUN_ACTIVITY_FILENAME = "dry_run_activity.jsonl"
MAX_DRY_RUN_TAIL = 500


class DryRunActivity(BaseModel):
    """One simulated fill with its selected synthetic custody authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    strategy_instance_id: str
    run_id: str
    authority_account_id: str
    authority_kind: Literal["synthetic"]
    recorded_at_ms: int = Field(ge=0)
    bar_ref: str
    intent: Literal["ENTER", "EXIT"]
    order_ref: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0)
    fill_price: float = Field(gt=0)
    simulated: Literal[True] = True


def _corrupt_error(path: Path, detail: str) -> RuntimeError:
    return RuntimeError(f"Dry-run activity journal corrupt at {path}: {detail}")


class DryRunActivityJournal:
    """Fsync'd append-only WAL kept beside one immutable strategy instance."""

    def __init__(self, instance_dir: Path) -> None:
        resolved = instance_dir.resolve()
        self._wal: JsonlWal[DryRunActivity] = JsonlWal(
            resolved / DRY_RUN_ACTIVITY_FILENAME,
            record_model=DryRunActivity,
            corrupt_error=_corrupt_error,
            seq_of=lambda row: row.seq,
            label="dry_run_activity",
            trusted_root=resolved,
        )
        self._lock = threading.Lock()

    def append(self, activity: DryRunActivity) -> None:
        with self._lock:
            self._wal.append(activity)

    def next_seq(self) -> int:
        with self._lock:
            return self._wal.allocate_seq()

    def tail(self, limit: int) -> list[DryRunActivity]:
        if limit <= 0:
            raise ValueError("dry-run activity tail limit must be positive")
        return self._wal.read_tail(limit=min(limit, MAX_DRY_RUN_TAIL))
