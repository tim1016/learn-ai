"""Durable broker-tagged bot binding codec and repository.

The in-container runner owns task liveness, while this module owns the
versioned binding artifact used to restore immutable deployment configuration.
Reads may lift the Alpaca v1 shape in memory, but never rewrite historical
artifacts, which preserves their byte-level audit evidence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.engine.live.run_status import _atomic_write_json
from app.schemas.action_plan import ActionPlan, CloseLegExit, StockEntryLeg, StockInstrument

logger = logging.getLogger(__name__)

BINDING_FILENAME = "broker_binding.json"


def alpaca_v1_action_plan(symbol: str) -> ActionPlan:
    """Build the v1 stock plan from the existing deploy controls."""
    return ActionPlan(
        on_enter=[
            StockEntryLeg(
                leg_id="primary",
                instrument=StockInstrument(kind="stock", underlying=symbol),
                position="long",
                qty_ratio=1,
            )
        ],
        on_exit=[CloseLegExit(kind="close_leg", entry_leg_id="primary")],
    )


class BrokerBotBinding(BaseModel):
    """Durable broker-tagged run binding (P9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[2] = 2
    strategy_instance_id: str
    # Version-2 Alpaca bindings predated this field and all used the only
    # available deployment strategy. The default lifts those durable records.
    strategy_key: str = "deployment_validation"
    broker: str
    symbol: str
    use_rth: bool = True
    mode: Literal["log_only", "trade"] = "log_only"
    quantity: int = 1
    carryover_policy: Literal["FORBID", "ALLOW"] = "FORBID"
    action_plan: ActionPlan
    run_id: str
    created_at_ms: int


class BotBindingRepository:
    """Read and write immutable deployment bindings beneath ``live_state``."""

    def __init__(
        self,
        artifacts_root: Path,
        *,
        instance_dir_for: Callable[[str], Path],
    ) -> None:
        self._live_state_root = Path(artifacts_root) / "live_state"
        self._instance_dir_for = instance_dir_for

    def write(self, binding: BrokerBotBinding) -> None:
        """Persist the current v2 binding atomically."""
        instance_dir = self._instance_dir_for(binding.strategy_instance_id)
        instance_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(instance_dir / BINDING_FILENAME, binding.model_dump())

    def read(self, strategy_instance_id: str) -> BrokerBotBinding | None:
        """Return one binding, lifting v1 Alpaca records in memory only."""
        path = self._instance_dir_for(strategy_instance_id) / BINDING_FILENAME
        if not path.is_file():
            return None
        return self.decode(path.read_text(encoding="utf-8"))

    def list_for_broker(self, broker: str) -> list[BrokerBotBinding]:
        """Return all valid bindings for a broker without hiding corrupt rows."""
        if not self._live_state_root.is_dir():
            return []

        bindings: list[BrokerBotBinding] = []
        for child in sorted(self._live_state_root.iterdir()):
            path = child / BINDING_FILENAME
            if not path.is_file():
                continue
            try:
                binding = self.decode(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError, ValueError) as exc:
                logger.warning(
                    "Skipping corrupt broker binding",
                    extra={
                        "action": "corrupt_broker_binding_skipped",
                        "path": str(path),
                        "error": str(exc),
                    },
                )
                continue
            if binding.broker == broker:
                bindings.append(binding)
        return bindings

    @staticmethod
    def decode(raw: str) -> BrokerBotBinding:
        """Validate v2 bindings and lift the constrained Alpaca v1 shape."""
        payload = json.loads(raw)
        if (
            payload.get("schema_version") == 1
            and payload.get("broker") == "alpaca"
            and isinstance(payload.get("symbol"), str)
            and "action_plan" not in payload
        ):
            payload = {
                **payload,
                "schema_version": 2,
                "action_plan": alpaca_v1_action_plan(payload["symbol"]).model_dump(),
            }
        return BrokerBotBinding.model_validate(payload)
