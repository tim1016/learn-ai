from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    BotRollCallOfferRecord,
    BotRollCallOfferRepo,
    stable_bot_lifecycle_state_path,
    stable_bot_roll_call_offers_path,
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


def test_lifecycle_state_repo_reopen_for_deploy_clears_retirement(tmp_path: Path) -> None:
    repo = BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, "paper-vwap"))
    repo.retire(
        now_ms=300,
        updated_by="operator",
        reason="machinery_replaced",
        replacement_strategy_instance_id="paper-vwap-v2",
    )

    reopened = repo.reopen_for_deploy(
        now_ms=400,
        updated_by="system",
        reason="deploy.replacement",
    )

    assert reopened.version == 2
    assert reopened.phase is BotLifecyclePhase.OFF_DUTY
    assert reopened.on_roster is True
    assert reopened.active_run_id is None
    assert reopened.reason == "deploy.replacement"
    assert reopened.retired_at_ms is None
    assert reopened.retired_reason is None
    assert reopened.replacement_strategy_instance_id is None
    assert repo.read() == reopened


def test_roll_call_offer_repo_round_trips_active_and_consumed_offers(tmp_path: Path) -> None:
    repo = BotRollCallOfferRepo(stable_bot_roll_call_offers_path(tmp_path, "paper-roll"))
    offer = BotRollCallOfferRecord(
        offer_id="offer-1",
        strategy_instance_id="paper-roll",
        run_id="run-1",
        session_date="2026-07-08",
        issued_at_ms=100,
        expires_at_ms=200,
        evidence_snapshot={"readiness_verdict": "READY"},
    )

    repo.append(offer)

    assert repo.active_offer(now_ms=150) == offer
    assert repo.active_offer(now_ms=200) is None
    consumed = repo.consume("offer-1")

    assert consumed is not None
    assert consumed.status == "consumed"
    assert repo.active_offer(now_ms=150) is None


def test_lifecycle_sidecar_paths_reject_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    live_state = tmp_path / "live_state"
    live_state.mkdir()
    (live_state / "paper-escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        stable_bot_lifecycle_state_path(tmp_path, "paper-escape")
    with pytest.raises(ValueError):
        stable_bot_roll_call_offers_path(tmp_path, "paper-escape")
