"""Start-admission policy tests for interactive and pinned cohort dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.ibkr.config import IbkrSettings
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchMemberSchedule,
    CohortBatchLaunchReceipt,
)
from app.engine.live.bot_lifecycle_state import BotLifecycleStateRepo, stable_bot_lifecycle_state_path
from app.schemas.live_runs import HostRunnerStartRequest
from app.services import start_admission_policy
from app.services.start_admission_policy import StartAdmissionDependencies, StartAdmissionService


def _receipt(*, now_ms: int) -> CohortBatchLaunchReceipt:
    return CohortBatchLaunchReceipt(
        schema_version=2,
        launch_profile="paper_three_bot_stagger_v2",
        account_id="DU123456",
        cohort_id="cohort-1",
        member_strategy_instance_ids=("bot-a", "bot-b", "bot-c"),
        window_start_ms=now_ms + 1_805_000,
        window_end_ms=now_ms + 5_405_000,
        authorized_by="operator.alice",
        recorded_at_ms=now_ms,
        member_pins=tuple(
            CohortBatchLaunchMemberPin(
                strategy_instance_id=f"bot-{member}",
                run_id=f"run-{member}",
                roll_call_offer_id=f"offer-{member}",
            )
            for member in ("a", "b", "c")
        ),
        member_schedule=tuple(
            CohortBatchLaunchMemberSchedule(
                strategy_instance_id=f"bot-{member}",
                run_id=f"run-{member}",
                scheduled_start_at_ms=now_ms + index * 900_000,
                start_request=HostRunnerStartRequest(strategy="spy_ema_crossover").model_dump(
                    mode="json", exclude={"roll_call_offer_id", "cohort_id"}
                ),
            )
            for index, member in enumerate(("a", "b", "c"))
        ),
    )


def _dependencies(
    *,
    receipt: CohortBatchLaunchReceipt | None,
    interactive_calls: list[str],
    daemon_state: str = "idle",
    daemon_run_id: str | None = None,
    recorded_member_outcome: bool = False,
) -> StartAdmissionDependencies:
    async def _interactive_observation(*_args: object) -> None:
        interactive_calls.append("observation")

    async def _interactive_fleet(*_args: object) -> None:
        interactive_calls.append("fleet")

    async def _fetch_process(*_args: object) -> tuple[object, dict[str, object] | None]:
        return object(), {"state": daemon_state, "run_id": daemon_run_id}

    return StartAdmissionDependencies(
        scan_runs_by_instance=lambda _root: {
            "bot-a": [{"run_id": "run-a", "run_dir": "/tmp/run-a"}]
        },
        run_is_soft_deleted=lambda _root, _sid, _run_id: False,
        soft_deleted_detail=lambda sid, run_id: {
            "reason_code": "BOT_SOFT_DELETED",
            "strategy_instance_id": sid,
            "run_id": run_id,
        },
        account_freeze=lambda _root, _runs: None,
        run_account_id=lambda _run_dir: "DU123456",
        interactive_observation_guard=_interactive_observation,
        interactive_fleet_guard=_interactive_fleet,
        fetch_instance_process=_fetch_process,
        active_roll_call_offer=lambda _root, _sid, now_ms: None,
        read_account_events=lambda _root, _account_id: _receipt_events(
            receipt,
            recorded_member_outcome=recorded_member_outcome,
        ),
        live_config_for_run=lambda _run_dir: {},
        start_boundary_allowed=lambda _now_ms, _config: SimpleNamespace(allowed=True),
        now_ms=lambda: 1_000,
    )


def _receipt_events(
    receipt: CohortBatchLaunchReceipt | None,
    *,
    recorded_member_outcome: bool,
) -> list[dict[str, object]]:
    if receipt is None:
        return []
    events: list[dict[str, object]] = [
        {"event_type": "cohort_batch_launch_authorized", "seq": 1, **receipt.model_dump(mode="json")}
    ]
    if recorded_member_outcome:
        events.append(
            {
                "event_type": "cohort_batch_launch_member_start_recorded",
                "seq": 2,
                "cohort_id": receipt.cohort_id,
                "strategy_instance_id": "bot-a",
            }
        )
    return events


def test_admit_valid_pinned_cohort_bypasses_interactive_broker_and_roll_call_guards(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(receipt=receipt, interactive_calls=interactive_calls),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="offer-a",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.allowed is True
    assert decision.policy == "receipt_authorized_cohort"
    assert interactive_calls == []


def test_admit_pinned_cohort_rejects_a_different_active_run_at_slot(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(
            receipt=receipt,
            interactive_calls=[],
            daemon_state="running",
            daemon_run_id="run-other",
        ),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="offer-a",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.allowed is False
    assert decision.policy == "receipt_authorized_cohort"
    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ALREADY_RUNNING"


def test_admit_pinned_cohort_recovers_an_exact_active_run_idempotently(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(
            receipt=receipt,
            interactive_calls=[],
            daemon_state="running",
            daemon_run_id="run-a",
        ),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="offer-a",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.allowed is True
    assert decision.policy == "receipt_authorized_cohort"
    assert decision.idempotent_process == {"state": "running", "run_id": "run-a"}


@pytest.mark.parametrize(
    ("dynamic_gate", "reason_code"),
    [
        ("account_freeze", "ACCOUNT_FROZEN"),
        ("soft_delete", "BOT_SOFT_DELETED"),
        ("lifecycle_retirement", "BOT_RETIRED"),
        ("crash_recovery", "CRASH_RECOVERY_REQUIRED"),
        ("poison", "STOPPED_REQUIRES_REDEPLOY"),
    ],
)
def test_admit_pinned_cohort_rechecks_each_dynamic_safety_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dynamic_gate: str,
    reason_code: str,
) -> None:
    receipt = _receipt(now_ms=1_000)
    dependencies = _dependencies(receipt=receipt, interactive_calls=[])
    if dynamic_gate == "account_freeze":
        dependencies = replace(
            dependencies,
            account_freeze=lambda _root, _runs: AccountFreezeEvidence(
                account_id="DU123456",
                reason="test.freeze",
                source="test",
                recorded_at_ms=1_000,
                operator_next_step="RECOVER",
            ),
        )
    elif dynamic_gate == "soft_delete":
        dependencies = replace(dependencies, run_is_soft_deleted=lambda _root, _sid, _run_id: True)
    elif dynamic_gate == "lifecycle_retirement":
        BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, "bot-a")).retire(
            now_ms=1_000,
            updated_by="operator.alice",
            reason="test retirement",
        )
    elif dynamic_gate == "crash_recovery":
        binding = SimpleNamespace()
        monkeypatch.setattr(
            start_admission_policy,
            "crash_recovery_blocking_binding",
            lambda *_args, **_kwargs: binding,
        )
        monkeypatch.setattr(
            start_admission_policy,
            "crash_recovery_block_detail",
            lambda _sid, _binding: {"reason_code": "CRASH_RECOVERY_REQUIRED"},
        )
    else:
        run_dir = tmp_path / "run-a"
        run_dir.mkdir()
        (run_dir / "poisoned.flag").write_text("{}", encoding="utf-8")
        dependencies = replace(
            dependencies,
            scan_runs_by_instance=lambda _root: {
                "bot-a": [{"run_id": "run-a", "run_dir": str(run_dir)}]
            },
        )

    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=dependencies,
    )
    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="offer-a",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "receipt_authorized_cohort"
    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == reason_code


def test_admit_client_cohort_id_without_a_matching_durable_pin_uses_interactive_policy(tmp_path: Path) -> None:
    interactive_calls: list[str] = []
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(receipt=None, interactive_calls=interactive_calls),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="fresh-offer",
                cohort_id="untrusted-client-id",
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]
    assert decision.allowed is False
    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ROLL_CALL_OFFER_EXPIRED"


def test_admit_cohort_with_nonpinned_offer_uses_interactive_policy(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(receipt=receipt, interactive_calls=interactive_calls),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="not-the-durable-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]
    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ROLL_CALL_OFFER_EXPIRED"


def test_admit_recorded_cohort_member_cannot_reuse_its_receipt_pin(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(
            receipt=receipt,
            interactive_calls=interactive_calls,
            recorded_member_outcome=True,
        ),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="offer-a",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]
