"""Operational run timings, deliberately outside every financial result (#3417).

Durations use a monotonic clock. UTC anchors describe when the phase ran;
clock adjustments cannot make elapsed work negative. Top-level phases cover
the whole run; chain/decode details may overlap and must not be summed again.
Integer-ms truncation contributes less than one ms per measured interval.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import now_ms_utc


class PhaseTiming(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    phase: str
    started_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    finished_at_ms: int = Field(ge=0, le=MAX_TIMESTAMP_MS)
    duration_ms: int = Field(ge=0)
    scope: Literal["run", "chain"] = "run"
    chain_key: str | None = None


class ReplayObservations(BaseModel):
    """What a pooled replay of frozen inputs measured: its identities and its workers' decoded-cache use."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    frozen_input_digest: str
    execution_identity_digest: str
    runtime_boot_generation: int
    worker_width: int = Field(ge=1)
    # The cold benchmark run's cache generation; None for a warm run.
    cache_epoch: str | None
    cache_hits: int = Field(ge=0)
    cache_misses: int = Field(ge=0)
    # The most decoded-cache bytes resident at once, summed over the workers that replayed chains.
    cache_bytes_peak: int = Field(ge=0)


class RunObservations(BaseModel):
    """One run's sealed operational record: its phases, its wall time and, for a pooled replay, what that measured.

    The run-scope phases sum to ``wall_duration_ms``; chain-scope phases overlap them. Stored as the phase rows and
    one flat metadata object, which a run replayed in the data service fills with its wall time alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    phase_timings: tuple[PhaseTiming, ...]
    wall_duration_ms: int = Field(ge=0)
    replay: ReplayObservations | None = None

    def stored_metadata(self) -> dict[str, Any]:
        replay = {} if self.replay is None else self.replay.model_dump(mode="json")
        return {**replay, "wall_duration_ms": self.wall_duration_ms}

    @classmethod
    def from_stored(cls, phase_timings: Iterable[PhaseTiming], metadata: Mapping[str, Any]) -> RunObservations:
        replay = {key: value for key, value in metadata.items() if key != "wall_duration_ms"}
        return cls(phase_timings=tuple(phase_timings), wall_duration_ms=metadata["wall_duration_ms"],
                   replay=ReplayObservations.model_validate(replay) if replay else None)


class RunTimings:
    """Records one run's phases as they close, and seals them once into ``RunObservations``."""

    def __init__(self, *, clock: Callable[[], int] = now_ms_utc,
                 monotonic: Callable[[], float] = time.perf_counter) -> None:
        self.clock, self.monotonic = clock, monotonic
        self._rows: list[PhaseTiming] = []
        self._replay: ReplayObservations | None = None
        self._start: tuple[int, float] | None = None
        self._sealed: RunObservations | None = None

    @property
    def rows(self) -> tuple[PhaseTiming, ...]:
        return tuple(self._rows)

    def start(self) -> None:
        self._start = (self.clock(), self.monotonic())

    def adopt(self, rows: Iterable[PhaseTiming]) -> None:
        """Keep phases measured elsewhere: a worker's chain spans, or a preparation's closed phases."""
        self._rows.extend(rows)

    def observe_replay(self, replay: ReplayObservations) -> None:
        self._replay = replay

    def seal(self) -> RunObservations:
        """Account for coordination between phases; detailed chain spans overlap. Sealing twice is the same record."""
        if self._sealed is not None:
            return self._sealed
        if self._start is None:
            raise ValueError("run timing must start before it is sealed")
        started, tick = self._start
        finished = self.clock()
        wall_ms = max(0, int((self.monotonic() - tick) * 1000))
        covered = sum(row.duration_ms for row in self._rows if row.scope == "run")
        if covered > wall_ms:
            raise ValueError("top-level phase intervals overlap the run wall time")
        self._rows.append(PhaseTiming(phase="coordination", started_at_ms=started,
            finished_at_ms=finished, duration_ms=wall_ms - covered))
        self._sealed = RunObservations(phase_timings=tuple(self._rows), wall_duration_ms=wall_ms, replay=self._replay)
        return self._sealed

    @contextmanager
    def measure(self, phase: str, *, chain_key: str | None = None) -> Iterator[Callable[[str], None]]:
        """Measure one phase. The yielded ``lap(next_phase)`` closes it and opens ``next_phase`` on one clock sample,
        from any thread; a lap to the phase already open, or after the measure ended, does nothing."""
        lock = threading.Lock()
        current: tuple[str, int, float] | None = (phase, self.clock(), self.monotonic())

        def close(opened: tuple[str, int, float], finished: int, tick: float) -> None:
            name, started, began = opened
            self._rows.append(PhaseTiming(phase=name, started_at_ms=started, finished_at_ms=finished,
                duration_ms=max(0, int((tick - began) * 1000)),
                scope="run" if chain_key is None else "chain", chain_key=chain_key))

        def lap(next_phase: str) -> None:
            nonlocal current
            with lock:
                if current is not None and next_phase != current[0]:
                    finished, tick = self.clock(), self.monotonic()
                    close(current, finished, tick)
                    current = (next_phase, finished, tick)

        try:
            yield lap
        finally:
            with lock:
                opened, current = current, None
                if opened is not None:
                    close(opened, self.clock(), self.monotonic())

    def wire(self) -> list[dict[str, object]]:
        return [row.model_dump(mode="json") for row in self._rows]
