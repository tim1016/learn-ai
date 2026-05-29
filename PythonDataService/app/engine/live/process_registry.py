"""Process registry — multi-process replacement for the single-_current
model in ``host_daemon.RunnerProcessManager``.

Keys by ``strategy_instance_id`` (per plan §16.4 Resolution 7). Owns
``Popen`` lifecycle: start / stop / list / status. Exit-code
disambiguation (clean exit 0 vs crashed N≠0) so the dispatcher can
decide whether to restart or escalate.

Engine-side wiring (FastAPI routes routing through this registry) is
a follow-up; this module is unit-testable in isolation.
"""

from __future__ import annotations


class ProcessRegistry:
    def __init__(self) -> None:
        self._managed: dict[str, object] = {}

    def list(self) -> list[object]:
        return list(self._managed.values())

    def status(self, strategy_instance_id: str) -> object | None:
        return self._managed.get(strategy_instance_id)
