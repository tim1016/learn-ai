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

from app.engine.live.process_registry import (
    AlreadyRunningError,
    ManagedProcess,
    ProcessRegistry,
)


class FakeProcess:
    """Quacks like subprocess.Popen for unit-test purposes.

    Mirrors the existing FakeProcess pattern in test_host_daemon.py so
    the migration to ProcessRegistry doesn't introduce a divergent
    test idiom.
    """

    def __init__(
        self,
        pid: int = 4242,
        *,
        exit_on_signal: int | None = None,
        exit_code_on_signal: int = 0,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.signals: list[int] = []
        self.killed = False
        self._exit_on_signal = exit_on_signal
        self._exit_code_on_signal = exit_code_on_signal

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return self.returncode

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self._exit_on_signal is not None and sig == self._exit_on_signal:
            self.returncode = self._exit_code_on_signal

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_empty_registry_lists_no_processes() -> None:
    registry = ProcessRegistry()
    assert registry.list() == []
    assert registry.status("never_registered") is None


def test_clean_exit_classified_as_intentional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal as _signal

    fake = FakeProcess(exit_on_signal=_signal.SIGTERM, exit_code_on_signal=0)
    monkeypatch.setattr(
        "app.engine.live.process_registry.subprocess.Popen",
        lambda *_args, **_kw: fake,
    )

    registry = ProcessRegistry()
    registry.start(
        strategy_instance_id="spy_ema_crossover",
        command=["python", "-m", "ema"],
        log_path=tmp_path / "ema.log",
    )
    stopped = registry.stop("spy_ema_crossover")

    assert stopped.exit_classification == "intentional"


def test_stop_sends_sigterm_and_records_ended_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal as _signal

    fake = FakeProcess(pid=4242, exit_on_signal=_signal.SIGTERM, exit_code_on_signal=0)
    monkeypatch.setattr(
        "app.engine.live.process_registry.subprocess.Popen",
        lambda *_args, **_kw: fake,
    )

    registry = ProcessRegistry()
    registry.start(
        strategy_instance_id="spy_ema_crossover",
        command=["python", "-m", "ema"],
        log_path=tmp_path / "ema.log",
    )

    stopped = registry.stop("spy_ema_crossover")

    assert _signal.SIGTERM in fake.signals
    assert stopped.state == "exited"
    assert stopped.ended_at_ms is not None
    assert stopped.exit_code == 0


def test_cannot_start_same_strategy_twice_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two competing processes under the same strategy_instance_id would
    fight for the same IBKR clientId, indicator-state sidecar, and
    live-state sidecar. The registry refuses the second start.
    """
    fake = FakeProcess(pid=4242)
    monkeypatch.setattr(
        "app.engine.live.process_registry.subprocess.Popen",
        lambda *_args, **_kw: fake,
    )

    registry = ProcessRegistry()
    registry.start(
        strategy_instance_id="spy_ema_crossover",
        command=["python", "-m", "ema"],
        log_path=tmp_path / "ema.log",
    )

    with pytest.raises(AlreadyRunningError) as excinfo:
        registry.start(
            strategy_instance_id="spy_ema_crossover",
            command=["python", "-m", "ema"],
            log_path=tmp_path / "ema.log",
        )
    assert excinfo.value.strategy_instance_id == "spy_ema_crossover"
    # First process untouched.
    assert registry.status("spy_ema_crossover").pid == 4242


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
