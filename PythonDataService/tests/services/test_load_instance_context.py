"""PRD #619-A §A4 — service-integration tests for ``load_instance_context``.

Asserts:

- The loader fetches the daemon binding once, stamps
  ``observation_at_ms`` AFTER the fetch returns, and never imports from
  ``app.routers.*`` (the test module deliberately does not import the
  router — the service must be standalone).
- The composed ``InstanceContext`` carries every fact the mutation
  endpoints' pre-write gate needs: process, live_binding, runs,
  desired_state, last_exit, poisoned, broker, owned_positions_empty,
  guard_state.
- ``daemon_boot_id`` is ``None`` in 619-A (619-B fills it).
- ``owned_positions_empty`` flips correctly between non-empty and empty
  position maps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.services.instance_context import InstanceContext, load_instance_context
from app.services.resume_guard_state import (
    BrokerSafetyArtifact,
    ReconciliationArtifact,
    ResumeGuardState,
    SubmissionCapabilityArtifact,
    UncertainIntentArtifact,
)


@dataclass
class _Process:
    state: str = "running"


@dataclass
class _Binding:
    run_id: str = "run-1"


@dataclass
class _LastExit:
    halt_trigger: str | None = None


@dataclass
class _Broker:
    owned_positions: dict[str, int]


def _make_guard_state(
    *, capability_state: str = "SATISFIED"
) -> ResumeGuardState:
    return ResumeGuardState(
        broker_safety=BrokerSafetyArtifact(state="SAFE", verdict="paper-only"),
        submission_capability=SubmissionCapabilityArtifact(
            state=capability_state,
            declared_submit_mode="live_paper",
            readonly_at_start=False,
        ),
        reconciliation=ReconciliationArtifact(state="NOT_AVAILABLE"),
        uncertain_intent=UncertainIntentArtifact(state="CLEAR"),
        reason_codes=[],
    )


@pytest.mark.asyncio
async def test_loader_stamps_observation_after_daemon_fetch() -> None:
    """observation_at_ms must be sampled AFTER fetch_daemon_process
    returns so it reflects the actual reading consumed.
    """
    clock = [1_700_000_000_000]

    def _now() -> int:
        return clock[0]

    fetch_calls: list[str] = []

    async def _fetch(sid: str) -> dict[str, Any] | None:
        fetch_calls.append(sid)
        # Simulate the daemon RPC taking 50ms.
        clock[0] += 50
        return {"some": "daemon-payload"}

    interpret_called_with: list[dict[str, Any] | None] = []

    def _interpret(d: dict[str, Any] | None) -> tuple[_Process, _Binding]:
        interpret_called_with.append(d)
        return _Process(), _Binding()

    ctx = await load_instance_context(
        "sid-1",
        now_ms=_now,
        fetch_daemon_process=_fetch,
        interpret_daemon_process=_interpret,
        scan_runs_for_instance=lambda _sid: [],
        resolve_desired_state=lambda _sid: None,
        instance_last_exit=lambda _runs: None,
        instance_broker=lambda _sid: None,
        resolve_guard_state_for=lambda _binding, _runs: _make_guard_state(),
    )

    assert fetch_calls == ["sid-1"]
    assert interpret_called_with == [{"some": "daemon-payload"}]
    # observation_at_ms is the clock AFTER the daemon fetch completed.
    assert ctx.observation_at_ms == 1_700_000_000_050
    assert ctx.strategy_instance_id == "sid-1"
    assert ctx.daemon_boot_id is None  # 619-B fills this.
    assert ctx.runtime_freshness is None


@pytest.mark.asyncio
async def test_loader_composes_full_context() -> None:
    runs = [{"run_id": "run-A"}, {"run_id": "run-B"}]
    broker = _Broker(owned_positions={"SPY": 100})
    last_exit = _LastExit(halt_trigger="OUTSIDE_MUTATION")

    ctx: InstanceContext = await load_instance_context(
        "sid-2",
        now_ms=lambda: 42,
        fetch_daemon_process=_make_async_returning(None),
        interpret_daemon_process=lambda _d: (_Process(), _Binding(run_id="run-A")),
        scan_runs_for_instance=lambda _sid: runs,
        resolve_desired_state=lambda _sid: "PAUSED",
        instance_last_exit=lambda r: last_exit if r else None,
        instance_broker=lambda _sid: broker,
        resolve_guard_state_for=lambda _binding, _runs: _make_guard_state(),
    )

    assert ctx.runs == runs
    assert ctx.desired_state == "PAUSED"
    assert ctx.last_exit is last_exit
    assert ctx.poisoned is True  # last_exit.halt_trigger is set
    assert ctx.broker is broker
    assert ctx.owned_positions_empty is False
    assert ctx.guard_state.allow_resume is True
    assert ctx.runtime_freshness is None


@pytest.mark.asyncio
async def test_loader_owned_positions_empty_when_all_zero() -> None:
    broker = _Broker(owned_positions={"SPY": 0, "QQQ": 0})

    ctx = await load_instance_context(
        "sid-3",
        now_ms=lambda: 0,
        fetch_daemon_process=_make_async_returning(None),
        interpret_daemon_process=lambda _d: (_Process(), None),
        scan_runs_for_instance=lambda _sid: [],
        resolve_desired_state=lambda _sid: None,
        instance_last_exit=lambda _runs: None,
        instance_broker=lambda _sid: broker,
        resolve_guard_state_for=lambda _binding, _runs: _make_guard_state(),
    )

    assert ctx.owned_positions_empty is True
    assert ctx.live_binding is None


def _make_async_returning(value):
    async def _fn(_sid: str):
        return value

    return _fn
