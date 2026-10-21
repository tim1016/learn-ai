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
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc
from app.options_backtest.cash_ladder import DEFAULT_CASH_LADDER
from app.options_backtest.run import DEFAULT_SLIPPAGE_LADDER
from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup
from app.research.runs.hashing import hash_payload
from app.schemas.options_benchmark import BenchmarkCaseName, BenchmarkResourcesResponse, BenchmarkResultResponse
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms, et_midnight_ms

BENCHMARK_SESSIONS = 480
BENCHMARK_REPETITIONS = 5
BENCHMARK_TARGET_MS = 60_000
BENCHMARK_END_MS = 1_792_157_400_000
RESEARCH_PROFILE_MEMORY_BYTES = 16 * 1024**3
RESEARCH_PROFILE_CPUS = 8
#: The run's top-level phases and the decode nested in its chains, named as the run records them.
REQUIRED_PHASES = frozenset({"check", "download", "input_freeze", "queue", "admit_decode", "replay", "save"})
CacheMode = Literal["cold", "warm"]
_Ms = Annotated[int, Field(strict=True, ge=0, le=MAX_TIMESTAMP_MS)]
_Nonnegative = Annotated[int, Field(strict=True, ge=0)]
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: BenchmarkCaseName
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
    sessions = expected_sessions(date(2024, 1, 1), et_date_at_ms(BENCHMARK_END_MS))[-BENCHMARK_SESSIONS:]
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
    archive_inventory_digest: _Digest
    result_digest: _Digest
    code_revision: str = Field(min_length=1)
    worker_width: Annotated[int, Field(strict=True, ge=1, le=8)]
    cache_mode: CacheMode
    runtime_instance: str = Field(min_length=1)
    #: The generation a cold run cleared each worker's decoded cache into; a warm run reuses its runtime's and has none.
    cache_epoch: str | None = Field(default=None, min_length=1)
    run_id: Annotated[int, Field(strict=True, ge=1)]
    accepted_at_ms: _Ms
    saved_at_ms: _Ms
    phase_duration_ms: dict[str, _Nonnegative]
    cache_hits: _Nonnegative
    cache_misses: _Nonnegative
    cache_bytes_peak: _Nonnegative | None = None
    cgroup_memory_peak_bytes: _Nonnegative | None = None
    staging_bytes_peak: _Nonnegative | None = None
    database_connections_peak: _Nonnegative | None = None
    operational_latency_ms: tuple[_Nonnegative, ...] = ()

    @model_validator(mode="after")
    def _valid_measurement(self) -> BenchmarkObservation:
        if self.saved_at_ms < self.accepted_at_ms:
            raise ValueError("a saved result cannot precede its acceptance")
        if not self.operational_latency_ms:
            raise ValueError("each repetition needs operational latency observations")
        if (self.cache_mode == "cold") != (self.cache_epoch is not None):
            raise ValueError("a cold repetition needs the cache epoch it cleared into; a warm one has none")
        if self.cache_mode == "warm" and self.cache_hits == 0:
            raise ValueError("warm repetitions must demonstrate decoded cache reuse")
        return self


