"""PRD #619-C2 — DaemonConnectivityMonitor folding + lifecycle tests.

The monitor under test only depends on:

- a probe coroutine (injected fake)
- a fake monotonic clock (``tests/_fixtures/fake_clock.make_test_clock``)
- a deterministic RNG (``random.Random(seed)``)

No respx, no httpx, no FastAPI app. The probe returns the typed
``DaemonResult`` directly — that's the entire contract the monitor sees.

Folding rules are exercised directly against the pure module-level
``fold_outcome`` function (no monitor instance required, no private
attribute access). Lifecycle tests drive the real async loop with tiny
cadences; the wake event provides the test-time short-circuit.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable

import pytest

from app.engine.live.daemon_connectivity_monitor import (
    DEFAULT_BACKOFF_SCHEDULE_MS,
    DEFAULT_PROBE_CADENCE_MS,
    DaemonConnectivityMonitor,
    DaemonConnectivityState,
    backoff_for_attempt,
    fold_outcome,
    initial_state,
)
from app.engine.live.daemon_transport import DaemonResult
from tests._fixtures.fake_clock import make_test_clock

START_MS = 1_700_000_000_000


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class ScriptedProbe:
    """Probe that yields a queued list of ``DaemonResult`` values.

    Re-uses the last entry once the script is exhausted so a tight test
    loop never crashes on KeyError-style indexing.
    """

    def __init__(self, script: list[DaemonResult]) -> None:
        if not script:
            raise ValueError("script must contain at least one DaemonResult")
        self._script = list(script)
        self._idx = 0
        self.calls: int = 0
        self.in_flight: int = 0
        self.max_concurrent: int = 0
        self._gate: asyncio.Event | None = None

    async def __call__(self) -> DaemonResult:
        self.in_flight += 1
        self.max_concurrent = max(self.max_concurrent, self.in_flight)
        try:
            if self._gate is not None:
                await self._gate.wait()
            self.calls += 1
            idx = min(self._idx, len(self._script) - 1)
            self._idx += 1
            return self._script[idx]
        finally:
            self.in_flight -= 1

    def gate(self) -> asyncio.Event:
        """Block subsequent probes until the returned event is set."""
        self._gate = asyncio.Event()
        return self._gate


async def _drive_probes(probe: ScriptedProbe, target_calls: int) -> None:
    """Yield to the event loop until the monitor has consumed ``target_calls``."""
    deadline = asyncio.get_running_loop().time() + 1.0
    while probe.calls < target_calls:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                f"probe stalled at {probe.calls}/{target_calls} calls"
            )
        await asyncio.sleep(0)


def _connected(
    *, boot_id: str | None = "boot-A", api_version: int | None = 1
) -> DaemonResult:
    return DaemonResult.connected(
        daemon_boot_id=boot_id, daemon_api_version=api_version
    )


def _unreachable(
    *,
    detail: str = "connect refused",
    error_category: str = "connect_error",
) -> DaemonResult:
    return DaemonResult(
        kind="UNREACHABLE",
        detail=detail,
        error_category=error_category,
        outcome_ambiguous=False,
    )


def _fold(
    prev: DaemonConnectivityState,
    result: DaemonResult,
    *,
    now_ms: int = START_MS,
    retry_budget: int = 3,
    probe_cadence_ms: int = DEFAULT_PROBE_CADENCE_MS,
    backoff_schedule_ms: tuple[int, ...] = DEFAULT_BACKOFF_SCHEDULE_MS,
    backoff_cap_ms: int = 10_000,
    jitter_fraction: float = 0.0,
    rng: random.Random | None = None,
) -> DaemonConnectivityState:
    """Shorthand wrapper to keep test setup short."""
    return fold_outcome(
        prev,
        result,
        now_ms=now_ms,
        retry_budget=retry_budget,
        probe_cadence_ms=probe_cadence_ms,
        backoff_schedule_ms=backoff_schedule_ms,
        backoff_cap_ms=backoff_cap_ms,
        jitter_fraction=jitter_fraction,
        rng=rng,
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_is_retrying_with_zero_attempt() -> None:
    state = initial_state(now_ms=START_MS)

    assert state.kind == "RETRYING"
    assert state.attempt == 0
    assert state.last_transition_ms == START_MS
    assert state.last_success_ms is None
    assert state.observed_daemon_boot_id is None
    assert state.next_probe_in_ms == 0


def test_monitor_exposes_initial_state_before_first_probe() -> None:
    clock = make_test_clock(START_MS)

    monitor = DaemonConnectivityMonitor(
        probe=ScriptedProbe([_connected()]), now_ms=clock
    )

    state = monitor.state
    assert state.kind == "RETRYING"
    assert state.attempt == 0
    assert state.last_success_ms is None


# ---------------------------------------------------------------------------
# Folding rules — exercised via the pure ``fold_outcome`` function
# ---------------------------------------------------------------------------


def test_connected_probe_marks_success_and_resets_attempt() -> None:
    prev = _fold(initial_state(now_ms=START_MS), _unreachable())
    assert prev.kind == "RETRYING"
    assert prev.attempt == 1

    state = _fold(
        prev,
        _connected(boot_id="boot-A", api_version=2),
        now_ms=START_MS + 7_500,
    )

    assert state.next_probe_in_ms == DEFAULT_PROBE_CADENCE_MS
    assert state.kind == "CONNECTED"
    assert state.attempt == 0
    assert state.last_success_ms == START_MS + 7_500
    assert state.observed_daemon_boot_id == "boot-A"
    assert state.observed_daemon_api_version == 2
    assert state.last_detail is None
    assert state.last_error_category is None


def test_consecutive_connected_probes_preserve_last_transition() -> None:
    s0 = _fold(initial_state(now_ms=START_MS), _connected(), now_ms=START_MS + 100)
    s1 = _fold(s0, _connected(), now_ms=START_MS + 500)

    # last_transition_ms updates on the FIRST CONNECTED (transition from
    # RETRYING) but stays put on subsequent CONNECTED probes.
    assert s0.last_transition_ms == START_MS + 100
    assert s1.last_transition_ms == START_MS + 100


def test_unreachable_folds_into_retrying_under_budget() -> None:
    s = initial_state(now_ms=START_MS)

    s = _fold(s, _unreachable(detail="d1", error_category="connect_error"))
    assert s.kind == "RETRYING"
    assert s.attempt == 1
    assert s.last_detail == "d1"
    assert s.last_error_category == "connect_error"
    assert s.next_probe_in_ms == DEFAULT_BACKOFF_SCHEDULE_MS[0]

    s = _fold(s, _unreachable())
    assert s.attempt == 2
    assert s.next_probe_in_ms == DEFAULT_BACKOFF_SCHEDULE_MS[1]

    s = _fold(s, _unreachable())
    assert s.attempt == 3
    assert s.kind == "RETRYING"


def test_unreachable_past_budget_emits_terminal_unreachable() -> None:
    s = initial_state(now_ms=START_MS)

    s = _fold(s, _unreachable(), retry_budget=2)  # attempt=1, RETRYING
    s = _fold(s, _unreachable(), retry_budget=2)  # attempt=2, RETRYING (at budget)
    s = _fold(s, _unreachable(), retry_budget=2)  # exceeds budget

    assert s.kind == "UNREACHABLE"
    assert s.attempt == 2  # pinned at budget, not 3
    assert s.next_probe_in_ms == DEFAULT_PROBE_CADENCE_MS

    # Further UNREACHABLE outcomes do not bump attempt past the cap.
    s = _fold(s, _unreachable(), retry_budget=2)
    assert s.attempt == 2


def test_recovery_clears_retrying_detail_and_resets_attempt() -> None:
    s = initial_state(now_ms=START_MS)
    s = _fold(s, _unreachable(detail="oops"))
    s = _fold(s, _unreachable(detail="oops"))
    assert s.attempt == 2

    s = _fold(s, _connected(boot_id="boot-A"))

    assert s.kind == "CONNECTED"
    assert s.attempt == 0
    assert s.last_detail is None
    assert s.last_error_category is None


@pytest.mark.parametrize(
    ("kind", "factory"),
    [
        ("AUTH_FAILED", lambda: DaemonResult.auth_failed(status=401, detail="bad token")),
        ("PROTOCOL_ERROR", lambda: DaemonResult.protocol_error(status=500, detail="boom")),
        ("INCOMPATIBLE_CONTRACT", lambda: DaemonResult.incompatible_contract(detail="schema drift")),
    ],
)
def test_terminal_kinds_do_not_fold_into_retrying(
    kind: str, factory: Callable[[], DaemonResult]
) -> None:
    s = _fold(initial_state(now_ms=START_MS), factory(), retry_budget=5)

    assert s.kind == kind
    assert s.attempt == 0  # terminal from initial: nothing to increment
    assert s.last_detail is not None
    assert s.next_probe_in_ms == DEFAULT_PROBE_CADENCE_MS


def test_auth_failed_preserves_unreachable_attempt() -> None:
    """An auth failure mid retry-storm does NOT pretend the backoff slot
    reset — the operator should see how many retries we'd burned."""
    s = initial_state(now_ms=START_MS)
    s = _fold(s, _unreachable(), retry_budget=5)
    s = _fold(s, _unreachable(), retry_budget=5)
    assert s.attempt == 2

    s = _fold(
        s, DaemonResult.auth_failed(status=401, detail="rotated token"), retry_budget=5
    )

    assert s.kind == "AUTH_FAILED"
    assert s.attempt == 2


