"""PRD #619-C2 — daemon connectivity monitor.

Folds per-call :class:`~app.engine.live.daemon_transport.DaemonResult`
outcomes from the host-daemon client into a single observable
*connectivity state*. The cockpit, operator surface composer, and
dispatch guardrails consume this state instead of poking the wire on
every render.

Lifecycle mirrors :class:`~app.engine.live.control_plane.DaemonLeaseWriter`:

- ``start()`` is idempotent; re-starts after ``stop()`` are clean.
- ``stop()`` is bounded by ``2 * max(probe_cadence_ms, backoff_cap_ms)``
  with a hard-cancel fallback. Shutdown never hangs even if a probe is
  in flight.
- The inner loop catches every ``Exception``; a flaky probe never kills
  the monitor for the rest of the process.
- Locks are never held across ``sleep`` — the wake event lets an
  operator-triggered re-probe land immediately during a backoff window.

The monitor does **not** know about HTTP. The caller (619-C2 wiring in
``host_daemon_client``) passes an async **probe** callable that returns a
fully-classified :class:`DaemonResult`. That keeps the monitor unit-
testable with an injected fake — no respx, no httpx, no FastAPI app.

Folding rules (see PRD §C):

================================ ========================================
Probe outcome                    Folded into
================================ ========================================
CONNECTED                        kind=CONNECTED, attempt=0,
                                 last_success_ms=now, observed_*
                                 fields refreshed. If observed
                                 boot_id changed across consecutive
                                 CONNECTED probes a signal fires.
UNREACHABLE (attempt+1 < budget) kind=RETRYING (carries detail +
                                 error_category from the probe)
UNREACHABLE (attempt+1 >= budget) kind=UNREACHABLE (terminal until a
                                 CONNECTED probe lands)
AUTH_FAILED                      terminal — pass-through, no folding,
                                 attempt preserved (a future
                                 UNREACHABLE picks up where it left off)
PROTOCOL_ERROR                   terminal — pass-through
INCOMPATIBLE_CONTRACT            terminal — pass-through
RETRYING (from the wire)         defensive pass-through (per-call
                                 factories never emit RETRYING; if a
                                 future caller does, the monitor stays
                                 correct without folding)
================================ ========================================

Backoff: exponential with ±20% jitter on RETRYING only. Default schedule
250ms → 500ms → 1s → 2s → 5s → 10s cap. CONNECTED returns to a steady
cadence (``probe_cadence_ms``, default 5s). Terminal kinds use the same
steady cadence — the caller decides when to act, the monitor just keeps
the state fresh.

The folding logic lives in module-level pure functions
(:func:`fold_outcome`, :func:`backoff_for_attempt`) so the test surface
exercises the rules directly without instantiating the monitor and
without poking at private state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from app.engine.live.daemon_transport import DaemonResult, DaemonResultKind

logger = logging.getLogger(__name__)


DEFAULT_PROBE_CADENCE_MS = 5_000
DEFAULT_RETRY_BUDGET = 5
DEFAULT_BACKOFF_SCHEDULE_MS: tuple[int, ...] = (250, 500, 1_000, 2_000, 5_000, 10_000)
DEFAULT_BACKOFF_CAP_MS = 10_000
DEFAULT_JITTER_FRACTION = 0.2

BootIdChangeSignal = Callable[[str, str], None]
ProbeFn = Callable[[], Awaitable[DaemonResult]]


# ---------------------------------------------------------------------------
# Observable state
# ---------------------------------------------------------------------------


class DaemonConnectivityState(BaseModel):
    """Frozen snapshot of the monitor's folded connectivity state.

    Every transition produces a new instance. Consumers (cockpit poll,
    operator surface composer, dispatch guard) read the latest snapshot
    via :meth:`DaemonConnectivityMonitor.state` and never mutate it.

    The shape is intentionally flat so the operator surface composer can
    project it directly into the cockpit payload without an extra mapping
    layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DaemonResultKind
    attempt: int = Field(default=0, ge=0)
    last_transition_ms: int = Field(ge=0)
    last_success_ms: int | None = Field(default=None, ge=0)
    observed_daemon_boot_id: str | None = None
    observed_daemon_api_version: int | None = Field(default=None, ge=1)
    last_detail: str | None = None
    last_error_category: str | None = None
    next_probe_in_ms: int = Field(default=0, ge=0)


# Kinds the monitor classifies as "terminal" for this poll: they do not
# fold into RETRYING and they do not increment ``attempt``. They stay in
# place until a CONNECTED probe overwrites them.
_TERMINAL_KINDS: frozenset[DaemonResultKind] = frozenset(
    {"AUTH_FAILED", "PROTOCOL_ERROR", "INCOMPATIBLE_CONTRACT"}
)


# ---------------------------------------------------------------------------
# Folding — pure module-level functions exposed for test coverage.
# ---------------------------------------------------------------------------


