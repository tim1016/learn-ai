"""Durable Alpaca STOP custody evidence.

The bot runner owns process liveness; this module owns the lifecycle proof
that connects a stopped run to Clerk-authored account truth. It deliberately
depends on narrow protocols so neither the runner binding nor the Clerk gains
a circular dependency.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from app.broker.alpaca.clerk.models import InstanceCustodyProof
from app.engine.live.run_status import _atomic_write_json
from app.schemas.action_plan import ActionPlan

logger = logging.getLogger(__name__)

StopCustodyOutcome = Literal[
    "STOPPED_FLAT",
    "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
    "STOP_REQUIRES_FLATTEN",
    "STOPPED_CUSTODY_UNPROVABLE",
]


class CarryoverBinding(Protocol):
    strategy_instance_id: str
    broker: str
    symbol: str
    use_rth: bool
    mode: Literal["log_only", "dry_run", "trade"]
    quantity: int
    carryover_policy: Literal["FORBID", "ALLOW"]
    evidence_override: object | None
    action_plan: ActionPlan
    strategy_params: dict[str, Any] | None
    run_id: str

    def model_dump(self, **kwargs) -> dict: ...


class CustodyClerk(Protocol):
    async def prove_instance_custody(self, strategy_instance_id: str) -> InstanceCustodyProof: ...


class CarryoverStopCheckpoint(BaseModel):
    """Historical STOP custody result; never permission to restart trading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    strategy_instance_id: str
    account_id: str
    stopped_run_id: str
    configuration_hash: str
    exposure: dict[str, float]
    approved: bool
    outcome: StopCustodyOutcome
    recorded_at_ms: int


def configuration_hash(binding: CarryoverBinding) -> str:
    """Hash immutable deployment semantics, excluding per-run identity/time."""
    payload = immutable_configuration_payload(binding)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def immutable_configuration_payload(binding: CarryoverBinding) -> dict:
    """Return the exact immutable configuration payload hashed for carryover."""
    return binding.model_dump(
        mode="json",
        exclude={"run_id", "created_at_ms", "exit_terms"},
        # Optional configuration fields added in later schema versions must
        # not invalidate hashes for older bindings when they remain absent.
        # A populated evidence override is still included and hash-bound.
        exclude_none=True,
    )


def read_checkpoint(path: Path) -> CarryoverStopCheckpoint | None:
    if not path.is_file():
        return None
    try:
        return CarryoverStopCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        logger.warning(
            "Ignoring corrupt carryover checkpoint",
            extra={
                "action": "corrupt_carryover_checkpoint",
                "path": str(path),
                "error": str(exc),
            },
        )
        return None


async def prove_stop_outcome(
    binding: CarryoverBinding,
    *,
    clerk: CustodyClerk,
    checkpoint_path: Path,
    now_ms: Callable[[], int],
) -> StopCustodyOutcome:
    """Obtain fresh custody and persist the STOP checkpoint.

    The run is already ``STOPPED`` when this runs, so the reconciliation
    inside ``prove_instance_custody`` cancels its working entries (#2362) --
    the same step every sweep re-drives. A cancel another owner's claim
    blocked right now leaves the order in the proof's working set, so the
    outcome is ``STOPPED_CUSTODY_UNPROVABLE`` without Stop racing the sweep
    for the claim (#2361).
    """
    try:
        proof = await clerk.prove_instance_custody(binding.strategy_instance_id)
    except Exception:
        logger.exception(
            "Alpaca STOP custody proof failed",
            extra={
                "action": "stop_custody_proof_failed",
                "strategy_instance_id": binding.strategy_instance_id,
            },
        )
        return "STOPPED_CUSTODY_UNPROVABLE"

    if (
        proof.freeze.active
        or proof.reconciliation_verdict != "clean"
        or proof.working_order_refs
        or proof.unresolved_intent_refs
    ):
        outcome: StopCustodyOutcome = "STOPPED_CUSTODY_UNPROVABLE"
        approved = False
    elif not proof.exposure:
        outcome = "STOPPED_FLAT"
        approved = False
    elif binding.carryover_policy == "ALLOW":
        outcome = "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE"
        approved = True
    else:
        outcome = "STOP_REQUIRES_FLATTEN"
        approved = False
    checkpoint = CarryoverStopCheckpoint(
        strategy_instance_id=binding.strategy_instance_id,
        account_id=proof.account_id,
        stopped_run_id=binding.run_id,
        configuration_hash=configuration_hash(binding),
        exposure=proof.exposure,
        approved=approved,
        outcome=outcome,
        recorded_at_ms=now_ms(),
    )
    _atomic_write_json(checkpoint_path, checkpoint.model_dump())
    return outcome
