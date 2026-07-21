"""Tests for server-authored cohort evidence evaluation."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberOutcome,
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchMemberSchedule,
    CohortBatchLaunchReceipt,
    append_account_event,
    read_account_events,
    record_cohort_batch_launch_receipt,
)
from app.engine.live.account_registry import (
    AccountInstanceBinding,
    write_account_instance_binding,
)
from app.engine.live.live_state_sidecar import LiveStateEnvelope
from app.schemas.live_runs import (
    BotRollCallOffer,
    BotRollCallResponse,
    BotRollCallSummary,
    HostRunnerStartRequest,
)
from app.services import cohort_launch
from app.services.cohort_batch_launch import CohortBatchLaunchService, parse_cohort_evidence_sample
from app.services.cohort_evidence import (
    CohortEvidenceSample,
    CohortEvidenceSampler,
    CohortEvidenceSamplerRegistry,
    CohortMemberSample,
    evaluate_healthy_overlap,
)
from app.services.cohort_evidence_runtime import CohortEvidenceRuntimeObserver
from app.services.cohort_launch import (
    CohortLaunchCoordinator,
    CohortLaunchSchedulerRegistry,
    resume_open_cohort_evidence_samplers,
)


def _sample(at_ms: int, *members: str) -> CohortEvidenceSample:
    return CohortEvidenceSample(
        expected_at_ms=at_ms,
        observed_at_ms=at_ms,
        account_truth="healthy",
        fleet="healthy",
        members=tuple(
            CohortMemberSample(member, f"run-{member}", "healthy", orders_used=0, orders_cap=4)
            for member in members
        ),
    )


async def _paper_target_account_posture(_account_id: str) -> str:
    return "PAPER_EXECUTION"


def _three_bot_stagger_receipt(*, first_start_ms: int = 1_780_000_000_000) -> CohortBatchLaunchReceipt:
    """Return a valid V2 receipt with deliberately distant later slots."""

    members = ("bot-a", "bot-b", "bot-c")
    return CohortBatchLaunchReceipt(
        schema_version=2,
        launch_profile="paper_three_bot_stagger_v2",
        account_id="DU123456",
        cohort_id="partial-outcomes-cohort",
        member_strategy_instance_ids=members,
        window_start_ms=first_start_ms + 1_805_000,
        window_end_ms=first_start_ms + 1_805_000 + 3_600_000,
        authorized_by="operator.alice",
        recorded_at_ms=first_start_ms,
        member_schedule=tuple(
            CohortBatchLaunchMemberSchedule(
                strategy_instance_id=member_id,
                run_id=f"run-{member_id}",
                scheduled_start_at_ms=first_start_ms + (index * 900_000),
                start_request={"strategy": "spy_ema_crossover"},
            )
            for index, member_id in enumerate(members)
        ),
    )


def test_cohort_member_outcome_rejects_an_unknown_reason_code() -> None:
    with pytest.raises(ValidationError, match="reason"):
        CohortBatchLaunchMemberOutcome(
            strategy_instance_id="bot-a",
            state="blocked",
            reason="ACCOUNT_FROZEN",
            next_safe_action="Clear the account freeze.",
        )


def test_scheduled_outcomes_project_partial_receipt_immediately(tmp_path: Path) -> None:
    receipt = _three_bot_stagger_receipt()
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    service = CohortBatchLaunchService(artifacts_root=tmp_path)

    asyncio.run(
        service.record_scheduled_member_outcome(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            outcome=CohortBatchLaunchMemberOutcome(
                strategy_instance_id="bot-a",
                state="accepted",
                reason="COHORT_START_ACCEPTED",
                next_safe_action="Monitor the bot receipt state and account exposure.",
            ),
            recorded_at_ms=receipt.recorded_at_ms + 1,
        )
    )

    status = asyncio.run(
        service.get_status(account_id=receipt.account_id, cohort_id=receipt.cohort_id)
    )

    assert status is not None
    assert status.outcomes_state == "recorded"
    assert [outcome.strategy_instance_id for outcome in status.outcomes] == ["bot-a"]
    assert status.member_strategy_instance_ids == ["bot-a", "bot-b", "bot-c"]


def test_scheduled_outcomes_fail_closed_for_duplicate_member(tmp_path: Path) -> None:
    receipt = _three_bot_stagger_receipt()
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    duplicate = {
        "event_type": "cohort_batch_launch_member_start_recorded",
        "cohort_id": receipt.cohort_id,
        "recorded_at_ms": receipt.recorded_at_ms + 1,
        "strategy_instance_id": "bot-a",
        "state": "accepted",
        "reason": "COHORT_START_ACCEPTED",
        "next_safe_action": "Monitor the bot receipt state and account exposure.",
    }
    append_account_event(tmp_path, receipt.account_id, duplicate)
    append_account_event(tmp_path, receipt.account_id, duplicate)

    status = asyncio.run(
        CohortBatchLaunchService(artifacts_root=tmp_path).get_status(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
        )
    )

    assert status is not None
    assert status.outcomes_state == "unreadable"
    assert status.outcomes == []


def test_healthy_overlap_requires_exact_concurrent_members() -> None:
    evidence = evaluate_healthy_overlap(
        (_sample(0, "a", "b", "c"), _sample(5_000, "a", "b", "c")),
        member_strategy_instance_ids=("a", "b", "c"),
        cadence_ms=5_000,
    )

    assert evidence.verdict == "healthy"
    assert evidence.healthy_overlap_ms == 10_000


def test_healthy_overlap_fails_sequential_members_and_observation_gaps() -> None:
    sequential = evaluate_healthy_overlap(
        (_sample(0, "a"), _sample(5_000, "b"), _sample(10_000, "c")),
        member_strategy_instance_ids=("a", "b", "c"),
        cadence_ms=5_000,
    )
    gapped = evaluate_healthy_overlap(
        (_sample(0, "a"), _sample(10_000, "a")),
        member_strategy_instance_ids=("a",),
        cadence_ms=5_000,
    )

    assert sequential.reason == "COHORT_MEMBER_OBSERVATION_MISSING"
    assert gapped.reason == "COHORT_OBSERVATION_GAP"


def test_sampler_persists_expected_cadence_and_failed_observation() -> None:
    clock = {"ms": 10_000}
    persisted: list[CohortEvidenceSample] = []

    async def observe(expected_at_ms: int) -> CohortEvidenceSample:
        if expected_at_ms == 15_000:
            raise ConnectionError("broker unavailable")
        return _sample(expected_at_ms, "a")

    async def persist(sample: CohortEvidenceSample) -> None:
        persisted.append(sample)

    sampler = CohortEvidenceSampler(
        cadence_ms=5_000,
        now_ms=lambda: clock["ms"],
        observe=observe,
        persist=persist,
    )
    asyncio.run(sampler.sample_once())
    clock["ms"] = 16_000
    asyncio.run(sampler.sample_once())

    assert [sample.expected_at_ms for sample in persisted] == [10_000, 15_000]
    assert persisted[1].observed_at_ms is None
    assert persisted[1].account_truth == "unknown"


def test_healthy_overlap_fails_after_a_restart_or_late_sampler_tick() -> None:
    evidence = evaluate_healthy_overlap(
        (_sample(10_000, "a"),),
        member_strategy_instance_ids=("a",),
        cadence_ms=5_000,
        evaluated_at_ms=15_000,
    )

    assert evidence.verdict == "failed"
    assert evidence.reason == "COHORT_OBSERVATION_GAP"


def test_healthy_overlap_refuses_missing_runtime_counters() -> None:
    sample = CohortEvidenceSample(
        expected_at_ms=10_000,
        observed_at_ms=10_000,
        account_truth="healthy",
        fleet="healthy",
        members=(CohortMemberSample("a", "run-a", "healthy"),),
    )

    evidence = evaluate_healthy_overlap(
        (sample,),
        member_strategy_instance_ids=("a",),
        cadence_ms=5_000,
    )

    assert evidence.verdict == "failed"
    assert evidence.reason == "RUNTIME_COUNTERS_MISSING"


def test_sampler_registry_releases_crashed_task_and_logs_failure(caplog) -> None:
    """A failed sampler is visible and a later restart can replace it."""

    class FailingSampler:
        async def run(self, _stop: asyncio.Event) -> None:
            raise RuntimeError("evidence persistence failed")

    class CompletingSampler:
        def __init__(self, runs: list[str]) -> None:
            self._runs = runs

        async def run(self, _stop: asyncio.Event) -> None:
            self._runs.append("replacement")

    async def exercise() -> None:
        registry = CohortEvidenceSamplerRegistry()
        registry.start("cohort-a", FailingSampler())  # type: ignore[arg-type]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        runs: list[str] = []
        registry.start("cohort-a", CompletingSampler(runs))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert runs == ["replacement"]

    asyncio.run(exercise())

    assert "cohort evidence sampler task died" in caplog.text
    assert "evidence persistence failed" in caplog.text


def test_resume_open_cohort_sampling_uses_next_durable_cadence(tmp_path: Path, monkeypatch) -> None:
    """Restart resumes an open receipt without manufacturing backdated ticks."""

    receipt = CohortBatchLaunchReceipt(
        account_id="DU123456",
        cohort_id="resume-cohort",
        member_strategy_instance_ids=("bot-a",),
        window_start_ms=10_000,
        window_end_ms=40_000,
        authorized_by="operator.alice",
        recorded_at_ms=10_000,
        member_pins=(
            CohortBatchLaunchMemberPin(
                strategy_instance_id="bot-a",
                run_id="run-a",
                roll_call_offer_id="offer-a",
            ),
        ),
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    asyncio.run(
        CohortBatchLaunchService(artifacts_root=tmp_path).record_evidence_sample(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            sample=_sample(10_000, "bot-a"),
        )
    )
    first_expected: list[int] = []

    def fake_sampler(**kwargs):
        first_expected.append(kwargs["first_expected_at_ms"])
        return object()

    class RecordingRegistry:
        def start(self, cohort_id: str, _sampler: object) -> None:
            assert cohort_id == receipt.cohort_id

    monkeypatch.setattr("app.services.cohort_launch._evidence_sampler", fake_sampler)
    asyncio.run(
        resume_open_cohort_evidence_samplers(
            artifacts_root=tmp_path,
            live_runs_root=tmp_path / "runs",
            visible_runs_by_instance=lambda _root: {},
            now_ms=lambda: 12_500,
            evidence_samplers=RecordingRegistry(),  # type: ignore[arg-type]
        )
    )

    assert first_expected == [15_000]


def test_three_bot_stagger_dispatches_from_durable_schedule(tmp_path: Path) -> None:
    """The V2 scheduler owns all three slots after receipt persistence."""

    initial_now_ms = int(datetime(2026, 7, 8, 18, 0, tzinfo=UTC).timestamp() * 1_000)
    clock = {"ms": initial_now_ms}
    starts: list[tuple[str, str | None]] = []
    posture_checks: list[str] = []
    offers = [
        BotRollCallOffer(
            offer_id=f"offer-{member}",
            strategy_instance_id=f"bot-{member}",
            run_id=f"run-{member}",
            session_date="2026-07-16",
            issued_at_ms=0,
            expires_at_ms=9_999_999,
        )
        for member in ("a", "b", "c")
    ]

    async def roll_call() -> BotRollCallResponse:
        return BotRollCallResponse(summary=BotRollCallSummary(ready=3), offers=offers)

    async def start_run(run_id: str, request: HostRunnerStartRequest) -> SimpleNamespace:
        starts.append((run_id, request.roll_call_offer_id))
        return SimpleNamespace(accepted=True)

    async def paper_target_account_posture(account_id: str) -> str:
        posture_checks.append(account_id)
        return "PAPER_EXECUTION"

    coordinator = CohortLaunchCoordinator(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        run_roll_call=roll_call,
        start_run=start_run,
        visible_runs_by_instance=lambda _root: {
            f"bot-{member}": [{"run_id": f"run-{member}", "run_dir": str(tmp_path / f"run-{member}")}]
            for member in ("a", "b", "c")
        },
        run_account_id=lambda _run_dir: "DU123456",
        run_live_config=lambda _run_dir: {},
        start_request_for_run=lambda _run_dir: HostRunnerStartRequest(strategy="spy_ema_crossover"),
        target_account_posture=paper_target_account_posture,
        now_ms=lambda: clock["ms"],
        evidence_samplers=CohortEvidenceSamplerRegistry(),
        launch_schedulers=CohortLaunchSchedulerRegistry(),
    )

    async def no_evidence(_receipt: CohortBatchLaunchReceipt) -> None:
        return None

    coordinator._start_evidence_sampler = no_evidence  # type: ignore[method-assign]

    async def exercise() -> None:
        status = await coordinator.launch(
            account_id="DU123456",
            requested_members=("bot-a", "bot-b", "bot-c"),
            operator_identity="operator.alice",
            identity_header_present=True,
            client_host="127.0.0.1",
            launch_profile="paper_three_bot_stagger_v2",
        )
        assert status.schema_version == 2
        assert status.launch_profile == "paper_three_bot_stagger_v2"
        assert status.outcomes_state == "pending"
        assert status.member_scheduled_start_at_ms == {
            "bot-a": initial_now_ms,
            "bot-b": initial_now_ms + 900_000,
            "bot-c": initial_now_ms + 1_800_000,
        }
        assert status.window_end_ms - status.window_start_ms == 60 * 60 * 1_000
        clock["ms"] = initial_now_ms + 2_000_000
        for _ in range(8):
            await asyncio.sleep(0.01)

    asyncio.run(exercise())

    assert starts == [
        ("run-a", "offer-a"),
        ("run-b", "offer-b"),
        ("run-c", "offer-c"),
    ]
    assert posture_checks == ["DU123456", "DU123456", "DU123456", "DU123456"]
    status = asyncio.run(
        CohortBatchLaunchService(artifacts_root=tmp_path).get_status(
            account_id="DU123456",
            cohort_id=None,
        )
    )
    assert status is not None
    assert [outcome.state for outcome in status.outcomes] == ["accepted", "accepted", "accepted"]


def test_staggered_cohort_cascades_skips_without_waiting_for_later_slots(tmp_path: Path) -> None:
    receipt = _three_bot_stagger_receipt(first_start_ms=0)
    starts: list[str] = []

    async def roll_call() -> BotRollCallResponse:
        return BotRollCallResponse(
            summary=BotRollCallSummary(ready=3),
            offers=[
                BotRollCallOffer(
                    offer_id=f"offer-{member}",
                    strategy_instance_id=member,
                    run_id=f"run-{member}",
                    session_date="2026-07-20",
                    issued_at_ms=0,
                    expires_at_ms=9_999_999,
                )
                for member in receipt.member_strategy_instance_ids
            ],
        )

    async def start_run(run_id: str, _request: HostRunnerStartRequest) -> SimpleNamespace:
        starts.append(run_id)
        return SimpleNamespace(accepted=False)

    coordinator = CohortLaunchCoordinator(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        run_roll_call=roll_call,
        start_run=start_run,
        visible_runs_by_instance=lambda _root: {},
        run_account_id=lambda _run_dir: None,
        run_live_config=lambda _run_dir: {},
        start_request_for_run=lambda _run_dir: None,
        target_account_posture=_paper_target_account_posture,
        now_ms=lambda: 0,
        evidence_samplers=CohortEvidenceSamplerRegistry(),
        launch_schedulers=CohortLaunchSchedulerRegistry(),
    )
    record_cohort_batch_launch_receipt(tmp_path, receipt)

    async def run_scheduler() -> None:
        await asyncio.wait_for(
            coordinator._run_staggered_cohort(receipt, asyncio.Event()),
            timeout=0.1,
        )

    asyncio.run(run_scheduler())

    assert starts == ["run-bot-a"]
    status = asyncio.run(
        CohortBatchLaunchService(artifacts_root=tmp_path).get_status(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
        )
    )
    assert status is not None
    assert [outcome.state for outcome in status.outcomes] == ["blocked", "skipped", "skipped"]
    assert [outcome.reason for outcome in status.outcomes] == [
        "COHORT_START_NOT_ACCEPTED",
        "COHORT_PRIOR_MEMBER_BLOCKED",
        "COHORT_PRIOR_MEMBER_BLOCKED",
    ]


def test_restarted_staggered_cohort_cascades_existing_blocker_without_waiting(tmp_path: Path) -> None:
    receipt = _three_bot_stagger_receipt(first_start_ms=0)
    record_cohort_batch_launch_receipt(tmp_path, receipt)
    service = CohortBatchLaunchService(artifacts_root=tmp_path)
    asyncio.run(
        service.record_scheduled_member_outcome(
            account_id=receipt.account_id,
            cohort_id=receipt.cohort_id,
            outcome=CohortBatchLaunchMemberOutcome(
                strategy_instance_id="bot-a",
                state="blocked",
                reason="COHORT_START_NOT_ACCEPTED",
                next_safe_action="Review the backend start response before authorizing a new cohort.",
            ),
            recorded_at_ms=0,
        )
    )

    async def unexpected_roll_call() -> BotRollCallResponse:
        raise AssertionError("a durable blocker must end the scheduler before a roll call")

    async def unexpected_start(_run_id: str, _request: HostRunnerStartRequest) -> SimpleNamespace:
        raise AssertionError("a durable blocker must end the scheduler before a start")

    coordinator = CohortLaunchCoordinator(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        run_roll_call=unexpected_roll_call,
        start_run=unexpected_start,
        visible_runs_by_instance=lambda _root: {},
        run_account_id=lambda _run_dir: None,
        run_live_config=lambda _run_dir: {},
        start_request_for_run=lambda _run_dir: None,
        target_account_posture=_paper_target_account_posture,
        now_ms=lambda: 0,
        evidence_samplers=CohortEvidenceSamplerRegistry(),
        launch_schedulers=CohortLaunchSchedulerRegistry(),
    )

    async def run_restarted_scheduler() -> None:
        await asyncio.wait_for(
            coordinator._run_staggered_cohort(receipt, asyncio.Event()),
            timeout=0.1,
        )

    asyncio.run(run_restarted_scheduler())

    status = asyncio.run(
        service.get_status(account_id=receipt.account_id, cohort_id=receipt.cohort_id)
    )
    assert status is not None
    assert [outcome.state for outcome in status.outcomes] == ["blocked", "skipped", "skipped"]
    assert [outcome.reason for outcome in status.outcomes] == [
        "COHORT_START_NOT_ACCEPTED",
        "COHORT_PRIOR_MEMBER_BLOCKED",
        "COHORT_PRIOR_MEMBER_BLOCKED",
    ]


def test_three_bot_stagger_refuses_before_authorization_when_restart_budget_is_exhausted(tmp_path: Path) -> None:
    for index, recorded_at_ms in enumerate((1_000, 2_000), start=1):
        write_account_instance_binding(
            tmp_path,
            AccountInstanceBinding(
                account_id="DU123456",
                strategy_instance_id=f"prior-{index}",
                run_id=f"prior-run-{index}",
                bot_order_namespace=f"learn-ai/prior-{index}/v1",
                lifecycle_state="ACTIVE",
                recorded_at_ms=recorded_at_ms,
                source="test",
            ),
        )
    offers = [
        BotRollCallOffer(
            offer_id=f"offer-{member}",
            strategy_instance_id=f"bot-{member}",
            run_id=f"run-{member}",
            session_date="2026-07-16",
            issued_at_ms=10_000,
            expires_at_ms=9_999_999,
        )
        for member in ("a", "b", "c")
    ]

    async def roll_call() -> BotRollCallResponse:
        return BotRollCallResponse(summary=BotRollCallSummary(ready=3), offers=offers)

    async def start_run(_run_id: str, _request: HostRunnerStartRequest) -> SimpleNamespace:
        raise AssertionError("restart admission must reject before a start")

    coordinator = CohortLaunchCoordinator(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        run_roll_call=roll_call,
        start_run=start_run,
        visible_runs_by_instance=lambda _root: {
            f"bot-{member}": [{"run_id": f"run-{member}", "run_dir": str(tmp_path / f"run-{member}")}]
            for member in ("a", "b", "c")
        },
        run_account_id=lambda _run_dir: "DU123456",
        run_live_config=lambda _run_dir: {},
        start_request_for_run=lambda _run_dir: HostRunnerStartRequest(strategy="spy_ema_crossover"),
        target_account_posture=_paper_target_account_posture,
        now_ms=lambda: 10_000,
        evidence_samplers=CohortEvidenceSamplerRegistry(),
        launch_schedulers=CohortLaunchSchedulerRegistry(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            coordinator.launch(
                account_id="DU123456",
                requested_members=("bot-a", "bot-b", "bot-c"),
                operator_identity="operator.alice",
                identity_header_present=True,
                client_host="127.0.0.1",
                launch_profile="paper_three_bot_stagger_v2",
            )
        )

    assert exc_info.value.detail["reason_code"] == "COHORT_RESTART_INTENSITY_WOULD_FREEZE"
    assert not any(
        event["event_type"] == "cohort_batch_launch_authorized"
        for event in read_account_events(tmp_path, "DU123456")
    )


def test_three_bot_stagger_refuses_before_authorization_when_window_crosses_stop(tmp_path: Path) -> None:
    now_ms = int(datetime(2026, 7, 8, 19, 10, tzinfo=UTC).timestamp() * 1_000)
    offers = [
        BotRollCallOffer(
            offer_id=f"offer-{member}",
            strategy_instance_id=f"bot-{member}",
            run_id=f"run-{member}",
            session_date="2026-07-08",
            issued_at_ms=now_ms,
            expires_at_ms=now_ms + 60_000,
        )
        for member in ("a", "b", "c")
    ]

    async def roll_call() -> BotRollCallResponse:
        return BotRollCallResponse(summary=BotRollCallSummary(ready=3), offers=offers)

    async def start_run(_run_id: str, _request: HostRunnerStartRequest) -> SimpleNamespace:
        raise AssertionError("the cohort must reject before any member starts")

    coordinator = CohortLaunchCoordinator(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        run_roll_call=roll_call,
        start_run=start_run,
        visible_runs_by_instance=lambda _root: {
            f"bot-{member}": [{"run_id": f"run-{member}", "run_dir": str(tmp_path / f"run-{member}")}]
            for member in ("a", "b", "c")
        },
        run_account_id=lambda _run_dir: "DU123456",
        run_live_config=lambda _run_dir: {"force_flat_at": "15:55"},
        start_request_for_run=lambda _run_dir: HostRunnerStartRequest(strategy="spy_ema_crossover"),
        target_account_posture=_paper_target_account_posture,
        now_ms=lambda: now_ms,
        evidence_samplers=CohortEvidenceSamplerRegistry(),
        launch_schedulers=CohortLaunchSchedulerRegistry(),
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            coordinator.launch(
                account_id="DU123456",
                requested_members=("bot-a", "bot-b", "bot-c"),
                operator_identity="operator.alice",
                identity_header_present=True,
                client_host="127.0.0.1",
                launch_profile="paper_three_bot_stagger_v2",
            )
        )

    assert exc_info.value.detail["reason_code"] == "COHORT_WINDOW_EXCEEDS_SESSION_STOP"
    assert not any(
        event["event_type"] == "cohort_batch_launch_authorized"
        for event in read_account_events(tmp_path, "DU123456")
    )


def test_cohort_evidence_parser_rejects_booleans_for_integral_fields() -> None:
    event = {
        "expected_at_ms": 10_000,
        "observed_at_ms": 10_000,
        "account_truth": "healthy",
        "fleet": "healthy",
        "broker_net_positions": {"SPY": 1},
        "broker_residual": {"SPY": 0},
        "members": [
            {
                "strategy_instance_id": "bot-a",
                "run_id": "run-a",
                "state": "healthy",
                "orders_used": 0,
                "orders_cap": 4,
            }
        ],
    }

    assert parse_cohort_evidence_sample(event) is not None
    for field, value in (("expected_at_ms", True), ("observed_at_ms", False)):
        malformed = dict(event, **{field: value})
        assert parse_cohort_evidence_sample(malformed) is None
    malformed_member = dict(event)
    malformed_member["members"] = [dict(event["members"][0], orders_used=True)]
    assert parse_cohort_evidence_sample(malformed_member) is None
    assert parse_cohort_evidence_sample(dict(event, broker_net_positions={"SPY": True})) is None


def test_runtime_observer_fails_member_when_poisoned_flag_precedes_sidecar_update(
    tmp_path: Path, monkeypatch
) -> None:
    """A fatal disk sentinel must stop proof even with stale healthy sidecars."""

    from app.services import cohort_evidence_runtime

    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    (run_dir / "poisoned.flag").write_text("{}", encoding="utf-8")
    observer = CohortEvidenceRuntimeObserver(
        live_runs_root=tmp_path,
        visible_runs_by_instance=lambda _root: {},
        now_ms=lambda: 10_000,
    )
    monkeypatch.setattr(
        cohort_evidence_runtime,
        "read_instance_live_state",
        lambda _root, _sid: LiveStateEnvelope(
            strategy_instance_id="a",
            run_id="run-a",
            bot_order_namespace="ns",
            ib_client_id=1,
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        ),
    )

    sample = observer._member(
        CohortBatchLaunchMemberPin(strategy_instance_id="a", run_id="run-a", roll_call_offer_id="offer"),
        {"a": [{"run_id": "run-a", "run_dir": str(run_dir)}]},
    )

    assert sample.state == "failed"
    assert sample.reason == "COHORT_MEMBER_HALTED"


async def test_runtime_observer_does_not_fetch_broker_positions(tmp_path: Path, monkeypatch) -> None:
    from app.broker.ibkr import account as ibkr_account

    observer = CohortEvidenceRuntimeObserver(
        live_runs_root=tmp_path,
        visible_runs_by_instance=lambda _root: {},
        now_ms=lambda: 10_000,
    )
    receipt = CohortBatchLaunchReceipt(
        account_id="DU123456",
        cohort_id="cohort-a",
        member_strategy_instance_ids=("bot-a",),
        window_start_ms=10_000,
        window_end_ms=20_000,
        authorized_by="operator.alice",
        recorded_at_ms=10_000,
        member_pins=(
            CohortBatchLaunchMemberPin(
                strategy_instance_id="bot-a",
                run_id="run-a",
                roll_call_offer_id="offer-a",
            ),
        ),
    )
    class BrokerPositionFetchForbidden(BaseException):
        pass

    async def fail_if_broker_position_fetches(*_args, **_kwargs) -> None:
        raise BrokerPositionFetchForbidden("cohort observation must not call IBKR")

    monkeypatch.setattr(ibkr_account, "fetch_positions", fail_if_broker_position_fetches)

    sample = await observer.observe(receipt, expected_at_ms=10_000)

    assert sample.fleet == "unknown"


async def test_runtime_observer_propagates_unexpected_account_projection_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.services import cohort_evidence_runtime

    observer = CohortEvidenceRuntimeObserver(
        live_runs_root=tmp_path,
        visible_runs_by_instance=lambda _root: {},
        now_ms=lambda: 10_000,
    )
    receipt = CohortBatchLaunchReceipt(
        account_id="DU123456",
        cohort_id="cohort-a",
        member_strategy_instance_ids=("bot-a",),
        window_start_ms=10_000,
        window_end_ms=20_000,
        authorized_by="operator.alice",
        recorded_at_ms=10_000,
        member_pins=(
            CohortBatchLaunchMemberPin(
                strategy_instance_id="bot-a",
                run_id="run-a",
                roll_call_offer_id="offer-a",
            ),
        ),
    )

    def fail_account_projection(*_args, **_kwargs) -> None:
        raise RuntimeError("account projection corrupt")

    monkeypatch.setattr(
        cohort_evidence_runtime,
        "build_account_fleet_read_contexts",
        fail_account_projection,
    )

    with pytest.raises(RuntimeError, match="account projection corrupt"):
        await observer.observe(receipt, expected_at_ms=10_000)


def test_runtime_observer_requires_fresh_running_runtime_and_ready_vector(
    tmp_path: Path, monkeypatch
) -> None:
    """Readiness counters alone cannot prove a dead or blocked child healthy."""

    from app.services import cohort_evidence_runtime

    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    observer = CohortEvidenceRuntimeObserver(
        live_runs_root=tmp_path,
        visible_runs_by_instance=lambda _root: {},
        now_ms=lambda: 10_000,
    )
    monkeypatch.setattr(
        cohort_evidence_runtime,
        "read_instance_live_state",
        lambda _root, _sid: LiveStateEnvelope(
            strategy_instance_id="a",
            run_id="run-a",
            bot_order_namespace="ns",
            ib_client_id=1,
            last_processed_bar_ms=1,
            last_artifact_flush_ms=1,
        ),
    )
    monkeypatch.setattr(
        cohort_evidence_runtime,
        "read_engine_runtime_snapshot",
        lambda _path: SimpleNamespace(
            run_id="run-a", command_loop=SimpleNamespace(state="RUNNING")
        ),
    )
    monkeypatch.setattr(
        cohort_evidence_runtime,
        "evaluate_runtime_freshness",
        lambda _runtime, **_kwargs: SimpleNamespace(posture_demoted=False),
    )
    monkeypatch.setattr(
        cohort_evidence_runtime,
        "read_readiness",
        lambda _path: {
            "kind": "live_readiness",
            "as_of_ms": 10_000,
            "source": "engine",
            "verdict": "BLOCKED",
            "summary": "broker unavailable",
            "orders_used": 0,
            "orders_cap": 4,
        },
    )

    sample = observer._member(
        CohortBatchLaunchMemberPin(strategy_instance_id="a", run_id="run-a", roll_call_offer_id="offer"),
        {"a": [{"run_id": "run-a", "run_dir": str(run_dir)}]},
    )

    assert sample.state == "failed"
    assert sample.reason == "RUNTIME_READINESS_BLOCKED"


def _retry_coordinator(tmp_path, roll_call):
    async def _unused_start(_run_id, _request):
        raise AssertionError("start_run must not be called during offer resolution")

    async def _unused_posture(_account_id):
        raise AssertionError("posture must not be called during offer resolution")

    return CohortLaunchCoordinator(
        artifacts_root=tmp_path,
        live_runs_root=tmp_path / "runs",
        run_roll_call=roll_call,
        start_run=_unused_start,
        visible_runs_by_instance=lambda _root: {},
        run_account_id=lambda _run_dir: None,
        run_live_config=lambda _run_dir: {},
        start_request_for_run=lambda _run_dir: None,
        target_account_posture=_unused_posture,
        now_ms=lambda: 0,
        evidence_samplers=CohortEvidenceSamplerRegistry(),
        launch_schedulers=CohortLaunchSchedulerRegistry(),
    )


def test_slot_preflight_retries_a_transient_roll_call_miss(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cohort_launch, "_SLOT_PREFLIGHT_RETRY_DELAY_S", 0)
    slot = CohortBatchLaunchMemberSchedule(
        strategy_instance_id="bot-nvda", run_id="run-nvda", scheduled_start_at_ms=0, start_request={}
    )
    offer = BotRollCallOffer(
        offer_id="offer-nvda",
        strategy_instance_id="bot-nvda",
        run_id="run-nvda",
        session_date="2026-07-20",
        issued_at_ms=0,
        expires_at_ms=9_999_999,
    )
    calls = {"n": 0}

    async def roll_call() -> BotRollCallResponse:
        calls["n"] += 1
        # The pinned member is omitted on the first two slot roll calls (a
        # transient eligibility flip) and only appears on the third.
        offers = [offer] if calls["n"] >= 3 else []
        return BotRollCallResponse(summary=BotRollCallSummary(ready=len(offers)), offers=offers)

    coordinator = _retry_coordinator(tmp_path, roll_call)
    resolved = asyncio.run(coordinator._resolve_scheduled_offer(slot))

    assert resolved is not None
    assert resolved.offer_id == "offer-nvda"
    assert calls["n"] == 3


def test_slot_preflight_gives_up_after_max_attempts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cohort_launch, "_SLOT_PREFLIGHT_RETRY_DELAY_S", 0)
    slot = CohortBatchLaunchMemberSchedule(
        strategy_instance_id="bot-nvda", run_id="run-nvda", scheduled_start_at_ms=0, start_request={}
    )
    calls = {"n": 0}

    async def roll_call() -> BotRollCallResponse:
        calls["n"] += 1
        return BotRollCallResponse(summary=BotRollCallSummary(ready=0), offers=[])

    coordinator = _retry_coordinator(tmp_path, roll_call)
    resolved = asyncio.run(coordinator._resolve_scheduled_offer(slot))

    assert resolved is None
    assert calls["n"] == cohort_launch._SLOT_PREFLIGHT_MAX_ATTEMPTS
