"""Tests for ProcessRegistry — multi-process replacement for the
existing single-_current model in RunnerProcessManager.

Engine-side wiring (FastAPI routes in host_daemon.py routing through
this registry) is a follow-up; this module owns the in-process
lifecycle bookkeeping.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.engine.live.process_registry import ManagedProcess, ProcessRegistry


class FakeProcess:
    """Quacks like subprocess.Popen for unit-test purposes.

    Mirrors the existing FakeProcess pattern in test_host_daemon.py so
    the migration to ProcessRegistry doesn't introduce a divergent
    test idiom.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_empty_registry_lists_no_processes() -> None:
    registry = ProcessRegistry()
    assert registry.list() == []
    assert registry.status("never_registered") is None


def test_two_distinct_strategies_register_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-process replacement for the single-_current model. Two
    different strategy_instance_ids must coexist; list() returns both.
    """
    fakes = iter([FakeProcess(pid=100), FakeProcess(pid=200)])
    monkeypatch.setattr(
        "app.engine.live.process_registry.subprocess.Popen",
        lambda *_args, **_kw: next(fakes),
    )

    registry = ProcessRegistry()
    ema = registry.start(
        strategy_instance_id="spy_ema_crossover",
        command=["python", "-m", "ema"],
        log_path=tmp_path / "ema.log",
    )
    vwap = registry.start(
        strategy_instance_id="spy_vwap_reversion_1min",
        command=["python", "-m", "vwap"],
        log_path=tmp_path / "vwap.log",
    )

    assert ema.pid == 100
    assert vwap.pid == 200
    assert {entry.strategy_instance_id for entry in registry.list()} == {
        "spy_ema_crossover",
        "spy_vwap_reversion_1min",
    }


def test_start_registers_a_managed_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeProcess(pid=4242)
    monkeypatch.setattr(
        "app.engine.live.process_registry.subprocess.Popen",
        lambda *_args, **_kw: fake,
    )

    registry = ProcessRegistry()
    managed = registry.start(
        strategy_instance_id="spy_ema_crossover",
        command=["python", "-m", "app.engine.live.run", "start"],
        log_path=tmp_path / "spy_ema_crossover.log",
    )

    assert isinstance(managed, ManagedProcess)
    assert managed.strategy_instance_id == "spy_ema_crossover"
    assert managed.pid == 4242
    assert managed.state == "running"
    assert registry.status("spy_ema_crossover") is managed
    assert registry.list() == [managed]
