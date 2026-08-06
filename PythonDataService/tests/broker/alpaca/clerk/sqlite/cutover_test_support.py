"""Shared deterministic fixtures for SQLite cutover tests."""

from __future__ import annotations

from pathlib import Path

from app.engine.live.bot_lifecycle_state import (
    BotDutyOutcome,
    BotLifecyclePhase,
    BotLifecycleStateRecord,
)
from app.engine.live.desired_state import DesiredState, DesiredStateRecord
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    alpaca_v1_action_plan,
)

PLAN_MS = 1_800_000_000_000


class CutoverTestClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def write_stopped_runner_bot(
    runner_root: Path,
    *,
    strategy_instance_id: str = "spy",
    broker: str = "alpaca",
    desired_state: DesiredState = DesiredState.STOPPED,
    legacy_binding: bool = False,
) -> Path:
    instance_dir = runner_root / "live_state" / strategy_instance_id
    instance_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"run-{strategy_instance_id}"
    binding = BrokerBotBinding(
        strategy_instance_id=strategy_instance_id,
        broker=broker,
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id=run_id,
        created_at_ms=PLAN_MS - 10_000,
    )
    if legacy_binding:
        (instance_dir / "broker_binding.json").write_text(
            binding.model_dump_json(), encoding="utf-8"
        )
    else:
        BotBindingRepository(
            runner_root,
            instance_dir_for=lambda sid: runner_root / "live_state" / sid,
        ).record_launch(binding, launch_reason="deploy")
    (instance_dir / "desired_state.json").write_text(
        DesiredStateRecord(
            desired_state=desired_state,
            updated_at_ms=PLAN_MS - 5_000,
            updated_by="operator",
            reason="cutover stop",
        ).model_dump_json(),
        encoding="utf-8",
    )
    (instance_dir / "lifecycle_state.json").write_text(
        BotLifecycleStateRecord(
            phase=BotLifecyclePhase.OFF_DUTY,
            active_run_id=None,
            last_transition_at_ms=PLAN_MS - 4_000,
            updated_by="operator",
            duty_outcome=BotDutyOutcome(
                kind="STOPPED",
                reason_code="STOPPED_FLAT",
                recorded_at_ms=PLAN_MS - 4_000,
                run_id=run_id,
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    return instance_dir