def test_retrying_from_wire_passes_through_defensively() -> None:
    """Per-call factories never emit RETRYING (619-C1), but if a future
    caller does, the monitor preserves the kind without folding."""
    s = initial_state(now_ms=START_MS)
    s = _fold(s, _unreachable())
    assert s.attempt == 1

    wire_retrying = DaemonResult(
        kind="RETRYING",
        error_category="connect_error",
        detail="ambiguous",
    )
    s = _fold(s, wire_retrying)

    assert s.kind == "RETRYING"
    assert s.attempt == 1  # preserved, not folded forward
    assert s.next_probe_in_ms == DEFAULT_PROBE_CADENCE_MS


# ---------------------------------------------------------------------------
# Backoff schedule + jitter (pure)
# ---------------------------------------------------------------------------


def test_backoff_schedule_respects_cap_after_exhaustion() -> None:
    schedule = (100, 200, 400)
    cap = 400
    observed = [
        backoff_for_attempt(i, schedule_ms=schedule, cap_ms=cap, jitter_fraction=0.0)
        for i in range(1, 7)
    ]
    assert observed == [100, 200, 400, 400, 400, 400]


def test_backoff_jitter_window_is_centred_on_schedule() -> None:
    rng = random.Random(42)
    waits = [
        backoff_for_attempt(
            1, schedule_ms=(1_000,), cap_ms=1_000, jitter_fraction=0.2, rng=rng
        )
        for _ in range(40)
    ]
    # Every value in [0.8 × 1000, 1.2 × 1000) and at least one ≠ base.
    assert all(800 <= w < 1_200 for w in waits)
    assert any(w != 1_000 for w in waits)


