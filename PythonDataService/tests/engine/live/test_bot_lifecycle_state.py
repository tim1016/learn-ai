from __future__ import annotations

from pathlib import Path

from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)


def test_lifecycle_state_repo_round_trips_roster_and_phase(tmp_path: Path) -> None:
    path = stable_bot_lifecycle_state_path(tmp_path, "paper-ema")
    repo = BotLifecycleStateRepo(path)

    off_roster = repo.set_roster(False, now_ms=100, updated_by="operator")
    on_duty = repo.set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=200,
        updated_by="roll_call",
        active_run_id="run-1",
        reason="roll_call_start",
    )

    assert off_roster.version == 1
    assert on_duty.version == 2
    assert on_duty.phase is BotLifecyclePhase.ON_DUTY
    assert on_duty.on_roster is False
    assert on_duty.active_run_id == "run-1"
    assert repo.read() == on_duty


def test_lifecycle_state_repo_clears_active_run_on_off_duty_and_retire(tmp_path: Path) -> None:
    repo = BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, "paper-vwap"))
    repo.set_phase(
        BotLifecyclePhase.ON_DUTY,
        now_ms=100,
        updated_by="roll_call",
        active_run_id="run-1",
    )

    off_duty = repo.set_phase(
        BotLifecyclePhase.OFF_DUTY,
        now_ms=200,
        updated_by="clean_exit",
        reason="clean_exit_receipt",
    )
    retired = repo.retire(
        now_ms=300,
        updated_by="operator",
        reason="machinery_replaced",
        replacement_strategy_instance_id="paper-vwap-v2",
    )

    assert off_duty.phase is BotLifecyclePhase.OFF_DUTY
    assert off_duty.active_run_id is None
    assert retired.phase is BotLifecyclePhase.RETIRED
    assert retired.on_roster is False
    assert retired.active_run_id is None
    assert retired.retired_at_ms == 300
    assert retired.replacement_strategy_instance_id == "paper-vwap-v2"