class BenchmarkGroupReport(BaseModel):
    """One case, width and cache mode, over its repetitions; never a capacity qualification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case: BenchmarkCaseName
    case_digest: _Digest
    input_digest: _Digest
    result_digest: _Digest
    code_revision: str
    worker_width: int
    cache_mode: CacheMode
    repetitions: int
    median_end_to_end_ms: float
    worst_end_to_end_ms: int
    median_phase_ms: dict[str, float]
    cache_bytes_peak: int | None
    cache_hits: list[int]
    cache_misses: list[int]
    cgroup_memory_peak_bytes: int | None
    staging_bytes_peak: int | None
    database_connections_peak: int | None
    worst_operational_latency_ms: int
    #: Unknown while any resource peak is unknown.
    target_met: bool | None
    instrumentation_complete: bool
    capacity_qualified: Literal[False] = False


def observe_repetition(
    *, case: BenchmarkCase, mode: CacheMode, inventory_digest: str, saved: BenchmarkResultResponse,
    samples: Sequence[BenchmarkResourcesResponse], operational_latency_ms: Sequence[int],
) -> BenchmarkObservation:
    """One repetition's receipt, from its saved run and the resource samples taken while it ran.

    Decode work is nested inside chain replay: its chain spans are summed, and
    the other chain spans are left out so parallel chains do not add to the
    run's wall-clock replay span.
    """
    phases: dict[str, int] = {}
    for span in saved.phase_timings:
        if span.scope == "run" or span.phase == "admit_decode":
            phases[span.phase] = phases.get(span.phase, 0) + span.duration_ms
    replay = saved.replay
    return BenchmarkObservation(
        case_digest=case.digest, archive_inventory_digest=inventory_digest,
        frozen_input_digest=replay.frozen_input_digest, result_digest=saved.result_digest,
        code_revision=replay.execution_identity_digest, worker_width=replay.worker_width, cache_mode=mode,
        runtime_instance=samples[0].runtime_instance, cache_epoch=replay.cache_epoch,
        run_id=saved.run_id, accepted_at_ms=saved.accepted_at_ms, saved_at_ms=saved.saved_at_ms,
        phase_duration_ms=phases, cache_bytes_peak=replay.cache_bytes_peak,
        cache_hits=replay.cache_hits, cache_misses=replay.cache_misses,
        cgroup_memory_peak_bytes=_peak(sample.cgroup_memory_bytes for sample in samples),
        staging_bytes_peak=max(sample.frozen_input_bytes for sample in samples),
        database_connections_peak=max(sample.database_connections for sample in samples),
        operational_latency_ms=tuple(operational_latency_ms),
    )


def require_qualified_widths(widths: Iterable[int]) -> frozenset[int]:
    """The worker widths a measurement may use: the width-1 baseline, and only widths of 1, 2, 4 or 8."""
    qualified = frozenset(widths)
    if 1 not in qualified or not qualified <= {1, 2, 4, 8}:
        raise ValueError("the qualified widths must include the width-1 baseline and be 1, 2, 4 or 8")
    return qualified


def summarize_measurements(
    observations: Sequence[BenchmarkObservation], *, qualified_widths: Iterable[int] = (1,),
) -> list[BenchmarkGroupReport]:
    """Refuse changed results, mixed inputs or incomplete repetitions before comparing speed."""
    cases = {case.digest: case for case in benchmark_cases()}
    qualified = require_qualified_widths(qualified_widths)
    groups: dict[tuple[str, int, str], list[BenchmarkObservation]] = defaultdict(list)
    identity: dict[str, tuple[str, str, str, str]] = {}
    run_ids: set[int] = set()
    for observation in observations:
        if observation.case_digest not in cases or observation.worker_width not in qualified:
            raise ValueError("observation names an unknown case or an unqualified width")
        if observation.run_id in run_ids:
            raise ValueError("a saved run cannot count as two repetitions")
        run_ids.add(observation.run_id)
        key = observation.case_digest
        pinned = (observation.frozen_input_digest, observation.result_digest, observation.code_revision,
                  observation.archive_inventory_digest)
        if key in identity and identity[key] != pinned:
            raise ValueError("case inputs, code or exact results changed across repetitions/widths")
        identity[key] = pinned
        groups[key, observation.worker_width, observation.cache_mode].append(observation)
    required = {(digest, width, mode) for digest in cases for width in qualified for mode in ("cold", "warm")}
    if set(groups) != required or any(len(group) < BENCHMARK_REPETITIONS for group in groups.values()):
        raise ValueError("both cases need at least five cold and five warm repetitions at every qualified width")
    report: list[BenchmarkGroupReport] = []
    for (digest, width, mode), group in sorted(groups.items()):
        if mode == "cold" and len({sample.cache_epoch for sample in group}) != len(group):
            raise ValueError("cold repetitions must each clear into a fresh cache epoch")
        if mode == "warm" and len({sample.runtime_instance for sample in group}) != 1:
            raise ValueError("the runtime restarted during warm measurements")
        if any(not sample.phase_duration_ms.keys() >= REQUIRED_PHASES for sample in group):
            raise ValueError("a repetition is missing required phase measurements")
        peaks = {field: _peak(getattr(sample, field) for sample in group) for field in (
            "cache_bytes_peak", "cgroup_memory_peak_bytes", "staging_bytes_peak", "database_connections_peak")}
        measured = None not in peaks.values()
        elapsed = [sample.saved_at_ms - sample.accepted_at_ms for sample in group]
        median = statistics.median(elapsed)
        report.append(BenchmarkGroupReport(
            case=cases[digest].name, case_digest=digest, input_digest=identity[digest][0],
            result_digest=identity[digest][1], code_revision=identity[digest][2],
            worker_width=width, cache_mode=mode, repetitions=len(group),
            median_end_to_end_ms=median, worst_end_to_end_ms=max(elapsed),
            median_phase_ms={phase: statistics.median([s.phase_duration_ms[phase] for s in group])
                             for phase in sorted(REQUIRED_PHASES)},
            cache_hits=[sample.cache_hits for sample in group],
            cache_misses=[sample.cache_misses for sample in group],
            worst_operational_latency_ms=max(value for s in group for value in s.operational_latency_ms),
            target_met=median <= BENCHMARK_TARGET_MS if measured else None,
            instrumentation_complete=measured, **peaks,
        ))
    return report


def _peak(values: Iterable[int | None]) -> int | None:
    """The largest sample, or ``None`` when any sample is unknown."""
    known = list(values)
    if any(value is None for value in known):
        return None
    return max(value for value in known if value is not None)
