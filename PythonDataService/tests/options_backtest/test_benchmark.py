"""Benchmark receipts cannot turn changed results or incomplete evidence into a speed claim."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options_backtest.benchmark import (
    BENCHMARK_END_MS,
    RESEARCH_PROFILE_MEMORY_BYTES,
    BenchmarkObservation,
    benchmark_cases,
    summarize_measurements,
)
from app.utils.session_anchors import et_date_at_ms


def observations() -> list[BenchmarkObservation]:
    result = []
    for case in benchmark_cases():
        for mode in ("cold", "warm"):
            for duration in (50_000, 60_000, 90_000, 20_000, 70_000):
                result.append(BenchmarkObservation(
                    case_digest=case.digest, frozen_input_digest="a" * 64, result_digest="b" * 64,
                    archive_inventory_digest="d" * 64,
                    code_revision="revision", worker_width=1, cache_mode=mode,
                    runtime_instance=f"cold-{len(result)}" if mode == "cold" else "warm-1",
                    cache_generation=f"cold-{len(result)}" if mode == "cold" else "warm-1",
                    run_id=len(result) + 1, accepted_at_ms=1_000_000, saved_at_ms=1_000_000 + duration,
                    phase_duration_ms={"check": 100, "fetch": 0, "freeze": 200, "queue": 300,
                                       "admit_decode": 400, "replay": 500, "save": 100},
                    cache_bytes_peak=900, cgroup_memory_peak_bytes=1_000_000,
                    cache_hits=0 if mode == "cold" else 480, cache_misses=480 if mode == "cold" else 0,
                    staging_bytes_peak=1000, database_connections_peak=8, operational_latency_ms=(10, 20),
                    profile_cpus=8, profile_memory_bytes=RESEARCH_PROFILE_MEMORY_BYTES,
                ))
    return result


def test_cases_cover_480_real_sessions_and_eight_leg_sold_cover_workload() -> None:
    typical, stress = benchmark_cases()
    first = et_date_at_ms(typical.session_open_ms[0])
    assert type(BENCHMARK_END_MS) is int and typical.session_open_ms[-1] == BENCHMARK_END_MS
    assert typical.session_open_ms == tuple(session_open_ms_utc(day) for day in expected_sessions(first, et_date_at_ms(BENCHMARK_END_MS)))
    assert len(typical.session_open_ms) == len(stress.session_open_ms) == 480
    assert len(typical.setup.legs) == 2
    assert len(stress.setup.legs) == 8
    assert all(leg.reentry_on_stop is not None for leg in stress.setup.legs)
    for right in ("call", "put"):
        assert sum(leg.contracts for leg in stress.setup.legs if leg.right == right and leg.side == "buy") == 2
        assert sum(leg.contracts for leg in stress.setup.legs if leg.right == right and leg.side == "sell") == 2
    assert typical.setup.fees.per_contract_usd == stress.setup.fees.per_contract_usd == Decimal("0.65")
    assert typical.digest != stress.digest
    assert (typical.digest, stress.digest) == (
        "2e393eacdd0ad7f5ee5a6b693c7a2d4158e20be7e86d4b65444f0d4959232eec",
        "5b9fba1af60d3ed71d4003baa2874e387fe71ea686c176d409799744facdd4b7",
    )


def test_report_separates_modes_and_does_not_qualify_capacity() -> None:
    reports = summarize_measurements(observations())
    assert len(reports) == 4
    assert {(r["case"], r["cache_mode"]) for r in reports} == {
        (case.name, mode) for case in benchmark_cases() for mode in ("cold", "warm")
    }
    for report in reports:
        assert report["median_end_to_end_ms"] == 60_000
        assert report["worst_end_to_end_ms"] == 90_000
        assert report["worst_operational_latency_ms"] == 20
        assert report["target_met"] is True
        assert report["capacity_qualified"] is False
        assert report["instrumentation_complete"] is True


@pytest.mark.parametrize("mutation", ["changed_results", "changed_inputs", "duplicate", "missing_repetition",
                                          "unqualified_width", "fake_cold", "missing_phase", "wrong_profile"])
def test_report_refuses_unusable_evidence(mutation: str) -> None:
    samples = observations()
    updates = {
        "changed_results": {"result_digest": "c" * 64},
        "changed_inputs": {"frozen_input_digest": "c" * 64},
        "unqualified_width": {"worker_width": 4},
        "fake_cold": {"cache_generation": samples[1].cache_generation},
        "missing_phase": {"phase_duration_ms": {"replay": 5}},
        "wrong_profile": {"profile_cpus": 16},
    }
    if mutation == "duplicate":
        samples[0] = samples[1]
    elif mutation == "missing_repetition":
        samples.pop()
    else:
        samples[0] = samples[0].model_copy(update=updates[mutation])
    with pytest.raises(ValueError):
        summarize_measurements(samples)


def test_missing_resource_observations_remain_unknown() -> None:
    samples = [sample.model_copy(update={"cache_bytes_peak": None}) for sample in observations()]
    for report in summarize_measurements(samples):
        assert report["cache_bytes_peak"] is None
        assert report["instrumentation_complete"] is False


def test_warm_report_refuses_a_working_set_with_no_decoded_reuse() -> None:
    samples = [sample.model_copy(update={"cache_hits": 0, "cache_misses": 480})
               if sample.cache_mode == "warm" else sample for sample in observations()]
    with pytest.raises(ValueError, match=r"warm.*reuse"):
        summarize_measurements(samples)


def test_report_exposes_decoded_cache_counters() -> None:
    for report in summarize_measurements(observations()):
        if report["cache_mode"] == "warm":
            assert report["cache_hits"] == [480] * 5
            assert report["cache_misses"] == [0] * 5
        else:
            assert report["cache_hits"] == [0] * 5
            assert report["cache_misses"] == [480] * 5
