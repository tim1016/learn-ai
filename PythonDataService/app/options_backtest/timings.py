"""Operational run timings, deliberately outside every financial result (#3417).

Durations use a monotonic clock. UTC anchors describe when the phase ran;
clock adjustments cannot make elapsed work negative. Top-level phases cover
the whole run; chain/decode details may overlap and must not be summed again.
Integer-ms truncation contributes less than one ms per measured interval.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal

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


class RunTimings:
    def __init__(self, *, clock: Callable[[], int] = now_ms_utc,
                 monotonic: Callable[[], float] = time.perf_counter) -> None:
        self.clock, self.monotonic = clock, monotonic
        self.rows: list[PhaseTiming] = []
        self._start: tuple[int, float] | None = None

    def start(self) -> None:
        self._start = (self.clock(), self.monotonic())

    def seal(self) -> int:
        """Account for coordination between phases; detailed chain spans overlap."""
        if self._start is None:
            raise ValueError("run timing must start before it is sealed")
        started, tick = self._start
        finished = self.clock()
        wall_ms = max(0, int((self.monotonic() - tick) * 1000))
        covered = sum(row.duration_ms for row in self.rows if row.scope == "run")
        if covered > wall_ms:
            raise ValueError("top-level phase intervals overlap the run wall time")
        self.rows.append(PhaseTiming(phase="coordination", started_at_ms=started,
            finished_at_ms=finished, duration_ms=wall_ms - covered))
        return wall_ms

    @contextmanager
    def measure(self, phase: str, *, chain_key: str | None = None) -> Iterator[None]:
        started, tick = self.clock(), self.monotonic()
        try:
            yield
        finally:
            self.rows.append(PhaseTiming(phase=phase, started_at_ms=started, finished_at_ms=self.clock(),
                duration_ms=max(0, int((self.monotonic() - tick) * 1000)),
                scope="run" if chain_key is None else "chain", chain_key=chain_key))

    def wire(self) -> list[dict[str, object]]:
        return [row.model_dump(mode="json") for row in self.rows]
