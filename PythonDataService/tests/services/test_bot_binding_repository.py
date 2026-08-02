"""Persistence contract for immutable strategy instances and append-only runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.bot_binding_repository import (
    BotBindingRepository,
    BrokerBotBinding,
    RunIdentityConflictError,
    StrategyInstanceConfigurationConflictError,
    StrategyInstanceRecord,
    alpaca_v1_action_plan,
)

_SID = "alpaca-spy-ema-01"


def _repository(tmp_path: Path) -> BotBindingRepository:
    return BotBindingRepository(
        tmp_path,
        instance_dir_for=lambda strategy_instance_id: tmp_path / "live_state" / strategy_instance_id,
    )


def _binding(
    *,
    run_id: str = "run-001",
    created_at_ms: int = 1_000,
    symbol: str = "SPY",
) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol=symbol,
        action_plan=alpaca_v1_action_plan(symbol),
        run_id=run_id,
        created_at_ms=created_at_ms,
    )


def test_launch_keeps_instance_immutable_and_appends_run_records(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _binding()
    second = _binding(run_id="run-002", created_at_ms=2_000)

    repository.record_launch(first, launch_reason="deploy")
    instance_path = tmp_path / "live_state" / _SID / "strategy_instance.json"
    original_instance_bytes = instance_path.read_bytes()

    repository.record_launch(second, launch_reason="resume")

    assert instance_path.read_bytes() == original_instance_bytes
    run_paths = sorted((tmp_path / "live_state" / _SID / "runs").glob("*.json"))
    assert [path.stem for path in run_paths] == ["run-001", "run-002"]
    assert json.loads(run_paths[0].read_text())["launch_reason"] == "deploy"
    assert json.loads(run_paths[1].read_text())["launch_reason"] == "resume"
    assert repository.read(_SID) == second
    assert [run.run_id for run in repository.list_runs(_SID)] == [
        "run-002",
        "run-001",
    ]


def test_changed_configuration_cannot_reuse_strategy_instance_identity(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = _binding()
    repository.record_launch(first, launch_reason="deploy")

    with pytest.raises(StrategyInstanceConfigurationConflictError):
        repository.record_launch(
            _binding(run_id="run-002", created_at_ms=2_000, symbol="QQQ"),
            launch_reason="resume",
        )

    assert repository.read(_SID) == first
    assert not (tmp_path / "live_state" / _SID / "runs" / "run-002.json").exists()


def test_duplicate_run_is_idempotent_but_changed_run_payload_conflicts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.record_launch(binding, launch_reason="deploy")
    original_run_bytes = (tmp_path / "live_state" / _SID / "runs" / "run-001.json").read_bytes()

    repository.record_launch(binding, launch_reason="deploy")
    assert (tmp_path / "live_state" / _SID / "runs" / "run-001.json").read_bytes() == original_run_bytes

    with pytest.raises(RunIdentityConflictError):
        repository.record_launch(
            binding.model_copy(update={"created_at_ms": 2_000}),
            launch_reason="deploy",
        )


def test_legacy_binding_is_lifted_without_rewrite_then_migrated_on_resume(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    instance_dir = tmp_path / "live_state" / _SID
    instance_dir.mkdir(parents=True)
    legacy = _binding().model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy.pop("action_plan")
    legacy_bytes = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
    legacy_path = instance_dir / "broker_binding.json"
    legacy_path.write_text(legacy_bytes, encoding="utf-8")

    assert repository.read(_SID) == _binding()
    assert [run.run_id for run in repository.list_runs(_SID)] == ["run-001"]
    assert legacy_path.read_text(encoding="utf-8") == legacy_bytes
    assert not (instance_dir / "strategy_instance.json").exists()

    # A crash may leave only the normalized instance record. Migration must
    # replay safely and retain the legacy run on the next Resume.
    instance_path = instance_dir / "strategy_instance.json"
    instance_path.write_text(
        StrategyInstanceRecord.from_binding(_binding()).model_dump_json(),
        encoding="utf-8",
    )
    resumed = _binding(run_id="run-002", created_at_ms=2_000)
    repository.record_launch(resumed, launch_reason="resume")

    assert legacy_path.read_text(encoding="utf-8") == legacy_bytes
    assert sorted(path.stem for path in (instance_dir / "runs").glob("*.json")) == [
        "run-001",
        "run-002",
    ]
    assert json.loads((instance_dir / "runs" / "run-001.json").read_text())["launch_reason"] == "legacy"
    assert repository.read(_SID) == resumed
