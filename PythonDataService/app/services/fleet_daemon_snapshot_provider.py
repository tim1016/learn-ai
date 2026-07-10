"""Fleet-owned daemon observations for Bot Cockpit producers (ADR-0028)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from app.engine.live.daemon_transport import DaemonResult
from app.engine.live.host_daemon_client import HostDaemonCircuitBreaker
from app.schemas.live_runs import HostRunnerInstancesStatus

logger = logging.getLogger(__name__)

FetchInstances = Callable[[str], Awaitable[tuple[DaemonResult, dict | None]]]


@dataclass(frozen=True, slots=True)
class FleetDaemonObservation:
    """Latest transport meaning plus the last successful stamped payload."""

    result: DaemonResult
    payload: dict | None
    processes_by_id: Mapping[str, dict]
    source_fetched_at_ms: int | None
    observed_at_ms: int

    @property
    def is_current(self) -> bool:
        return self.result.kind == "CONNECTED"

    def process_for(self, strategy_instance_id: str) -> dict | None:
        """Return current process truth without relabelling stale evidence."""

        if not self.is_current:
            return None
        process = self.processes_by_id.get(strategy_instance_id)
        if process is not None:
            return dict(process)
        return {
            "state": "idle",
            "message": (
                "No managed process for strategy_instance_id "
                f"{strategy_instance_id!r}."
            ),
        }


class FleetDaemonSnapshotProvider:
    """Own one stale-while-revalidate ``/instances`` poll for the fleet."""

    def __init__(
        self,
        *,
        daemon_url: str,
        fetch_instances: FetchInstances,
        poll_interval_seconds: float = 1.0,
        breaker_initial_backoff_seconds: float = 1.0,
        breaker_max_backoff_seconds: float = 30.0,
        now_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._daemon_url = daemon_url
        self._fetch_instances = fetch_instances
        self._poll_interval_seconds = poll_interval_seconds
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic or time.monotonic
        self._breaker = HostDaemonCircuitBreaker(
            initial_backoff_seconds=breaker_initial_backoff_seconds,
            max_backoff_seconds=breaker_max_backoff_seconds,
        )
        self._observation: FleetDaemonObservation | None = None
        self._next_refresh_at = 0.0
        self._refresh_guard = asyncio.Lock()
        self._refresh_task: asyncio.Task[FleetDaemonObservation] | None = None
        self._lifecycle_guard = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._generation = 0

    @property
    def latest(self) -> FleetDaemonObservation | None:
        return self._observation

    @property
    def is_running(self) -> bool:
        return self._poll_task is not None and not self._poll_task.done()

    @property
    def breaker(self) -> HostDaemonCircuitBreaker:
        return self._breaker

    async def start(self) -> FleetDaemonObservation:
        """Prime one observation, then start the sole cadence owner."""

        async with self._lifecycle_guard:
            if self.is_running:
                observation = self._observation
                if observation is not None:
                    return observation
            self._generation += 1
            self._stop_event = asyncio.Event()
            generation = self._generation
        observation = await self.refresh(force=True)
        async with self._lifecycle_guard:
            if generation == self._generation and not self.is_running:
                self._poll_task = asyncio.create_task(
                    self._poll_loop(generation),
                    name="fleet-daemon-snapshot-provider",
                )
        return observation

    async def observation(self) -> FleetDaemonObservation:
        """Return stale data immediately and coalesce any due revalidation."""

        current = self._observation
        if current is None:
            return await self.refresh(force=True)
        if self._monotonic() >= self._next_refresh_at:
            await self._ensure_refresh_task()
        return current

    async def process_for(
        self,
        strategy_instance_id: str,
    ) -> tuple[DaemonResult, dict | None]:
        observation = await self.observation()
        return observation.result, observation.process_for(strategy_instance_id)

    async def refresh(self, *, force: bool = False) -> FleetDaemonObservation:
        current = self._observation
        if (
            not force
            and current is not None
            and self._monotonic() < self._next_refresh_at
        ):
            return current
        task = await self._ensure_refresh_task()
        return await asyncio.shield(task)

    async def wait_for_idle(self) -> None:
        """Wait for a scheduled revalidation; useful for lifecycle coordination."""

        task = self._refresh_task
        if task is not None:
            await asyncio.shield(task)

    async def stop(self, *, timeout_seconds: float = 2.0) -> None:
        """Cancel polling and revalidation within the shutdown budget."""

        async with self._lifecycle_guard:
            self._generation += 1
            self._stop_event.set()
            pending = [
                task
                for task in (self._poll_task, self._refresh_task)
                if task is not None and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    logger.error(
                        "fleet daemon provider did not stop within shutdown budget"
                    )
            self._poll_task = None
            self._refresh_task = None

    async def _ensure_refresh_task(self) -> asyncio.Task[FleetDaemonObservation]:
        async with self._refresh_guard:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(
                    self._perform_refresh(self._generation),
                    name="fleet-daemon-snapshot-refresh",
                )
            return self._refresh_task

    async def _perform_refresh(self, generation: int) -> FleetDaemonObservation:
        now = self._monotonic()
        current = self._observation
        if current is not None and self._breaker.is_open(now):
            self._next_refresh_at = max(
                now + self._poll_interval_seconds,
                self._breaker.open_until,
            )
            return current
        try:
            result, payload = await self._fetch_instances(self._daemon_url)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("fleet daemon snapshot refresh failed unexpectedly")
            result = DaemonResult(
                kind="UNREACHABLE",
                detail="daemon fleet fetch raised unexpectedly",
                error_category="client_error",
            )
            payload = None

        completed_at = self._monotonic()
        if result.kind == "CONNECTED":
            result, payload = _validate_connected_payload(result, payload)
        self._breaker.observe(result, now=completed_at)
        successful_payload = payload if result.kind == "CONNECTED" else None
        source_fetched_at_ms = _source_fetched_at_ms(successful_payload)
        processes_by_id = _processes_by_id(successful_payload)
        if successful_payload is None and current is not None:
            successful_payload = current.payload
            source_fetched_at_ms = current.source_fetched_at_ms
            processes_by_id = current.processes_by_id
        observation = FleetDaemonObservation(
            result=result,
            payload=successful_payload,
            processes_by_id=processes_by_id,
            source_fetched_at_ms=source_fetched_at_ms,
            observed_at_ms=self._now_ms(),
        )
        self._next_refresh_at = max(
            completed_at + self._poll_interval_seconds,
            self._breaker.open_until,
        )
        if generation == self._generation:
            self._observation = observation
        return observation

    async def _poll_loop(self, generation: int) -> None:
        while generation == self._generation and not self._stop_event.is_set():
            delay = max(0.0, self._next_refresh_at - self._monotonic())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("fleet daemon polling cycle failed")


def _source_fetched_at_ms(payload: dict | None) -> int | None:
    if payload is None:
        return None
    value = payload.get("fetched_at_ms")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
        else None
    )


def _validate_connected_payload(
    result: DaemonResult,
    payload: dict | None,
) -> tuple[DaemonResult, dict | None]:
    if payload is None or _source_fetched_at_ms(payload) is None:
        return (
            DaemonResult.incompatible_contract(
                detail="daemon /instances response lacks an int64 ms UTC fetched_at_ms",
            ),
            None,
        )
    try:
        validated = HostRunnerInstancesStatus.model_validate(payload)
    except ValidationError as exc:
        return DaemonResult.incompatible_contract(detail=str(exc)), None
    return result, validated.model_dump(mode="json")


def _processes_by_id(payload: dict | None) -> dict[str, dict]:
    indexed: dict[str, dict] = {}
    for instance in (payload or {}).get("instances", []):
        strategy_instance_id = instance["strategy_instance_id"]
        process = dict(instance["process"])
        if not process.get("run_id") and instance.get("run_id"):
            process["run_id"] = instance["run_id"]
        indexed[strategy_instance_id] = process
    return indexed


__all__ = [
    "FleetDaemonObservation",
    "FleetDaemonSnapshotProvider",
]
