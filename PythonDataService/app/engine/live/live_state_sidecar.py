"""Order-idempotency sidecar — persists what the bot believes about
its in-flight orders, fills, positions, and bar cursor so a crash
between submit and acknowledgement cannot cause a double trade.

Grown vertically via TDD: each cycle adds one field or one
mechanic. See plan §16.4 Resolution 3 for the 12-field target
schema this module grows toward, and ``indicator_state.py`` for
the envelope+repo+atomic-write pattern this mirrors.
"""

from __future__ import annotations

from pathlib import Path

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LiveStateEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_instance_id: str
    run_id: str
    bot_order_namespace: str
    ib_client_id: int

    pending_intents: list[dict[str, Any]] = Field(default_factory=list)
    submitted_orders: dict[str, dict[str, Any]] = Field(default_factory=dict)
    known_perm_ids: list[int] = Field(default_factory=list)
    known_exec_ids: list[str] = Field(default_factory=list)

    expected_position_by_symbol: dict[str, int] = Field(default_factory=dict)
    last_processed_bar_ms: int = Field(gt=0)
    last_artifact_flush_ms: int = Field(gt=0)


class LiveStateSidecarRepo:
    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> LiveStateEnvelope | None:
        if not self._path.exists():
            return None
        return LiveStateEnvelope.model_validate_json(self._path.read_text(encoding="utf-8"))

    def write(self, envelope: LiveStateEnvelope) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(envelope.model_dump_json(), encoding="utf-8")