def test_backoff_floor_is_one_ms() -> None:
    """Multiplicative jitter on a tiny base must not yield 0ms."""
    rng = random.Random(0)
    wait = backoff_for_attempt(
        1, schedule_ms=(1,), cap_ms=1, jitter_fraction=0.9, rng=rng
    )
    assert wait >= 1


def test_backoff_attempt_must_be_positive() -> None:
    with pytest.raises(ValueError, match="attempt must be"):
        backoff_for_attempt(0)


# ---------------------------------------------------------------------------
# Boot-id change signal (drives via the monitor's inner loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_id_change_fires_signal_with_old_and_new() -> None:
    clock = make_test_clock(START_MS)
    changes: list[tuple[str, str]] = []
    probe = ScriptedProbe(
        [
            _connected(boot_id="boot-A"),
            _connected(boot_id="boot-A"),  # same — no signal
            _connected(boot_id="boot-B"),  # change — signal
        ]
    )
    monitor = DaemonConnectivityMonitor(
        probe=probe,
        now_ms=clock,
        probe_cadence_ms=1,
        on_boot_id_change=lambda old, new: changes.append((old, new)),
    )

    await monitor.start()
    try:
        await _drive_probes(probe, target_calls=3)
        # Give the loop one more turn so the final fold settles before stop().
        await asyncio.sleep(0)
    finally:
        await monitor.stop()

    assert changes == [("boot-A", "boot-B")]
    assert monitor.state.observed_daemon_boot_id == "boot-B"