def backoff_for_attempt(
    attempt: int,
    *,
    schedule_ms: tuple[int, ...] = DEFAULT_BACKOFF_SCHEDULE_MS,
    cap_ms: int = DEFAULT_BACKOFF_CAP_MS,
    jitter_fraction: float = DEFAULT_JITTER_FRACTION,
    rng: random.Random | None = None,
) -> int:
    """Return the next probe wait (ms) for a 1-indexed retry slot.

    ``attempt`` is the retry-slot number after the failure (first failure
    → 1). The schedule is indexed 0..N-1; after exhausting the explicit
    schedule, slots clamp to ``cap_ms``. Symmetric ±``jitter_fraction``
    multiplicative jitter is applied with a 1ms floor so a degenerate
    ``int(0.x)`` → 0 never tight-loops the runner.
    """
    if attempt < 1:
        raise ValueError("attempt must be ≥ 1")
    slot = attempt - 1
    base_ms = schedule_ms[slot] if slot < len(schedule_ms) else schedule_ms[-1]
    base_ms = min(base_ms, cap_ms)
    if jitter_fraction <= 0.0:
        return base_ms
    r = rng if rng is not None else random
    low = 1.0 - jitter_fraction
    high = 1.0 + jitter_fraction
    return max(int(base_ms * r.uniform(low, high)), 1)


