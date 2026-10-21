"""Measure the frozen Options cases from acceptance to saved result (#3433).

Run only against a reviewed deployment on the 8-CPU / 16-GiB research profile.
The API preflight admits all 480 complete archived days before any run starts.
Five cold and five warm decoded-cache repetitions are separate; the OS page
cache is uncontrolled. Resource peaks are sampled observations at one-second
intervals. This command changes no worker limits and never qualifies capacity.
The control secret is read from DATA_PLANE_CONTROL_SECRET, never an argument.

``cases --output cases.json`` exports the exact delegated benchmark choices.
``run`` appends measurement receipts; ``report`` validates the complete matrix.
Reports belong on #3433 and are never merged as one-off repository notes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx

from app.broker.fleet.internal_http import enforce_private_http_target
from app.options_backtest.benchmark import (
    BENCHMARK_REPETITIONS,
    RESEARCH_PROFILE_CPUS,
    RESEARCH_PROFILE_MEMORY_BYTES,
    BenchmarkCase,
    BenchmarkObservation,
    benchmark_cases,
    summarize_measurements,
)
from app.schemas.options_benchmark import BenchmarkResourcesResponse, BenchmarkResultResponse
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.atomic_file import atomic_write_bytes

logger = logging.getLogger(__name__)
_PREFIX = "/api/jobs-internal/options-backtest"
_PHASES = {"download": "fetch", "input_freeze": "freeze"}


async def _preflight(client: httpx.AsyncClient, base: str, case: BenchmarkCase, *, timeout_seconds: int = 1800) -> str:
    response = await client.post(base + _PREFIX + "/benchmark-preflight", json={"case": case.name},
                                 timeout=httpx.Timeout(30, read=timeout_seconds))
    response.raise_for_status()
    body = response.json()
    if body["case_digest"] != case.digest:
        raise ValueError("deployed benchmark case differs from the frozen local choice")
    if body["sessions"] != len(case.session_open_ms) or body["incomplete_session_ms"]:
        raise ValueError(f"{case.name}: {len(body['incomplete_session_ms'])} sessions are not fully archived; no benchmark job was started")
    return str(body["inventory_digest"])


def _require_profile(sample: BenchmarkResourcesResponse) -> None:
    if (sample.cgroup_memory_limit_bytes != RESEARCH_PROFILE_MEMORY_BYTES or sample.cpu_period_us is None
            or sample.cpu_quota_us != RESEARCH_PROFILE_CPUS * sample.cpu_period_us):
        raise ValueError("benchmark requires the unchanged 8-CPU / 16-GiB research cgroup profile")


async def _measure(
    client: httpx.AsyncClient, *, api: str, runtime: str, operational: str,
    case: BenchmarkCase, mode: str, qualified_widths: list[int], timeout_seconds: int,
) -> BenchmarkObservation:
    inventory = await _preflight(client, api, case, timeout_seconds=timeout_seconds)
    initial = await client.get(runtime + _PREFIX + "/benchmark-resources")
    initial.raise_for_status()
    samples = [BenchmarkResourcesResponse.model_validate(initial.json())]
    _require_profile(samples[0])
    if samples[0].worker_width not in qualified_widths:
        raise ValueError("runtime Options width has no qualification evidence; no job was started")
    job_id = "benchmark_" + uuid4().hex
    response = await client.post(api + _PREFIX, json={
        "job_id": job_id, "setup": case.setup.model_dump(mode="json"),
        "start_ms": case.start_ms, "end_ms": case.end_ms, "benchmark_cache_mode": mode,
    })
    response.raise_for_status()
    run_id = response.json()["run_id"]
    latency = []
    deadline = time.monotonic() + timeout_seconds
    saved = None
    while time.monotonic() < deadline:
        started = time.perf_counter()
        operational_response = await client.get(operational)
        operational_response.raise_for_status()
        latency.append(max(0, int((time.perf_counter() - started) * 1000)))
        resource_response = await client.get(runtime + _PREFIX + "/benchmark-resources")
        resource_response.raise_for_status()
        sample = BenchmarkResourcesResponse.model_validate(resource_response.json())
        _require_profile(sample)
        if sample.runtime_instance != samples[0].runtime_instance:
            raise ValueError("research runtime restarted during the measured run")
        samples.append(sample)
        response = await client.get(runtime + _PREFIX + f"/benchmark-result/{run_id}")
        if response.status_code == 200:
            saved = BenchmarkResultResponse.model_validate(response.json())
            break
        if response.status_code != 409:
            response.raise_for_status()
        status = await client.get(api + f"/api/research/options-backtest/runs/{run_id}")
        status.raise_for_status()
        if status.json()["status"] not in ("running", "complete"):
            raise ValueError(f"benchmark run {run_id} ended {status.json()['status']}")
        await asyncio.sleep(1)
    if saved is None:
        raise TimeoutError(f"benchmark run {run_id} has no saved result with complete timing evidence")
    if await _preflight(client, api, case, timeout_seconds=timeout_seconds) != inventory:
        raise ValueError("archived input inventory changed during the measured run")
    metadata = saved.execution_metadata
    required = {"worker_width", "frozen_input_digest", "execution_identity_digest", "cache_epoch", "cache_bytes_peak",
                "cache_hits", "cache_misses"}
    if not required <= metadata.keys() or (mode == "cold" and not metadata["cache_epoch"]):
        raise ValueError("saved result lacks the required execution/cache measurement identity")
    width = int(metadata["worker_width"])
    if width not in qualified_widths:
        raise ValueError("runtime used an Options width without qualification evidence")
    phases: dict[str, int] = {}
    for span in saved.phase_timings:
        # Decode work is nested inside chain replay. Keep it visible without
        # adding every parallel chain's replay to the wall-clock replay span.
        if span.scope == "run" or span.phase == "admit_decode":
            phase = _PHASES.get(span.phase, span.phase)
            phases[phase] = phases.get(phase, 0) + span.duration_ms
    memory = [sample.cgroup_memory_bytes for sample in samples]
    return BenchmarkObservation(
        case_digest=case.digest, archive_inventory_digest=inventory,
        frozen_input_digest=metadata["frozen_input_digest"], result_digest=saved.result_digest,
        code_revision=metadata["execution_identity_digest"], worker_width=width, cache_mode=mode,
        runtime_instance=samples[0].runtime_instance,
        cache_generation=str(metadata["cache_epoch"]) if mode == "cold" else samples[0].runtime_instance,
        run_id=run_id, accepted_at_ms=saved.accepted_at_ms, saved_at_ms=saved.saved_at_ms,
        phase_duration_ms=phases, cache_bytes_peak=metadata["cache_bytes_peak"],
        cache_hits=metadata["cache_hits"], cache_misses=metadata["cache_misses"],
        cgroup_memory_peak_bytes=max(memory) if all(value is not None for value in memory) else None,
        staging_bytes_peak=max(sample.frozen_input_bytes for sample in samples),
        database_connections_peak=max(sample.database_connections for sample in samples),
        operational_latency_ms=tuple(latency), profile_cpus=RESEARCH_PROFILE_CPUS,
        profile_memory_bytes=RESEARCH_PROFILE_MEMORY_BYTES,
    )


async def _run(args: argparse.Namespace) -> None:
    if args.timeout_seconds <= 0 or 1 not in args.qualified_widths or not set(args.qualified_widths) <= {1, 2, 4, 8}:
        raise ValueError("provide a positive timeout and qualified widths including the width-1 baseline")
    secret = os.environ.get("DATA_PLANE_CONTROL_SECRET", "").strip()
    if not secret:
        raise ValueError("DATA_PLANE_CONTROL_SECRET is required for benchmark dispatch and protected observations")
    for target in (args.api_url, args.runtime_url, args.operational_url):
        await asyncio.to_thread(enforce_private_http_target, target)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(headers={CONTROL_SECRET_HEADER: secret}, timeout=120, trust_env=False) as client:
        for case in benchmark_cases():
            for mode in ("cold", "warm"):
                for repetition in range(BENCHMARK_REPETITIONS):
                    observation = await _measure(client, api=args.api_url.rstrip("/"), runtime=args.runtime_url.rstrip("/"),
                        operational=args.operational_url, case=case, mode=mode,
                        qualified_widths=args.qualified_widths, timeout_seconds=args.timeout_seconds)
                    with args.output.open("a", encoding="utf-8") as output:
                        output.write(observation.model_dump_json() + "\n")
                        output.flush()
                        os.fsync(output.fileno())
                    logger.info("Benchmark repetition saved", extra={"action": "options_benchmark_repetition",
                        "case": case.name, "cache_mode": mode, "repetition": repetition + 1, "run_id": observation.run_id})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cases = commands.add_parser("cases")
    cases.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--api-url", required=True)
    run.add_argument("--runtime-url", required=True)
    run.add_argument("--operational-url", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=int, default=1800)
    run.add_argument("--qualified-widths", type=int, nargs="+", default=[1])
    report = commands.add_parser("report")
    report.add_argument("--observations", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--qualified-widths", type=int, nargs="+", default=[1])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        if args.command == "cases":
            atomic_write_bytes(args.output, json.dumps([{
                "case_digest": case.digest, "case": case.model_dump(mode="json"),
            } for case in benchmark_cases()], indent=2).encode())
        elif args.command == "run":
            asyncio.run(_run(args))
        else:
            observations = [BenchmarkObservation.model_validate_json(line)
                            for line in args.observations.read_text().splitlines() if line.strip()]
            result = {"cache_scope": "decoded worker cache; OS cache uncontrolled", "sample_interval_ms": 1000,
                      "resources": "maximum observed samples, not an unsampled kernel peak",
                      "groups": summarize_measurements(observations, qualified_widths=args.qualified_widths)}
            atomic_write_bytes(args.output, json.dumps(result, indent=2).encode())
    except (ValueError, OSError, httpx.HTTPError) as exc:
        logger.error("Benchmark refused", extra={"action": "options_benchmark_refused", "reason": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
