"""Pure authoritative evaluation for durable cohort-validation samples."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

SampleVerdict = Literal["healthy", "failed", "unknown"]


@dataclass(frozen=True)
class CohortMemberSample:
    """One member's server-observed state at one expected sampler tick."""

    strategy_instance_id: str
    run_id: str | None
    state: SampleVerdict
    reason: str | None = None
    orders_used: int | None = None
    orders_cap: int | None = None


@dataclass(frozen=True)
class CohortEvidenceSample:
    """Durable account and member proof at a scheduled five-second tick."""

    expected_at_ms: int
    observed_at_ms: int | None
    account_truth: SampleVerdict
    fleet: SampleVerdict
    members: tuple[CohortMemberSample, ...]


@dataclass(frozen=True)
class CohortOverlapEvidence:
    """Server-authored result for the exact receipt membership."""

    verdict: SampleVerdict
    healthy_overlap_ms: int
    reason: str | None


Observation = Callable[[int], Awaitable[CohortEvidenceSample]]
PersistSample = Callable[[CohortEvidenceSample], Awaitable[None]]


class CohortEvidenceSampler:
    """Server-owned fixed-cadence sampler; callers cannot substitute UI polls."""

    def __init__(
        self,
        *,
        cadence_ms: int,
        now_ms: Callable[[], int],
        observe: Observation,
        persist: PersistSample,
        first_expected_at_ms: int | None = None,
        last_expected_at_ms: int | None = None,
    ) -> None:
        if cadence_ms <= 0:
            raise ValueError("cadence_ms must be positive")
        self._cadence_ms = cadence_ms
        self._now_ms = now_ms
        self._observe = observe
        self._persist = persist
        self._next_expected_at_ms = first_expected_at_ms
        self._last_expected_at_ms = last_expected_at_ms

    async def sample_once(self) -> CohortEvidenceSample:
        """Persist exactly one scheduled tick, including a late/missing result."""

        expected_at_ms = self._next_expected_at_ms
        if expected_at_ms is None:
            expected_at_ms = self._now_ms()
        self._next_expected_at_ms = expected_at_ms + self._cadence_ms
        try:
            sample = await self._observe(expected_at_ms)
        except Exception:
            sample = CohortEvidenceSample(
                expected_at_ms=expected_at_ms,
                observed_at_ms=None,
                account_truth="unknown",
                fleet="unknown",
                members=(),
            )
        if sample.expected_at_ms != expected_at_ms:
            raise ValueError("sampler observation changed the scheduled tick")
        await self._persist(sample)
        return sample

    async def run(self, stop: asyncio.Event) -> None:
        """Continue sampling against the durable expected-tick schedule."""

        while not stop.is_set():
            if (
                self._next_expected_at_ms is not None
                and self._last_expected_at_ms is not None
                and self._next_expected_at_ms > self._last_expected_at_ms
            ):
                return
            await self.sample_once()
            expected_at_ms = self._next_expected_at_ms
            assert expected_at_ms is not None
            delay_seconds = max(0, expected_at_ms - self._now_ms()) / 1_000
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay_seconds)
            except TimeoutError:
                continue


class CohortEvidenceSamplerRegistry:
    """Owns cohort sampler tasks for this API process and stops them cleanly."""

    def __init__(self) -> None:
        self._samplers: dict[str, tuple[asyncio.Event, asyncio.Task[None]]] = {}

    def start(self, cohort_id: str, sampler: CohortEvidenceSampler) -> None:
        """Start one sampler once; a duplicate launch cannot create a second writer."""

        if cohort_id in self._samplers:
            return
        stop = asyncio.Event()
        self._samplers[cohort_id] = (stop, asyncio.create_task(sampler.run(stop)))

    async def stop_all(self) -> None:
        """Fence every task before application shutdown tears down dependencies."""

        samplers = tuple(self._samplers.values())
        self._samplers.clear()
        for stop, _task in samplers:
            stop.set()
        if samplers:
            await asyncio.gather(*(task for _stop, task in samplers), return_exceptions=True)


_SAMPLER_REGISTRY = CohortEvidenceSamplerRegistry()


def get_cohort_evidence_sampler_registry() -> CohortEvidenceSamplerRegistry:
    """Return the process-owned sampler registry used by live cohort launches."""

    return _SAMPLER_REGISTRY


def evaluate_healthy_overlap(
    samples: tuple[CohortEvidenceSample, ...],
    *,
    member_strategy_instance_ids: tuple[str, ...],
    cadence_ms: int,
    evaluated_at_ms: int | None = None,
) -> CohortOverlapEvidence:
    """Intersect healthy exact-member ticks; unknown and gaps fail closed."""

    if cadence_ms <= 0:
        raise ValueError("cadence_ms must be positive")
    expected_members = set(member_strategy_instance_ids)
    if not expected_members:
        return CohortOverlapEvidence("unknown", 0, "COHORT_MEMBERS_MISSING")
    if not samples:
        return CohortOverlapEvidence("unknown", 0, "COHORT_EVIDENCE_MISSING")

    healthy_ticks = 0
    previous_expected_at_ms: int | None = None
    for sample in samples:
        if previous_expected_at_ms is not None and sample.expected_at_ms - previous_expected_at_ms != cadence_ms:
            return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "COHORT_OBSERVATION_GAP")
        previous_expected_at_ms = sample.expected_at_ms
        if sample.observed_at_ms is None or sample.observed_at_ms - sample.expected_at_ms > cadence_ms:
            return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "COHORT_SAMPLE_LATE_OR_MISSING")
        if sample.account_truth != "healthy" or sample.fleet != "healthy":
            return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "COHORT_ACCOUNT_PROOF_FAILED")
        members = {member.strategy_instance_id: member for member in sample.members}
        if set(members) != expected_members:
            return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "COHORT_MEMBER_OBSERVATION_MISSING")
        if any(member.state != "healthy" for member in members.values()):
            return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "COHORT_MEMBER_HEALTH_FAILED")
        if any(member.orders_used is None or member.orders_cap is None for member in members.values()):
            return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "RUNTIME_COUNTERS_MISSING")
        healthy_ticks += 1
    if evaluated_at_ms is not None and evaluated_at_ms - previous_expected_at_ms >= cadence_ms:
        return CohortOverlapEvidence("failed", healthy_ticks * cadence_ms, "COHORT_OBSERVATION_GAP")
    return CohortOverlapEvidence("healthy", healthy_ticks * cadence_ms, None)
