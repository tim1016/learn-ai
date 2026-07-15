"""Tests for server-authored cohort evidence evaluation."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.engine.live.account_artifacts import (
    CohortBatchLaunchMemberPin,
    CohortBatchLaunchReceipt,
    record_cohort_batch_launch_receipt,
)
from app.engine.live.live_state_sidecar import LiveStateEnvelope
from app.services.cohort_batch_launch import CohortBatchLaunchService, parse_cohort_evidence_sample
from app.services.cohort_evidence import (
    CohortEvidenceSample,
    CohortEvidenceSampler,
    CohortEvidenceSamplerRegistry,
    CohortMemberSample,
    evaluate_healthy_overlap,
)
from app.services.cohort_evidence_runtime import CohortEvidenceRuntimeObserver
from app.services.cohort_launch import resume_open_cohort_evidence_samplers


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
