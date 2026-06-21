"""PRD #619-B — shared fake clock fixture for asyncio tests.

Periodic-write tests (``EngineRuntimePublisher``, ``DaemonLeaseWriter``,
and future watchdog/freshness writers) all need a deterministic monotonic
clock the test body can advance synchronously between ``asyncio.sleep``
yields. Extracting the fixture here keeps the test files focused on the
behaviour under test and gives every new periodic writer one canonical
clock seam.
"""

from __future__ import annotations

from typing import Protocol


class TickableClock(Protocol):
    """A callable that returns the current simulated ms-UTC and exposes
    ``.tick(delta_ms)`` to advance it."""

    def __call__(self) -> int: ...

    def tick(self, delta_ms: int = 1) -> None: ...


def make_test_clock(start_ms: int) -> TickableClock:
    """Build a stateful fake clock starting at ``start_ms``.

    Returns a callable; call it to read the simulated ms. Call
    ``.tick(delta_ms)`` to advance the simulated time (default 1ms).

    Example::

        now = make_test_clock(1_700_000_000_000)
        assert now() == 1_700_000_000_000
        now.tick(500)
        assert now() == 1_700_000_000_500
    """
    state = {"ms": start_ms}

    def _now() -> int:
        return state["ms"]

    def _tick(delta_ms: int = 1) -> None:
        state["ms"] += delta_ms

    _now.tick = _tick  # type: ignore[attr-defined]
    return _now  # type: ignore[return-value]