def fold_outcome(
    prev: DaemonConnectivityState,
    result: DaemonResult,
    *,
    now_ms: int,
    retry_budget: int,
    probe_cadence_ms: int,
    backoff_schedule_ms: tuple[int, ...] = DEFAULT_BACKOFF_SCHEDULE_MS,
    backoff_cap_ms: int = DEFAULT_BACKOFF_CAP_MS,
    jitter_fraction: float = DEFAULT_JITTER_FRACTION,
    rng: random.Random | None = None,
) -> DaemonConnectivityState:
    """Compute the next state from one probe outcome.

    Pure: same ``(prev, result, …)`` → same output, modulo the RNG. Pass
    a seeded :class:`random.Random` for deterministic jitter tests. The
    next-probe wait (ms) is carried inside the returned state as
    ``next_probe_in_ms``; the monitor reads it directly off the new
    state rather than receiving it as a separate tuple element.
    """
    if result.kind == "CONNECTED":
        return _fold_connected(prev, result, now_ms=now_ms, probe_cadence_ms=probe_cadence_ms)

    if result.kind == "UNREACHABLE":
        return _fold_unreachable(
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

    # AUTH_FAILED / PROTOCOL_ERROR / INCOMPATIBLE_CONTRACT — retries do
    # not help. Preserve ``attempt`` (so a subsequent UNREACHABLE picks
    # up where it left off) and pass the kind through. The defensive
    # RETRYING-from-wire case (per-call factories don't emit it; if a
    # future caller does, stay correct without folding) shares the same
    # pass-through shape.
    return DaemonConnectivityState(
        kind=result.kind,
        attempt=prev.attempt,
        last_transition_ms=(
            prev.last_transition_ms if prev.kind == result.kind else now_ms
        ),
        last_success_ms=prev.last_success_ms,
        observed_daemon_boot_id=prev.observed_daemon_boot_id,
        observed_daemon_api_version=prev.observed_daemon_api_version,
        last_detail=result.detail,
        last_error_category=result.error_category,
        next_probe_in_ms=probe_cadence_ms,
    )


def _fold_connected(
    prev: DaemonConnectivityState,
    result: DaemonResult,
    *,
    now_ms: int,
    probe_cadence_ms: int,
) -> DaemonConnectivityState:
    return DaemonConnectivityState(
        kind="CONNECTED",
        attempt=0,
        last_transition_ms=(
            prev.last_transition_ms if prev.kind == "CONNECTED" else now_ms
        ),
        last_success_ms=now_ms,
        observed_daemon_boot_id=result.observed_daemon_boot_id,
        observed_daemon_api_version=result.observed_daemon_api_version,
        last_detail=None,
        last_error_category=None,
        next_probe_in_ms=probe_cadence_ms,
    )


def _fold_unreachable(
    prev: DaemonConnectivityState,
    result: DaemonResult,
    *,
    now_ms: int,
    retry_budget: int,
    probe_cadence_ms: int,
    backoff_schedule_ms: tuple[int, ...],
    backoff_cap_ms: int,
    jitter_fraction: float,
    rng: random.Random | None,
) -> DaemonConnectivityState:
    # Attempt counter advances on every UNREACHABLE outcome until the
    # budget is exceeded; once exceeded we surface UNREACHABLE and stop
    # counting upward (the operator sees a stable terminal number).
    next_attempt = prev.attempt if prev.kind == "UNREACHABLE" else prev.attempt + 1

    if next_attempt < retry_budget:
        wait_ms = backoff_for_attempt(
            next_attempt,
            schedule_ms=backoff_schedule_ms,
            cap_ms=backoff_cap_ms,
            jitter_fraction=jitter_fraction,
            rng=rng,
        )
        return DaemonConnectivityState(
            kind="RETRYING",
            attempt=next_attempt,
            last_transition_ms=(
                prev.last_transition_ms if prev.kind == "RETRYING" else now_ms
            ),
            last_success_ms=prev.last_success_ms,
            observed_daemon_boot_id=prev.observed_daemon_boot_id,
            observed_daemon_api_version=prev.observed_daemon_api_version,
            last_detail=result.detail,
            last_error_category=result.error_category,
            next_probe_in_ms=wait_ms,
        )

    # Budget exhausted — emit UNREACHABLE as soon as the last allowed
    # attempt is reached, then idle at the steady cadence until recovery
    # or operator action.
    return DaemonConnectivityState(
        kind="UNREACHABLE",
        attempt=retry_budget,
        last_transition_ms=(
            prev.last_transition_ms if prev.kind == "UNREACHABLE" else now_ms
        ),
        last_success_ms=prev.last_success_ms,
        observed_daemon_boot_id=prev.observed_daemon_boot_id,
        observed_daemon_api_version=prev.observed_daemon_api_version,
        last_detail=result.detail,
        last_error_category=result.error_category,
        next_probe_in_ms=probe_cadence_ms,
    )


def initial_state(*, now_ms: int) -> DaemonConnectivityState:
    """The state of a monitor that has not yet observed a probe.

    Modeled as ``RETRYING`` with ``attempt=0`` — the cockpit reads this
    as "we'll have a result shortly". The first real probe outcome
    overwrites it; the next ``last_transition_ms`` is set fresh because
    ``RETRYING -> CONNECTED`` (or anything else) is a real transition.
    """
    return DaemonConnectivityState(
        kind="RETRYING",
        attempt=0,
        last_transition_ms=now_ms,
        next_probe_in_ms=0,
    )


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class DaemonConnectivityMonitor:
    """Background async task that polls a daemon-health probe and folds
    the outcomes into :class:`DaemonConnectivityState`.

    Construction seams (all keyword-only):

    - ``probe`` — the injected ``async`` callable returning a
      ``DaemonResult``. The caller owns the HTTP client and URL; the
      monitor only sees the classified outcome.
    - ``now_ms`` — clock seam, matches ``DaemonLeaseWriter``.
    - ``rng`` — jitter seam. Pass ``random.Random(seed)`` for
      deterministic backoff in tests; default is the module-level
      ``random`` (non-deterministic, fine for production).
    - ``on_boot_id_change`` — optional callback fired exactly when a
      ``CONNECTED`` probe carries a boot_id that differs from the
      previously-observed (non-empty) boot_id. Old → new.

    The between-probes sleep uses ``asyncio.wait_for(self._wake.wait(),
    timeout=...)``; tests drive the loop quickly by setting tiny
    cadences and/or firing ``wake()``. There is no injectable
    ``sleep_fn`` — the wake event IS the sleep seam.

    ``state`` is the latest immutable :class:`DaemonConnectivityState`.
    Reads are lock-free; the folding logic lives in pure module-level
    functions so tests can exercise the rules without instantiating
    this class.
    """

    def __init__(
        self,
        *,
        probe: ProbeFn,
        now_ms: Callable[[], int],
        probe_cadence_ms: int = DEFAULT_PROBE_CADENCE_MS,
        retry_budget: int = DEFAULT_RETRY_BUDGET,
        backoff_schedule_ms: tuple[int, ...] = DEFAULT_BACKOFF_SCHEDULE_MS,
        backoff_cap_ms: int = DEFAULT_BACKOFF_CAP_MS,
        jitter_fraction: float = DEFAULT_JITTER_FRACTION,
        rng: random.Random | None = None,
        on_boot_id_change: BootIdChangeSignal | None = None,
    ) -> None:
        if probe_cadence_ms <= 0:
            raise ValueError("probe_cadence_ms must be > 0")
        if retry_budget < 0:
            raise ValueError("retry_budget must be ≥ 0")
        if backoff_cap_ms <= 0:
            raise ValueError("backoff_cap_ms must be > 0")
        if not 0.0 <= jitter_fraction < 1.0:
            raise ValueError("jitter_fraction must be in [0.0, 1.0)")
        if not backoff_schedule_ms:
            raise ValueError("backoff_schedule_ms must be non-empty")

        self._probe = probe
        self._now_ms = now_ms
        self._probe_cadence_ms = probe_cadence_ms
        self._retry_budget = retry_budget
        self._backoff_schedule_ms = backoff_schedule_ms
        self._backoff_cap_ms = backoff_cap_ms
        self._jitter_fraction = jitter_fraction
        self._rng = rng
        self._on_boot_id_change = on_boot_id_change

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._state = initial_state(now_ms=now_ms())

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def state(self) -> DaemonConnectivityState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def wake(self) -> None:
        """Cause the next probe to land immediately (drop any backoff sleep).

        Idempotent and lock-free.
        """
        self._wake.set()

    async def start(self) -> None:
        """Start the monitor. Idempotent — a re-start after stop is clean."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(
            self._run(), name="daemon_connectivity_monitor"
        )

    async def stop(self) -> None:
        """Cancel + drain. Bounded by ``2 * max(probe_cadence, backoff_cap)``.

        Exception-safe: a probe that hangs is cancelled; failures during
        teardown are logged and swallowed so the FastAPI lifespan does
        not stall on shutdown.
        """
        if self._task is None:
            return
        self._stop.set()
        self._wake.set()
        bound_ms = max(self._probe_cadence_ms, self._backoff_cap_ms) * 2
        bound_s = max(bound_ms / 1000.0, 0.5)
        try:
            await asyncio.wait_for(self._task, timeout=bound_s)
        except TimeoutError:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        finally:
            self._task = None

    # ------------------------------------------------------------------
    # Inner loop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        # One probe in flight at a time — guaranteed by the linear
        # await-then-sleep loop. No semaphore needed.
        while not self._stop.is_set():
            try:
                result = await self._probe()
            except Exception:
                # Exception-tolerant by contract: the probe is expected
                # to return a DaemonResult for every transport failure
                # (619-C1). A raise here is a programmer error, not a
                # wire outcome — log + keep going.
                logger.exception(
                    "daemon_connectivity_monitor probe raised; sleeping at steady cadence"
                )
                # A raising probe is a programmer error, not a wire
                # outcome. The probe is expected to return a
                # DaemonResult for every transport failure (619-C1).
                # Don't fold into the typed state — just keep going.
                await self._sleep_with_wake(self._probe_cadence_ms)
                continue

            prev = self._state
            next_state = fold_outcome(
                prev,
                result,
                now_ms=self._now_ms(),
                retry_budget=self._retry_budget,
                probe_cadence_ms=self._probe_cadence_ms,
                backoff_schedule_ms=self._backoff_schedule_ms,
                backoff_cap_ms=self._backoff_cap_ms,
                jitter_fraction=self._jitter_fraction,
                rng=self._rng,
            )
            if result.kind == "CONNECTED":
                self._maybe_signal_boot_id_change(prev, result)
            self._state = next_state

            if self._stop.is_set():
                break
            await self._sleep_with_wake(next_state.next_probe_in_ms)

    async def _sleep_with_wake(self, wait_ms: int) -> None:
        """Sleep for up to ``wait_ms`` ms unless ``wake`` or ``stop`` fires."""
        if wait_ms <= 0:
            return
        timeout_s = wait_ms / 1000.0
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout_s)
            self._wake.clear()
        except TimeoutError:
            return

    def _maybe_signal_boot_id_change(
        self,
        prev: DaemonConnectivityState,
        result: DaemonResult,
    ) -> None:
        if self._on_boot_id_change is None:
            return
        new_boot_id = result.observed_daemon_boot_id
        old_boot_id = prev.observed_daemon_boot_id
        # Fire only when BOTH old and new are non-empty: a daemon that
        # loses its boot_id (regression to None) is a different alert
        # class — flagged elsewhere, not as a "change" event.
        if not new_boot_id or not old_boot_id:
            return
        if new_boot_id == old_boot_id:
            return
        try:
            self._on_boot_id_change(old_boot_id, new_boot_id)
        except Exception:
            # Signal handler must not kill the loop — log + continue.
            logger.exception(
                "daemon_connectivity_monitor boot_id_change handler raised",
                extra={"old_boot_id": old_boot_id, "new_boot_id": new_boot_id},
            )


# ---------------------------------------------------------------------------
# Process-wide singleton — installed by the FastAPI lifespan.
# ---------------------------------------------------------------------------
#
# Mirrors ``app.broker.ibkr.auto_reconnect_monitor.{get,set}_monitor``: the
# operator-surface composer (619-C3) and the cockpit poll endpoints consult
# this to read the latest connectivity state without dependency injection.
# Consumers must guard with ``if monitor is not None`` so unit tests and
# broker-disabled deployments stay clean.

_monitor: DaemonConnectivityMonitor | None = None


def get_monitor() -> DaemonConnectivityMonitor | None:
    """Return the active connectivity monitor, or ``None`` when the
    lifespan has not installed one (tests, broker-disabled mode, or
    deployments with no daemon URL configured)."""
    return _monitor


def set_monitor(monitor: DaemonConnectivityMonitor | None) -> None:
    global _monitor
    _monitor = monitor