@pytest.mark.asyncio
async def test_boot_id_first_observation_does_not_signal() -> None:
    """The first CONNECTED is the establishing observation, not a change."""
    clock = make_test_clock(START_MS)
    changes: list[tuple[str, str]] = []
    probe = ScriptedProbe([_connected(boot_id="boot-A")])
    monitor = DaemonConnectivityMonitor(
        probe=probe,
        now_ms=clock,
        probe_cadence_ms=1,
        on_boot_id_change=lambda old, new: changes.append((old, new)),
    )

    await monitor.start()
    try:
        await _drive_probes(probe, target_calls=1)
        await asyncio.sleep(0)
    finally:
        await monitor.stop()

    assert changes == []
    assert monitor.state.observed_daemon_boot_id == "boot-A"


@pytest.mark.asyncio
async def test_boot_id_signal_raising_handler_does_not_kill_state_update() -> None:
    clock = make_test_clock(START_MS)

    def _raising(_old: str, _new: str) -> None:
        raise RuntimeError("downstream handler is angry")

    probe = ScriptedProbe(
        [_connected(boot_id="boot-A"), _connected(boot_id="boot-B")]
    )
    monitor = DaemonConnectivityMonitor(
        probe=probe,
        now_ms=clock,
        probe_cadence_ms=1,
        on_boot_id_change=_raising,
    )

    await monitor.start()
    try:
        await _drive_probes(probe, target_calls=2)
        await asyncio.sleep(0)
    finally:
        await monitor.stop()

    assert monitor.state.observed_daemon_boot_id == "boot-B"


# ---------------------------------------------------------------------------
# Construction validation
# ---------------------------------------------------------------------------


def test_construction_validates_parameters() -> None:
    clock = make_test_clock(START_MS)
    probe = ScriptedProbe([_connected()])

    with pytest.raises(ValueError, match="probe_cadence_ms"):
        DaemonConnectivityMonitor(probe=probe, now_ms=clock, probe_cadence_ms=0)
    with pytest.raises(ValueError, match="retry_budget"):
        DaemonConnectivityMonitor(probe=probe, now_ms=clock, retry_budget=-1)
    with pytest.raises(ValueError, match="jitter_fraction"):
        DaemonConnectivityMonitor(probe=probe, now_ms=clock, jitter_fraction=1.0)
    with pytest.raises(ValueError, match="backoff_schedule_ms"):
        DaemonConnectivityMonitor(probe=probe, now_ms=clock, backoff_schedule_ms=())


# ---------------------------------------------------------------------------
# Lifecycle (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    clock = make_test_clock(START_MS)
    monitor = DaemonConnectivityMonitor(
        probe=ScriptedProbe([_connected()]),
        now_ms=clock,
        probe_cadence_ms=10,
    )

    await monitor.start()
    assert monitor.is_running
    task_first = monitor._task
    await monitor.start()
    assert monitor._task is task_first
    await monitor.stop()
    assert not monitor.is_running


@pytest.mark.asyncio
async def test_stop_is_bounded() -> None:
    clock = make_test_clock(START_MS)
    monitor = DaemonConnectivityMonitor(
        probe=ScriptedProbe([_connected()]),
        now_ms=clock,
        probe_cadence_ms=10,
        backoff_cap_ms=10,
    )

    await monitor.start()
    await asyncio.wait_for(monitor.stop(), timeout=1.0)


