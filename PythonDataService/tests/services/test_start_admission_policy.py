"""Start-admission policy tests for interactive and pinned cohort dispatch."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.broker.ibkr.config import IbkrSettings
from app.engine.live.account_artifacts import (
    AccountFreezeEvidence,
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchMemberSchedule,
    CohortBatchLaunchReceipt,
)
from app.engine.live.bot_lifecycle_state import (
    BotLifecycleStateRepo,
    BotRollCallOfferRecord,
    stable_bot_lifecycle_state_path,
)
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
    recorded_member_outcome: bool = False,
) -> StartAdmissionDependencies:
    async def _interactive_observation(*_args: object) -> None:
        interactive_calls.append("observation")

    async def _interactive_fleet(*_args: object) -> None:
        interactive_calls.append("fleet")

    async def _fetch_process(*_args: object) -> tuple[object, dict[str, object] | None]:
        return object(), {"state": daemon_state}

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
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.allowed is True
    assert decision.policy == "receipt_authorized_cohort"
    assert interactive_calls == []


def test_admit_pinned_cohort_rechecks_daemon_startability_at_slot(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    service = StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=IbkrSettings(broker_enabled=False),
        dependencies=_dependencies(receipt=receipt, interactive_calls=[], daemon_state="running"),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.allowed is False
    assert decision.policy == "receipt_authorized_cohort"
    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ALREADY_RUNNING"


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
                roll_call_offer_id="fresh-offer",
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
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def _offer(*, offer_id: str, run_id: str) -> BotRollCallOfferRecord:
    return BotRollCallOfferRecord(
        offer_id=offer_id,
        strategy_instance_id="bot-a",
        run_id=run_id,
        session_date="2026-07-21",
        issued_at_ms=0,
        expires_at_ms=999_999_999,
    )


def _service(tmp_path: Path, dependencies: StartAdmissionDependencies, *, settings: IbkrSettings | None) -> StartAdmissionService:
    return StartAdmissionService(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        settings=settings,
        dependencies=dependencies,
    )


def test_admit_unknown_run_id_returns_interactive_with_no_strategy_instance_id_and_no_refusal(
    tmp_path: Path,
) -> None:
    interactive_calls: list[str] = []
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit("unknown-run", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.policy == "interactive"
    assert decision.strategy_instance_id is None
    assert decision.allowed is True
    assert interactive_calls == []


def test_admit_interactive_happy_path_allows_start(tmp_path: Path) -> None:
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        active_roll_call_offer=lambda _root, _sid, _now_ms: _offer(offer_id="fresh-offer", run_id="run-a"),
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(strategy="spy_ema_crossover", roll_call_offer_id="fresh-offer"),
        )
    )

    assert decision.allowed is True
    assert decision.policy == "interactive"
    assert decision.strategy_instance_id == "bot-a"
    assert interactive_calls == ["observation", "fleet"]


def test_admit_interactive_rejects_when_bot_soft_deleted(tmp_path: Path) -> None:
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        run_is_soft_deleted=lambda _root, _sid, _run_id: True,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.policy == "interactive"
    assert decision.refusal is not None
    assert decision.refusal.status_code == 410
    assert decision.refusal.detail["reason_code"] == "BOT_SOFT_DELETED"
    assert interactive_calls == []


def test_admit_interactive_rejects_when_lifecycle_retired(tmp_path: Path) -> None:
    BotLifecycleStateRepo(stable_bot_lifecycle_state_path(tmp_path, "bot-a")).retire(
        now_ms=1_000,
        updated_by="operator.alice",
        reason="test retirement",
    )
    interactive_calls: list[str] = []
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.policy == "interactive"
    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "BOT_RETIRED"
    assert interactive_calls == []


def test_admit_interactive_rejects_when_lifecycle_state_unreadable(tmp_path: Path) -> None:
    path = stable_bot_lifecycle_state_path(tmp_path, "bot-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=[]),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.status_code == 409
    assert decision.refusal.detail["reason_code"] == "BOT_LIFECYCLE_STATE_UNREADABLE"


def test_admit_interactive_rejects_invalid_strategy_instance_id(tmp_path: Path) -> None:
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=[]),
        scan_runs_by_instance=lambda _root: {
            "../evil": [{"run_id": "run-a", "run_dir": "/tmp/run-a"}]
        },
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.status_code == 400
    assert decision.refusal.detail == "invalid strategy_instance_id"


def test_admit_interactive_rejects_when_account_frozen(tmp_path: Path) -> None:
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        account_freeze=lambda _root, _runs: AccountFreezeEvidence(
            account_id="DU123456",
            reason="test.freeze",
            source="test",
            recorded_at_ms=1_000,
            operator_next_step="RECOVER",
        ),
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ACCOUNT_FROZEN"
    assert interactive_calls == []


def test_admit_interactive_rejects_when_account_id_unavailable(tmp_path: Path) -> None:
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        run_account_id=lambda _run_dir: None,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ACCOUNT_ID_UNAVAILABLE"
    assert interactive_calls == []


def test_admit_interactive_requires_ibkr_settings(tmp_path: Path) -> None:
    service = _service(tmp_path, _dependencies(receipt=None, interactive_calls=[]), settings=None)

    with pytest.raises(RuntimeError, match="requires IBKR settings"):
        asyncio.run(
            service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
        )


def test_admit_interactive_translates_observation_guard_http_exception_with_dict_detail(
    tmp_path: Path,
) -> None:
    async def _raising_observation(*_args: object) -> None:
        raise HTTPException(409, detail={"reason_code": "OUTCOME_UNKNOWN", "message": "retry"})

    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=[]),
        interactive_observation_guard=_raising_observation,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.status_code == 409
    assert decision.refusal.detail == {"reason_code": "OUTCOME_UNKNOWN", "message": "retry"}


def test_admit_interactive_translates_fleet_guard_http_exception_with_string_detail(
    tmp_path: Path,
) -> None:
    interactive_calls: list[str] = []

    async def _observation(*_args: object) -> None:
        interactive_calls.append("observation")

    async def _raising_fleet(*_args: object) -> None:
        raise HTTPException(503, detail="fleet contamination detected")

    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        interactive_observation_guard=_observation,
        interactive_fleet_guard=_raising_fleet,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.status_code == 503
    assert decision.refusal.detail == "fleet contamination detected"
    assert interactive_calls == ["observation"]


def test_admit_interactive_rejects_when_crash_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    interactive_calls: list[str] = []
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
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "CRASH_RECOVERY_REQUIRED"
    assert interactive_calls == ["observation", "fleet"]


def test_admit_interactive_rejects_when_run_poisoned(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    (run_dir / "poisoned.flag").write_text("{}", encoding="utf-8")
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        scan_runs_by_instance=lambda _root: {
            "bot-a": [{"run_id": "run-a", "run_dir": str(run_dir)}]
        },
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "STOPPED_REQUIRES_REDEPLOY"
    assert interactive_calls == ["observation", "fleet"]


def test_admit_interactive_rejects_when_host_service_offline(tmp_path: Path) -> None:
    async def _fetch_offline(*_args: object) -> tuple[object, dict[str, object] | None]:
        return object(), None

    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        fetch_instance_process=_fetch_offline,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "HOST_SERVICE_OFFLINE"


def test_admit_interactive_rejects_when_daemon_stopping(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=[], daemon_state="stopping"),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "STOPPING"


def test_admit_interactive_rejects_at_start_boundary(tmp_path: Path) -> None:
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=[]),
        start_boundary_allowed=lambda _now_ms, _config: SimpleNamespace(
            allowed=False,
            reason_code="SESSION_STOP_REACHED",
            message="past the effective stop",
            session_date="2026-07-21",
            effective_stop_ms=12_345,
        ),
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.status_code == 409
    detail = decision.refusal.detail
    assert detail["reason_code"] == "SESSION_STOP_REACHED"
    assert detail["message"] == "past the effective stop"
    assert detail["gate_id"] == "daily_lifecycle.effective_stop"
    assert detail["session_date"] == "2026-07-21"
    assert detail["effective_stop_ms"] == 12_345
    assert detail["strategy_instance_id"] == "bot-a"


def test_admit_interactive_requires_roll_call_offer_id(tmp_path: Path) -> None:
    interactive_calls: list[str] = []
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit("run-a", HostRunnerStartRequest(strategy="spy_ema_crossover"))
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ROLL_CALL_OFFER_REQUIRED"
    assert interactive_calls == ["observation", "fleet"]


def test_admit_interactive_rejects_stale_roll_call_offer(tmp_path: Path) -> None:
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=[]),
        active_roll_call_offer=lambda _root, _sid, _now_ms: _offer(offer_id="current-offer", run_id="run-a"),
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(strategy="spy_ema_crossover", roll_call_offer_id="stale-offer"),
        )
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ROLL_CALL_OFFER_STALE"
    assert decision.refusal.detail["current_offer_id"] == "current-offer"


def test_admit_interactive_rejects_roll_call_offer_run_mismatch(tmp_path: Path) -> None:
    dependencies = replace(
        _dependencies(receipt=None, interactive_calls=[]),
        active_roll_call_offer=lambda _root, _sid, _now_ms: _offer(offer_id="fresh-offer", run_id="run-other"),
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(strategy="spy_ema_crossover", roll_call_offer_id="fresh-offer"),
        )
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ROLL_CALL_OFFER_RUN_MISMATCH"
    assert decision.refusal.detail["run_id"] == "run-a"
    assert decision.refusal.detail["offer_run_id"] == "run-other"


def test_admit_interactive_rejects_when_expired_roll_call_offer_absent(tmp_path: Path) -> None:
    service = _service(
        tmp_path,
        _dependencies(receipt=None, interactive_calls=[]),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(strategy="spy_ema_crossover", roll_call_offer_id="fresh-offer"),
        )
    )

    assert decision.refusal is not None
    assert decision.refusal.detail["reason_code"] == "ROLL_CALL_OFFER_EXPIRED"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda receipt: receipt.model_copy(update={"schema_version": 1}), id="schema_v1"),
        pytest.param(
            lambda receipt: receipt.model_copy(
                update={
                    "member_pins": tuple(
                        pin for pin in receipt.member_pins if pin.strategy_instance_id != "bot-a"
                    )
                }
            ),
            id="missing_pin",
        ),
    ],
)
def test_receipt_does_not_authorize_when_receipt_shape_disqualifies_it(tmp_path: Path, mutate) -> None:
    receipt = mutate(_receipt(now_ms=1_000))
    interactive_calls: list[str] = []
    service = _service(
        tmp_path,
        _dependencies(receipt=receipt, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def test_receipt_does_not_authorize_when_account_id_does_not_match(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=receipt, interactive_calls=interactive_calls),
        run_account_id=lambda _run_dir: "OTHER-ACCOUNT",
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def test_receipt_does_not_authorize_before_the_scheduled_slot_time(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    dependencies = replace(
        _dependencies(receipt=receipt, interactive_calls=interactive_calls),
        now_ms=lambda: 0,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def test_receipt_does_not_authorize_when_start_request_does_not_match_pinned_schedule(
    tmp_path: Path,
) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    service = _service(
        tmp_path,
        _dependencies(receipt=receipt, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                max_orders_per_day=5,
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def test_receipt_does_not_authorize_when_request_has_no_cohort_id(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []
    service = _service(
        tmp_path,
        _dependencies(receipt=receipt, interactive_calls=interactive_calls),
        settings=IbkrSettings(broker_enabled=False),
    )

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(strategy="spy_ema_crossover", roll_call_offer_id="fresh-offer"),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def test_receipt_does_not_authorize_when_account_events_are_unreadable(tmp_path: Path) -> None:
    receipt = _receipt(now_ms=1_000)
    interactive_calls: list[str] = []

    def _raise_events(_root: Path, _account_id: str) -> list[dict[str, object]]:
        raise OSError("account events unreadable")

    dependencies = replace(
        _dependencies(receipt=receipt, interactive_calls=interactive_calls),
        read_account_events=_raise_events,
    )
    service = _service(tmp_path, dependencies, settings=IbkrSettings(broker_enabled=False))

    decision = asyncio.run(
        service.admit(
            "run-a",
            HostRunnerStartRequest(
                strategy="spy_ema_crossover",
                roll_call_offer_id="fresh-offer",
                cohort_id=receipt.cohort_id,
            ),
        )
    )

    assert decision.policy == "interactive"
    assert interactive_calls == ["observation", "fleet"]


def test_event_follows_authorization_rejects_non_int_and_bool_seq() -> None:
    assert start_admission_policy._event_follows_authorization({"seq": 5}, 1) is True
    assert start_admission_policy._event_follows_authorization({"seq": 1}, 1) is False
    assert start_admission_policy._event_follows_authorization({"seq": "5"}, 1) is False
    assert start_admission_policy._event_follows_authorization({"seq": True}, 0) is False
    assert start_admission_policy._event_follows_authorization({}, 1) is False


def test_http_exception_detail_handles_string_dict_and_other_types() -> None:
    assert start_admission_policy._http_exception_detail(HTTPException(400, detail="bad")) == "bad"
    assert start_admission_policy._http_exception_detail(
        HTTPException(400, detail={"reason_code": "X"})
    ) == {"reason_code": "X"}
    assert start_admission_policy._http_exception_detail(HTTPException(400, detail=["a", "b"])) == "['a', 'b']"
