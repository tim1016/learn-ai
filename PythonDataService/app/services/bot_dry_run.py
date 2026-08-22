"""Durable simulated activity for zero-broker-write bot runs."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
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

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_authority(cls, value: Any) -> Any:
        """Lift pre-authority rows without weakening current evidence checks."""
        if not isinstance(value, dict):
            return value
        lifted = dict(value)
        if "authority_account_id" not in lifted:
            strategy_instance_id = lifted.get("strategy_instance_id")
            if isinstance(strategy_instance_id, str):
                lifted["authority_account_id"] = synthetic_account_id_for_strategy(
                    strategy_instance_id
                )
        lifted.setdefault("authority_kind", "synthetic")
        return lifted

    @model_validator(mode="after")
    def _require_authority_for_instance(self) -> DryRunActivity:
        expected = synthetic_account_id_for_strategy(self.strategy_instance_id)
        if self.authority_account_id != expected:
            raise ValueError("Dry Run activity authority does not match strategy instance")
        return self


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
