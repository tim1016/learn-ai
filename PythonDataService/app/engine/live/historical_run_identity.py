"""Strict read-only access to retained IBKR run-ledger identity evidence.

The legacy run ledger is never constructed or written here.  These readers
exist only so retained IBKR evidence can continue to deny identity collisions
and support historical read surfaces after the legacy parser/runtime retired.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class _HistoricalRunIdentity(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    strategy_instance_id: StrictStr = Field(min_length=1)

    @field_validator("strategy_instance_id")
    @classmethod
    def _nonblank_strategy_instance_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("strategy_instance_id must not be blank")
        return value


def read_historical_run_ledger_object(path: Path) -> dict[str, object]:
    """Return one regular-file JSON object or fail closed."""

    if path.is_symlink() or not path.is_file():
        raise OSError("historical run ledger must be a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("historical run ledger must contain a JSON object")
    return payload


def read_historical_strategy_instance_id(path: Path) -> str:
    """Return the one strict identity field needed by collision guards."""

    payload = read_historical_run_ledger_object(path)
    return _HistoricalRunIdentity.model_validate(payload).strategy_instance_id


__all__ = [
    "read_historical_run_ledger_object",
    "read_historical_strategy_instance_id",
]
