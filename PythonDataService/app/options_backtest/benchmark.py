"""Frozen Options Backtest cases and measurement receipts (#3433).

These cases exercise the production replay; they supply no alternative math.
Reports remain issue evidence, rather than checked-in timing expectations.

Formula: median and worst elapsed time are statistics.median and max over
  repetitions of one case, width, input digest and decoded-cache mode.
Reference: #3433 and #3412's performance acceptance contract.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_benchmark.py.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options_backtest.cash_ladder import DEFAULT_CASH_LADDER
from app.options_backtest.run import DEFAULT_SLIPPAGE_LADDER
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from app.research.runs.hashing import hash_payload
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_midnight_ms

BENCHMARK_SESSIONS = 480
BENCHMARK_REPETITIONS = 5
BENCHMARK_TARGET_MS = 60_000
BENCHMARK_END = date(2026, 10, 16)
RESEARCH_PROFILE_MEMORY_BYTES = 16 * 1024**3
RESEARCH_PROFILE_CPUS = 8
_Ms = Annotated[int, Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)]
_Nonnegative = Annotated[int, Field(strict=True, ge=0)]
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["spy_typical_v1", "spy_eight_leg_cover_v1"]
    setup: OptionsBacktestSetup
    session_open_ms: tuple[_Ms, ...]
    start_ms: _Ms
    end_ms: _Ms
    slippage_ladder: tuple[Decimal, ...] = DEFAULT_SLIPPAGE_LADDER
    cash_ladder: tuple[Decimal, ...] = DEFAULT_CASH_LADDER

    @property
    def digest(self) -> str:
        return hash_payload(self.model_dump(mode="json"))


def benchmark_cases() -> tuple[BenchmarkCase, BenchmarkCase]:
    """The delegated choices, frozen to 480 actual NYSE sessions ending 2026-10-16."""
    sessions = expected_sessions(date(2024, 1, 1), BENCHMARK_END)[-BENCHMARK_SESSIONS:]
    common = {
        "underlying": "SPY",
        "entry": {"anchor": "open", "offset_ms": 5 * MS_PER_MINUTE},
        "exit": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
        "slippage_percent": "0.3",
        "fees": {"kind": "per_contract", "per_contract_usd": "0.65"},
        "price_step_usd": "0.01",
    }
    typical = OptionsBacktestSetup.model_validate({
        **common, "starting_cash_usd": "10000.00", "square_off": "complete",
        "legs": [{
            "leg_id": right, "right": right, "contracts": 1,
            "expiry": {"kind": "this_week"}, "strike": {"kind": "atm"},
            "stop_loss": {"kind": "percent", "value": "25"},
            "momentum": {"kind": "percent_up", "value": "25"},
        } for right in ("call", "put")],
    })
    legs = []
    for right, sign in (("call", 1), ("put", -1)):
        for pair in (0, 1):
            for side in ("buy", "sell"):
                steps = sign * (pair + 5 if side == "buy" else pair)
                leg = {
                    "leg_id": f"{right}_{pair}_{side}", "side": side, "right": right, "contracts": 1,
                    "expiry": {"kind": "this_week"},
                    "strike": {"kind": "atm_offset", "steps": steps} if steps else {"kind": "atm"},
                    "stop_loss": {"kind": "percent", "value": "25" if side == "buy" else "50"},
                    "reentry_on_stop": {"kind": "at_once", "count": 2},
                }
                if side == "sell":
                    leg["momentum"] = {"kind": "percent_up", "value": "5"}
                    leg["reentry_on_stop"] = {"kind": "on_momentum", "count": 2}
                legs.append(leg)
    stress = OptionsBacktestSetup.model_validate({
        **common, "starting_cash_usd": "100000.00", "square_off": "partial",
        "no_reentry_after": {"anchor": "close", "offset_ms": 60 * MS_PER_MINUTE}, "legs": legs,
    })
    range_values = {
        "session_open_ms": tuple(session_open_ms_utc(day) for day in sessions),
        "start_ms": et_midnight_ms(sessions[0]),
        "end_ms": et_midnight_ms(sessions[-1] + timedelta(days=1)),
    }
    return (
        BenchmarkCase(name="spy_typical_v1", setup=typical, **range_values),
        BenchmarkCase(name="spy_eight_leg_cover_v1", setup=stress, **range_values),
    )


class BenchmarkObservation(BaseModel):
    """One measured production run; omitted instrumentation stays unknown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_digest: _Digest
    frozen_input_digest: _Digest
    result_digest: _Digest
    code_revision: str = Field(min_length=1)
    worker_width: Annotated[int, Field(strict=True, ge=1, le=8)]
    cache_mode: Literal["cold", "warm"]
    runtime_instance: str = Field(min_length=1)
    run_id: Annotated[int, Field(strict=True, ge=1)]
    accepted_at_ms: _Ms
    saved_at_ms: _Ms
    phase_duration_ms: dict[str, _Nonnegative]
    cache_bytes_peak: _Nonnegative | None = None
    cgroup_memory_peak_bytes: _Nonnegative | None = None
    staging_bytes_peak: _Nonnegative | None = None
    database_connections_peak: _Nonnegative | None = None
    operational_latency_ms: tuple[_Nonnegative, ...] = ()
    profile_cpus: Annotated[int, Field(strict=True, ge=1)]
    profile_memory_bytes: Annotated[int, Field(strict=True, ge=1)]

    @model_validator(mode="after")
    def _valid_measurement(self) -> BenchmarkObservation:
        if self.saved_at_ms < self.accepted_at_ms:
            raise ValueError("a saved result cannot precede its acceptance")
        if not self.operational_latency_ms:
            raise ValueError("each repetition needs operational latency observations")
        return self


