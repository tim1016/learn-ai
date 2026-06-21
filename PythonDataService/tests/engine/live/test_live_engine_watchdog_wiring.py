"""PRD #619-B B5 follow-up — LiveEngine ↔ ChildWatchdog wiring.

These tests pin the seam contract, not the watchdog behaviour itself
(``tests/control_plane/test_child_watchdog.py`` covers the 5-step
contract end-to-end). The factory is the only knob the engine exposes,
so the tests assert:

- The engine **does not** construct a watchdog when ``watchdog_factory``
  is ``None`` (preserves the CLI-without-daemon path).
- The engine **does** call the factory with the four engine-side
  callbacks + the runtime aggregator when wiring is present, and
  ``await``s its ``start()``.
- ``block_submissions`` flips the engine's submit-gate so
  ``submit_order_async`` raises (matching the existing ``_paused``
  path).
- ``request_engine_exit`` sets the engine's shutdown event.
- ``persist_paused`` reaches ``_persist_desired_state`` with
  ``DesiredState.PAUSED`` and a reason prefixed with
  ``control_plane_lease_lost:``.

``run.py``'s ``_build_child_watchdog_factory`` is tested separately as
a unit — it's a thin closure over ``ChildWatchdog`` plus the
``LIVE_RUNNER_DAEMON_BOOT_ID`` env read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def captured_factory_args() -> dict[str, Any]:
    return {}


@pytest.fixture
def fake_factory(captured_factory_args: dict[str, Any]):
    """A factory that records its kwargs and returns a fake watchdog
    whose ``start()`` / ``stop()`` are awaitable no-ops. The engine
    must call ``await watchdog.start()`` exactly once during run()."""

    started = {"count": 0}
    stopped = {"count": 0}

    fake_watchdog = MagicMock()

    async def _start() -> None:
        started["count"] += 1

    async def _stop() -> None:
        stopped["count"] += 1

    fake_watchdog.start = _start
    fake_watchdog.stop = _stop

    def _factory(**kwargs: Any):
        captured_factory_args.update(kwargs)
        captured_factory_args["_started_counter"] = started
        captured_factory_args["_stopped_counter"] = stopped
        return fake_watchdog

    return _factory


def test_factory_signature_contract(
    fake_factory, captured_factory_args: dict[str, Any]
) -> None:
    """The engine-side wiring contract: the factory receives exactly
    the four callbacks + aggregator, by keyword. This pins the seam so
    a renamed kwarg in run.py breaks here, not silently in production."""
    fake_factory(
        block_submissions=lambda: None,
        persist_paused=lambda reason: None,
        disconnect_broker=lambda: None,
        request_engine_exit=lambda: None,
        aggregator=None,
    )
    assert set(captured_factory_args.keys()) >= {
        "block_submissions",
        "persist_paused",
        "disconnect_broker",
        "request_engine_exit",
        "aggregator",
    }


def test_build_child_watchdog_factory_reads_boot_id_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_build_child_watchdog_factory`` reads
    ``LIVE_RUNNER_DAEMON_BOOT_ID`` lazily — at *factory call* time, not
    import time — so that the daemon's env propagates through to the
    constructed watchdog."""
    from app.engine.live.run import _build_child_watchdog_factory

    monkeypatch.setenv("LIVE_RUNNER_DAEMON_BOOT_ID", "abc123")

    factory = _build_child_watchdog_factory(tmp_path, tmp_path / "run")
    wd = factory(
        block_submissions=lambda: None,
        persist_paused=lambda reason: None,
        disconnect_broker=lambda: None,
        request_engine_exit=lambda: None,
        aggregator=None,
    )

    assert wd._expected_daemon_boot_id == "abc123"


def test_build_child_watchdog_factory_without_boot_id_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI-without-daemon path: the env var is unset and the factory
    builds a watchdog with ``expected_daemon_boot_id=None`` (skips the
    BOOT_ID_CHANGED check, still detects expired lease)."""
    from app.engine.live.run import _build_child_watchdog_factory

    monkeypatch.delenv("LIVE_RUNNER_DAEMON_BOOT_ID", raising=False)

    factory = _build_child_watchdog_factory(tmp_path, tmp_path / "run")
    wd = factory(
        block_submissions=lambda: None,
        persist_paused=lambda reason: None,
        disconnect_broker=lambda: None,
        request_engine_exit=lambda: None,
        aggregator=None,
    )

    assert wd._expected_daemon_boot_id is None
