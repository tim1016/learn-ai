"""Tests for server-authored cohort evidence evaluation."""

import asyncio

from app.services.cohort_evidence import (
    CohortEvidenceSample,
    CohortEvidenceSampler,
    CohortMemberSample,
    evaluate_healthy_overlap,
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
