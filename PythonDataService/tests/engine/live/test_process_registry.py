"""Tests for ProcessRegistry — multi-process replacement for the
existing single-_current model in RunnerProcessManager.

Engine-side wiring (FastAPI routes in host_daemon.py routing through
this registry) is a follow-up; this module owns the in-process
lifecycle bookkeeping.
"""

from __future__ import annotations

from app.engine.live.process_registry import ProcessRegistry


def test_empty_registry_lists_no_processes() -> None:
    registry = ProcessRegistry()
    assert registry.list() == []
    assert registry.status("never_registered") is None
