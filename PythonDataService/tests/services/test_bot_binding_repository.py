"""Persistence contract for immutable strategy instances and append-only runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.engine.live.durable_append_log as durable_append_log
from app.schemas.bot_run_evidence import BotCrashDiagnostic
from app.schemas.broker_bots import AlpacaPaperEvidenceOverride
from app.services.bot_binding_repository import (
    BotBindingRepository,
    BotRunOutcomeRecord,
    BotRunRecord,
    BrokerBotBinding,
    CurrentRunBinding,
    RunIdentityConflictError,
    RunOutcomeConflictError,
    StrategyInstanceConfigurationConflictError,
    StrategyInstanceRecord,
    alpaca_v1_action_plan,
)
from app.services.bot_carryover import configuration_hash, immutable_configuration_payload

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
    evidence_override: AlpacaPaperEvidenceOverride | None = None,
    strategy_params: dict[str, object] | None = None,
) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol=symbol,
        evidence_override=evidence_override,
        action_plan=alpaca_v1_action_plan(symbol),
        strategy_params=strategy_params,
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
    assert repository.read_run(_SID, "run-002") is not None
    assert repository.read_run(_SID, "missing") is None
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


def test_strategy_params_round_trip_and_are_immutable_configuration(
    tmp_path: Path,
) -> None:
    """#1701: deploy-time parameters are part of create-once configuration.

    Resume replays the exact original parameter set (round-trip through
    ``record_launch``/``read``); changing a parameter for the same instance
    identity is refused the same way a changed symbol already is.
    """
    repository = _repository(tmp_path)
    first = _binding(strategy_params={"gap": 5.0, "rsi_min": 40.0})
    repository.record_launch(first, launch_reason="deploy")

    assert repository.read(_SID) == first
    assert repository.read(_SID).strategy_params == {"gap": 5.0, "rsi_min": 40.0}

    resumed = _binding(run_id="run-002", created_at_ms=2_000, strategy_params={"gap": 5.0, "rsi_min": 40.0})
    repository.record_launch(resumed, launch_reason="resume")
    assert repository.read(_SID) == resumed

    with pytest.raises(StrategyInstanceConfigurationConflictError):
        repository.record_launch(
            _binding(run_id="run-003", created_at_ms=3_000, strategy_params={"gap": 9.0, "rsi_min": 40.0}),
            launch_reason="resume",
        )
    assert repository.read(_SID) == resumed
    assert not (tmp_path / "live_state" / _SID / "runs" / "run-003.json").exists()


def test_evidence_override_is_durable_immutable_configuration(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    override = AlpacaPaperEvidenceOverride(
        acknowledgement="I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK",
        reason="Paper canary approved by the strategy owner.",
    )
    binding = _binding(evidence_override=override)

    repository.record_launch(binding, launch_reason="deploy")

    assert repository.read(_SID) == binding
    instance = json.loads((tmp_path / "live_state" / _SID / "strategy_instance.json").read_text())
    assert instance["evidence_override"] == override.model_dump()

    with pytest.raises(StrategyInstanceConfigurationConflictError):
        repository.record_launch(
            _binding(
                run_id="run-002",
                created_at_ms=2_000,
                evidence_override=override.model_copy(update={"reason": "A different human risk decision."}),
            ),
            launch_reason="resume",
        )


def test_absent_override_preserves_pre_override_configuration_hash_shape() -> None:
    assert "evidence_override" not in immutable_configuration_payload(_binding())


def test_absent_strategy_params_preserves_pre_feature_configuration_hash_shape() -> None:
    """#1701: a binding persisted before this feature existed must keep its
    original configuration_hash. `strategy_params` defaults to `None` (not
    `{}`) specifically so `exclude_none=True` strips it here — an empty-dict
    default would appear in the hash payload and invalidate every hash
    computed before this field existed, the same way ``evidence_override``
    is already required to behave."""
    assert "strategy_params" not in immutable_configuration_payload(_binding())
    assert configuration_hash(_binding()) == configuration_hash(_binding(strategy_params=None))


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


def test_terminal_outcome_is_create_once_and_run_scoped(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.record_launch(binding, launch_reason="deploy")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id=binding.run_id,
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=2_000,
    )

    repository.record_outcome(outcome)
    repository.record_outcome(outcome)
    repository.record_outcome(outcome.model_copy(update={"recorded_at_ms": 999}))

    assert repository.read_outcome(_SID, binding.run_id) == outcome
    with pytest.raises(RunOutcomeConflictError):
        repository.record_outcome(outcome.model_copy(update={"reason_code": "SERVICE_SHUTDOWN"}))


def test_crash_diagnostic_requires_a_crashed_terminal_outcome() -> None:
    with pytest.raises(ValueError, match="require a CRASHED terminal outcome"):
        BotRunOutcomeRecord(
            strategy_instance_id=_SID,
            run_id="run-001",
            kind="STOPPED",
            reason_code="OPERATOR_STOP",
            recorded_at_ms=2_000,
            crash_diagnostic=BotCrashDiagnostic(
                exception_type="TypeError",
                message="unexpected keyword argument 'start_ms'",
                source_file="app/services/bot_trade_strategy.py",
                source_line=86,
            ),
        )


def test_terminal_outcome_rejects_conflicting_crash_diagnostics(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.record_launch(binding, launch_reason="deploy")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id=binding.run_id,
        kind="CRASHED",
        reason_code="TypeError",
        recorded_at_ms=2_000,
        crash_diagnostic=BotCrashDiagnostic(
            exception_type="TypeError",
            message="unexpected keyword argument 'start_ms'",
            source_file="app/services/bot_trade_strategy.py",
            source_line=86,
        ),
    )
    repository.record_outcome(outcome)
    repository.record_outcome(outcome)

    assert outcome.crash_diagnostic is not None
    conflicting = outcome.model_copy(
        update={
            "crash_diagnostic": outcome.crash_diagnostic.model_copy(
                update={"source_line": 87}
            )
        }
    )
    with pytest.raises(RunOutcomeConflictError):
        repository.record_outcome(conflicting)


def test_terminal_outcome_recovers_after_interrupted_atomic_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.record_launch(binding, launch_reason="deploy")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id=binding.run_id,
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=2_000,
    )
    outcome_path = tmp_path / "live_state" / _SID / "run_outcomes" / f"{binding.run_id}.json"

    def fail_publication(*args: object, **kwargs: object) -> None:
        raise OSError("injected outcome publication failure")

    with monkeypatch.context() as publication_failure:
        publication_failure.setattr(durable_append_log.os, "link", fail_publication)
        with pytest.raises(OSError, match="injected outcome publication failure"):
            repository.record_outcome(outcome)

    assert not outcome_path.exists()
    assert not list(outcome_path.parent.glob(f".{outcome_path.name}.*.tmp"))

    repository.record_outcome(outcome)

    assert repository.read_outcome(_SID, binding.run_id) == outcome


def test_terminal_outcome_rejects_a_run_record_with_a_mismatched_run_id(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    repository.record_launch(binding, launch_reason="deploy")
    run_path = tmp_path / "live_state" / _SID / "runs" / f"{binding.run_id}.json"
    corrupt_run = json.loads(run_path.read_text(encoding="utf-8"))
    corrupt_run["run_id"] = "run-other"
    run_path.write_text(json.dumps(corrupt_run), encoding="utf-8")
    outcome = BotRunOutcomeRecord(
        strategy_instance_id=_SID,
        run_id=binding.run_id,
        kind="STOPPED",
        reason_code="OPERATOR_STOP",
        recorded_at_ms=2_000,
    )

    with pytest.raises(ValueError, match="terminal outcome belongs"):
        repository.record_outcome(outcome)

    assert not (tmp_path / "live_state" / _SID / "run_outcomes" / f"{binding.run_id}.json").exists()


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


def test_a_strategy_instance_record_predating_1701_still_reads_and_hash_validates(
    tmp_path: Path,
) -> None:
    """A ``strategy_instance.json`` written before #1701 added the field has
    no ``strategy_params`` key at all — not even an empty one. Its
    ``configuration_hash`` was computed over a payload that never included
    the key. Reading it today must still hash-validate; if `strategy_params`
    ever defaulted to `{}` instead of `None`, this would raise
    ``ValueError('strategy instance configuration hash is invalid')`` for
    every bot deployed before this PR."""
    repository = _repository(tmp_path)
    instance_dir = tmp_path / "live_state" / _SID
    instance_dir.mkdir(parents=True)
    pre_1701_record = StrategyInstanceRecord.from_binding(_binding()).model_dump(mode="json")
    pre_1701_record.pop("strategy_params")
    (instance_dir / "strategy_instance.json").write_text(
        json.dumps(pre_1701_record), encoding="utf-8"
    )
    (instance_dir / "current_run.json").write_text(
        CurrentRunBinding(strategy_instance_id=_SID, run_id="run-001", bound_at_ms=1_000).model_dump_json(),
        encoding="utf-8",
    )
    runs_dir = instance_dir / "runs"
    runs_dir.mkdir()
    (runs_dir / "run-001.json").write_text(
        BotRunRecord(
            run_id="run-001",
            strategy_instance_id=_SID,
            configuration_hash=pre_1701_record["configuration_hash"],
            launch_reason="deploy",
            started_at_ms=1_000,
        ).model_dump_json(),
        encoding="utf-8",
    )

    read_back = repository.read(_SID)

    assert read_back == _binding()
    assert read_back.strategy_params is None


def test_read_returns_none_for_instance_without_current_run(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    instance_dir = tmp_path / "live_state" / _SID
    instance_dir.mkdir(parents=True)
    (instance_dir / "strategy_instance.json").write_text(
        StrategyInstanceRecord.from_binding(_binding()).model_dump_json(),
        encoding="utf-8",
    )

    assert repository.read(_SID) is None


def test_read_lifts_legacy_binding_when_current_run_evidence_is_missing(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    binding = _binding()
    instance_dir = tmp_path / "live_state" / _SID
    instance_dir.mkdir(parents=True)
    (instance_dir / "strategy_instance.json").write_text(
        StrategyInstanceRecord.from_binding(binding).model_dump_json(),
        encoding="utf-8",
    )
    (instance_dir / "current_run.json").write_text(
        CurrentRunBinding(
            strategy_instance_id=_SID,
            run_id=binding.run_id,
            bound_at_ms=binding.created_at_ms,
        ).model_dump_json(),
        encoding="utf-8",
    )
    (instance_dir / "broker_binding.json").write_text(
        binding.model_dump_json(),
        encoding="utf-8",
    )

    assert repository.read(_SID) == binding