def summarize_measurements(
    observations: Sequence[BenchmarkObservation], *, qualified_widths: Sequence[int] = (1,),
) -> list[dict[str, object]]:
    """Refuse changed results, mixed inputs or incomplete repetitions before comparing speed."""
    cases = {case.digest: case for case in benchmark_cases()}
    widths = set(qualified_widths)
    if 1 not in widths or not widths <= {1, 2, 4, 8}:
        raise ValueError("the qualified widths must include the width-1 baseline")
    groups: dict[tuple[str, int, str], list[BenchmarkObservation]] = defaultdict(list)
    identity: dict[str, tuple[str, str, str]] = {}
    run_ids: set[int] = set()
    for observation in observations:
        if observation.case_digest not in cases or observation.worker_width not in widths:
            raise ValueError("observation names an unknown case or an unqualified width")
        if observation.run_id in run_ids:
            raise ValueError("a saved run cannot count as two repetitions")
        run_ids.add(observation.run_id)
        if (observation.profile_cpus, observation.profile_memory_bytes) != (
            RESEARCH_PROFILE_CPUS, RESEARCH_PROFILE_MEMORY_BYTES,
        ):
            raise ValueError("measurement must use the 8-CPU / 16-GiB research profile")
        key = observation.case_digest
        pinned = (observation.frozen_input_digest, observation.result_digest, observation.code_revision)
        if key in identity and identity[key] != pinned:
            raise ValueError("case inputs, code or exact results changed across repetitions/widths")
        identity[key] = pinned
        groups[key, observation.worker_width, observation.cache_mode].append(observation)
    required = {(digest, width, mode) for digest in cases for width in widths for mode in ("cold", "warm")}
    if set(groups) != required or any(len(group) < BENCHMARK_REPETITIONS for group in groups.values()):
        raise ValueError("both cases need at least five cold and five warm repetitions at every qualified width")
    report: list[dict[str, object]] = []
    for (digest, width, mode), group in sorted(groups.items()):
        instances = [sample.runtime_instance for sample in group]
        if mode == "cold" and len(instances) != len(set(instances)):
            raise ValueError("cold repetitions must prove a fresh runtime/cache generation")
        if mode == "warm" and len(set(instances)) != 1:
            raise ValueError("warm repetitions must reuse one runtime/cache generation")
        required_phases = {"check", "fetch", "freeze", "queue", "admit_decode", "replay", "save"}
        if any(not required_phases <= sample.phase_duration_ms.keys() for sample in group):
            raise ValueError("a repetition is missing required phase measurements")
        measured = all(
            all(value is not None for value in (
                sample.cache_bytes_peak, sample.cgroup_memory_peak_bytes,
                sample.staging_bytes_peak, sample.database_connections_peak,
            )) for sample in group
        )
        elapsed = [sample.saved_at_ms - sample.accepted_at_ms for sample in group]
        median = statistics.median(elapsed)
        report.append({
            "case": cases[digest].name, "case_digest": digest, "input_digest": identity[digest][0],
            "result_digest": identity[digest][1], "code_revision": identity[digest][2],
            "worker_width": width, "cache_mode": mode, "repetitions": len(group),
            "median_end_to_end_ms": median, "worst_end_to_end_ms": max(elapsed),
            "median_phase_ms": {phase: statistics.median([s.phase_duration_ms[phase] for s in group])
                                for phase in sorted(required_phases)},
            "cache_bytes_peak": _peak(group, "cache_bytes_peak"),
            "cgroup_memory_peak_bytes": _peak(group, "cgroup_memory_peak_bytes"),
            "staging_bytes_peak": _peak(group, "staging_bytes_peak"),
            "database_connections_peak": _peak(group, "database_connections_peak"),
            "worst_operational_latency_ms": max(value for s in group for value in s.operational_latency_ms),
            "target_met": median <= BENCHMARK_TARGET_MS,
            "instrumentation_complete": measured,
            "capacity_qualified": False,
        })
    return report


def _peak(samples: Sequence[BenchmarkObservation], field: str) -> int | None:
    values = [getattr(sample, field) for sample in samples]
    return max(values) if all(value is not None for value in values) else None