@pytest.mark.asyncio
async def test_stop_with_no_start_is_noop() -> None:
    clock = make_test_clock(START_MS)
    monitor = DaemonConnectivityMonitor(
        probe=ScriptedProbe([_connected()]), now_ms=clock
    )

    await monitor.stop()  # must not raise


@pytest.mark.asyncio
async def test_restart_after_stop_is_clean() -> None:
    clock = make_test_clock(START_MS)
    monitor = DaemonConnectivityMonitor(
        probe=ScriptedProbe([_connected()]),
        now_ms=clock,
        probe_cadence_ms=10,
    )

    await monitor.start()
    await monitor.stop()
    await monitor.start()
    assert monitor.is_running
    await monitor.stop()


@pytest.mark.asyncio
async def test_loop_drives_probe_and_folds_state() -> None:
    clock = make_test_clock(START_MS)
    probe = ScriptedProbe([_connected(boot_id="boot-A")])

    monitor = DaemonConnectivityMonitor(
        probe=probe,
        now_ms=clock,
        probe_cadence_ms=1,
    )
    await monitor.start()
    try:
        await _drive_probes(probe, target_calls=3)
        await asyncio.sleep(0)
    finally:
        await monitor.stop()

    assert monitor.state.kind == "CONNECTED"
    assert monitor.state.observed_daemon_boot_id == "boot-A"


@pytest.mark.asyncio
async def test_no_overlapping_probes() -> None:
    """Two probes must never be in flight at the same time."""
    clock = make_test_clock(START_MS)
    probe = ScriptedProbe([_connected()])
    gate = probe.gate()

    monitor = DaemonConnectivityMonitor(
        probe=probe,
        now_ms=clock,
        probe_cadence_ms=1,
    )
    await monitor.start()

    # Yield repeatedly while the first probe is blocked behind the gate.
    for _ in range(10):
        await asyncio.sleep(0)

    assert probe.in_flight <= 1
    assert probe.max_concurrent <= 1

    gate.set()
    try:
        await _drive_probes(probe, target_calls=2)
    finally:
        await monitor.stop()

    assert probe.max_concurrent == 1


@pytest.mark.asyncio
async def test_wake_short_circuits_backoff_sleep() -> None:
    clock = make_test_clock(START_MS)
    # Script: one UNREACHABLE then CONNECTED forever. With a very long
    # backoff schedule, only ``wake()`` can drive the second probe
    # quickly.
    probe = ScriptedProbe([_unreachable(), _connected()])

    monitor = DaemonConnectivityMonitor(
        probe=probe,
        now_ms=clock,
        probe_cadence_ms=60_000,
        retry_budget=3,
        backoff_schedule_ms=(60_000,),
        backoff_cap_ms=60_000,
        jitter_fraction=0.0,
    )
    await monitor.start()
    try:
        await _drive_probes(probe, target_calls=1)
        assert monitor.state.kind == "RETRYING"
        monitor.wake()
        await _drive_probes(probe, target_calls=2)
    finally:
        await monitor.stop()

    assert monitor.state.kind == "CONNECTED"


@pytest.mark.asyncio
async def test_raising_probe_does_not_kill_loop() -> None:
    clock = make_test_clock(START_MS)
    call_count = {"n": 0}

    async def _flaky() -> DaemonResult:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient programmer error")
        return _connected()

    monitor = DaemonConnectivityMonitor(
        probe=_flaky,
        now_ms=clock,
        probe_cadence_ms=1,
    )
    await monitor.start()
    try:
        deadline = asyncio.get_running_loop().time() + 1.0
        while monitor.state.kind != "CONNECTED":
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"loop did not recover; last kind={monitor.state.kind}"
                )
            await asyncio.sleep(0)
    finally:
        await monitor.stop()

    assert call_count["n"] >= 2
    assert monitor.state.kind == "CONNECTED"


def test_state_is_frozen_pydantic_model() -> None:
    state = initial_state(now_ms=START_MS)

    assert isinstance(state, DaemonConnectivityState)
    with pytest.raises(Exception):
        state.attempt = 99  # type: ignore[misc]
