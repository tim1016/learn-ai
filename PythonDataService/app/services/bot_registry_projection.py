"""Read-only projections for the in-process bot registry."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.engine.live.bot_lifecycle_state import BotLifecyclePhase, BotLifecycleStateRecord
from app.engine.live.desired_state import DesiredState
from app.schemas.broker_bots import BotDutyOutcomeView, BotProcessFact, BotStatusView
from app.services.bot_binding_repository import BrokerBotBinding
from app.services.bot_dry_run import DryRunActivity, DryRunActivityJournal
from app.services.bot_runtime import ManagedBot


def project_process_fact(
    binding: BrokerBotBinding,
    lifecycle: BotLifecycleStateRecord | None,
    managed: ManagedBot | None,
    *,
    registry_generation: str,
    observed_at_ms: int,
) -> BotProcessFact:
    """Project process-owner evidence without inferring broker custody."""
    process_identity: str | None = None
    state: Literal["STARTING", "RUNNING", "STOPPING", "EXITED", "UNKNOWN"] = "UNKNOWN"
    if managed is not None and managed.binding.run_id == binding.run_id:
        process_identity = f"in-process-task:{binding.run_id}"
        lifecycle_matches = (
            lifecycle is not None
            and lifecycle.phase is BotLifecyclePhase.ON_DUTY
            and lifecycle.active_run_id == binding.run_id
        )
        if lifecycle_matches and not managed.task.done():
            state = "STOPPING" if managed.stop_reason_code is not None else "RUNNING"
    elif (
        lifecycle is not None
        and lifecycle.phase is BotLifecyclePhase.OFF_DUTY
        and lifecycle.active_run_id is None
        and lifecycle.duty_outcome is not None
        and lifecycle.duty_outcome.run_id == binding.run_id
    ):
        state = "EXITED"

    return BotProcessFact(
        strategy_instance_id=binding.strategy_instance_id,
        run_id=binding.run_id,
        process_identity=process_identity,
        state=state,
        registry_generation=registry_generation,
        observed_at_ms=observed_at_ms,
    )


def project_bot_status(
    binding: BrokerBotBinding,
    lifecycle: BotLifecycleStateRecord | None,
    desired: DesiredState,
    *,
    running: bool,
    carryover_account_policy_enabled: bool,
    checkpoint_exposure: str | None,
    checkpoint_matches: bool,
) -> BotStatusView:
    """Compose one typed roster row from durable artifacts and task liveness."""
    duty_outcome = None
    if lifecycle is not None and lifecycle.duty_outcome is not None:
        duty_outcome = BotDutyOutcomeView(
            kind=lifecycle.duty_outcome.kind,
            reason_code=lifecycle.duty_outcome.reason_code,
            recorded_at_ms=lifecycle.duty_outcome.recorded_at_ms,
            run_id=lifecycle.duty_outcome.run_id,
        )
    return BotStatusView(
        strategy_instance_id=binding.strategy_instance_id,
        strategy_key=binding.strategy_key,
        broker=binding.broker,
        symbol=binding.symbol,
        mode=binding.mode,
        quantity=binding.quantity,
        carryover_policy=binding.carryover_policy,
        carryover_account_policy_enabled=carryover_account_policy_enabled,
        carryover_checkpoint_exposure=checkpoint_exposure,
        carryover_checkpoint_config_matches=checkpoint_matches,
        running=running,
        phase=(lifecycle.phase.value if lifecycle is not None else "OFF_DUTY"),
        desired_state=desired.value,
        active_run_id=(lifecycle.active_run_id if lifecycle is not None else None),
        duty_outcome=duty_outcome,
        binding_created_at_ms=binding.created_at_ms,
        last_transition_at_ms=(
            lifecycle.last_transition_at_ms if lifecycle is not None else None
        ),
    )


def read_dry_run_activity(
    binding: BrokerBotBinding,
    instance_dir: Path,
    *,
    limit: int,
) -> list[DryRunActivity]:
    """Read a bounded, explicitly simulated activity suffix for a dry run."""
    if binding.mode != "dry_run":
        return []
    return DryRunActivityJournal(instance_dir).tail(limit)
