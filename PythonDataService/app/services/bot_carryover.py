"""Durable Alpaca STOP checkpoint and fresh Resume-custody policy.

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
from typing import Literal, Protocol

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
    mode: Literal["log_only", "trade"]
    quantity: int
    carryover_policy: Literal["FORBID", "ALLOW"]
    action_plan: ActionPlan
    run_id: str

    def model_dump(self, **kwargs) -> dict: ...


class CustodyClerk(Protocol):
    async def cancel_working_entries_for_instance(self, strategy_instance_id: str) -> tuple: ...

    async def prove_instance_custody(self, strategy_instance_id: str) -> InstanceCustodyProof: ...


class CarryoverResumeRefusedError(Exception):
    """Fresh Resume proof failed without changing runtime or broker state."""

    def __init__(self, message: str, *, detail: str) -> None:
        super().__init__(message)
        self.detail = detail


class CarryoverStopCheckpoint(BaseModel):
    """Durable STOP custody result used as the Resume comparison baseline."""

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
    payload = binding.model_dump(
        mode="json",
        exclude={"run_id", "created_at_ms"},
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def checkpoint_status(
    binding: CarryoverBinding,
    path: Path,
) -> tuple[dict[str, float], bool]:
    checkpoint = read_checkpoint(path)
    matches = (
        checkpoint is not None
        and checkpoint.approved
        and checkpoint.stopped_run_id == binding.run_id
        and checkpoint.configuration_hash == configuration_hash(binding)
    )
    return (
        checkpoint.exposure if checkpoint is not None and checkpoint.approved else {},
        matches,
    )


async def prove_stop_outcome(
    binding: CarryoverBinding,
    *,
    clerk: CustodyClerk,
    checkpoint_path: Path,
    now_ms: Callable[[], int],
) -> StopCustodyOutcome:
    """Cancel entries, obtain fresh custody, and persist the STOP checkpoint."""
    try:
        await clerk.cancel_working_entries_for_instance(binding.strategy_instance_id)
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


async def require_resume_custody(
    binding: CarryoverBinding,
    *,
    clerk: CustodyClerk | None,
    checkpoint_path: Path,
    desired_state: str,
    phase: str,
) -> None:
    """Admit Resume only after a fresh exact Clerk proof."""
    if phase == "RETIRED":
        raise CarryoverResumeRefusedError(
            f"Bot '{binding.strategy_instance_id}' is retired and cannot resume.",
            detail="Deploy a replacement strategy instance with explicit lineage.",
        )
    if desired_state != "STOPPED":
        raise CarryoverResumeRefusedError(
            f"Bot '{binding.strategy_instance_id}' has no durable STOP intent.",
            detail=(
                "Resume is limited to an explicitly stopped instance; resolve "
                "the prior run outcome before starting a new run."
            ),
        )
    if binding.broker != "alpaca" or binding.mode != "trade":
        return
    if clerk is None:
        raise CarryoverResumeRefusedError(
            "Resume is refused because the Alpaca Clerk is unavailable.",
            detail="Restore Clerk custody and obtain a fresh account proof.",
        )

    proof = await clerk.prove_instance_custody(binding.strategy_instance_id)
    if proof.freeze.active or proof.reconciliation_verdict != "clean":
        raise CarryoverResumeRefusedError(
            "Resume is refused by the account custody proof.",
            detail=proof.freeze.explanation or f"Reconciliation verdict is {proof.reconciliation_verdict}.",
        )
    if proof.working_order_refs or proof.unresolved_intent_refs:
        raise CarryoverResumeRefusedError(
            "Resume is refused while this instance has unresolved order work.",
            detail=(f"working={len(proof.working_order_refs)}; unresolved={len(proof.unresolved_intent_refs)}"),
        )
    if not proof.exposure:
        return
    if binding.carryover_policy != "ALLOW":
        raise CarryoverResumeRefusedError(
            "Resume is refused because stopped exposure was not approved for carryover.",
            detail="Flatten the exact Clerk-attributed exposure before resuming.",
        )
    checkpoint = read_checkpoint(checkpoint_path)
    if checkpoint is None or not checkpoint.approved:
        raise CarryoverResumeRefusedError(
            "Resume is refused because no approved carryover checkpoint exists.",
            detail="STOP must persist an approved checkpoint before exposure can carry.",
        )
    comparisons_match = (
        checkpoint.account_id == proof.account_id
        and checkpoint.stopped_run_id == binding.run_id
        and checkpoint.configuration_hash == configuration_hash(binding)
        and checkpoint.exposure == proof.exposure
    )
    if not comparisons_match:
        raise CarryoverResumeRefusedError(
            "Resume is refused because the carryover custody proof changed.",
            detail=(
                "Account, run, immutable configuration, or exact attributed quantity differs from the STOP checkpoint."
            ),
        )
