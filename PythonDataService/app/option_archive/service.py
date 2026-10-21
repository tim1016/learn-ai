"""Preservation orchestration; provider work stays on the shared background loop."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.admission import LakeAdmissionError
from app.data_lake.option_lake import OptionCaptureResult, OptionDataFailure
from app.data_lake.option_request_priority import archive_request
from app.data_lake.option_session_staging import CAPACITY_PREFIX
from app.data_lake.root_identity import active_root_id
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive.backup import (
    BackupRefusedError,
    RestoreReceipt,
    bounded_file_work,
    create_generation,
    decode_restored_artifacts,
    manifest_digest,
    prune_generations,
    restore_generation,
    validate_destination,
    verify_generation,
)
from app.option_archive.health import project_health
from app.option_archive.planner import (
    UNDERLYINGS,
    archive_baseline_ms,
    archive_history_boundary,
    plan_archive,
    regular_hours_pause,
    wall_clock_ms,
)
from app.option_archive.scheduler import ArchiveJob, ArchiveScheduler, PostgresJobOwnership
from app.option_archive.settings import ArchiveSettings, archive_settings
from app.option_archive.snapshot import ChainObservation, observe_chain, save_observation
from app.option_archive.state import ArchiveState
from app.schemas.option_archive import OptionArchiveHealth
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

logger = logging.getLogger(__name__)


def _capacity_pause(result: OptionCaptureResult) -> bool:
    failure = result.failure
    return failure is not None and failure.reason == "lease_timeout" and failure.detail.startswith(CAPACITY_PREFIX)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class OptionArchiveService:
    def __init__(self, *, config: ArchiveSettings = archive_settings, state: ArchiveState | None = None,
                 clock: Callable[[], int] = now_ms,
                 capture: Callable[..., Awaitable[OptionCaptureResult]] | None = None) -> None:
        self.config, self.clock = config, clock
        self.root = Path(settings.LEAN_DATA_WRITE_ROOT)
        self.service_root = Path(__file__).resolve().parents[2]
        self.checkout = self.service_root.parent if (self.service_root.parent / "compose.yaml").is_file() else self.service_root
        self.state = state or ArchiveState(self.root / "option-archive-state.json")
        self.capture = capture
        self.scheduler = ArchiveScheduler(jobs=(
            ArchiveJob("capture", self.capture_due, self.capture_pass),
            ArchiveJob("snapshot", self.snapshot_due, self.snapshot_pass),
            ArchiveJob("backup", lambda now: self.daily_due("backup", now, config.BACKUP_START_MINUTE_ET), self.backup_pass),
            ArchiveJob("restore", self.restore_due, self.restore_pass),
        ), ownership=PostgresJobOwnership(), clock=clock)
        self._health: OptionArchiveHealth | None = None
        self._health_at = 0
        self._session_tasks: dict[tuple[str, int], asyncio.Task[OptionCaptureResult]] = {}

    async def start(self) -> None:
        now = self.clock()
        def initialize(state: dict[str, Any]) -> None:
            state.setdefault("baseline_ms", archive_baseline_ms(et_date_at_ms(now)))
            state.setdefault("snapshot_activation_ms", now)
        await asyncio.to_thread(self.state.update, initialize)
        self.scheduler.start()

    async def _cancel_collectors(self) -> None:
        tasks = tuple(self._session_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._session_tasks.clear()

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self._cancel_collectors()

    def daily_due(self, name: str, now: int, minute_et: int) -> bool:
        receipt = self.state.read().get("jobs", {}).get(name, {})
        day = et_date_at_ms(now)
        return (now >= wall_clock_ms(day, minute_et) and not regular_hours_pause(now, self.config.REQUEST_OPEN_MARGIN_MS)
                and receipt.get("completed_day_ms") != et_midnight_ms(day)
                and now >= receipt.get("retry_after_ms", 0))

    def capture_due(self, now: int) -> bool:
        return self.daily_due("capture", now, self.config.CAPTURE_START_MINUTE_ET) and not self.snapshot_due(now)

    def snapshot_due(self, now: int) -> bool:
        today = et_date_at_ms(now)
        try:
            window = session_window_for_date(today)
        except LookupError:
            return False
        due = window.close_ms_utc + self.config.SNAPSHOT_DELAY_MS
        receipt = self.state.read().get("jobs", {}).get("snapshot", {})
        return (due <= now < due + self.config.SNAPSHOT_WINDOW_MS
                and receipt.get("completed_day_ms") != et_midnight_ms(today)
                and now >= receipt.get("retry_after_ms", 0))

    def restore_due(self, now: int) -> bool:
        receipt = self.state.read().get("jobs", {}).get("restore", {})
        last = receipt.get("last_success_ms", 0)
        return self.daily_due("restore", now, self.config.BACKUP_START_MINUTE_ET) and now - last >= 604_800_000

    async def _job_update(self, job: str, **changes: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            state.setdefault("jobs", {}).setdefault(job, {}).update(changes)
        await asyncio.to_thread(self.state.update, update)
        self._health = None

    def should_pause(self) -> bool:
        return regular_hours_pause(self.clock(), self.config.REQUEST_OPEN_MARGIN_MS) or self.snapshot_due(self.clock())

    async def _record_session(self, symbol: str, session_ms: int, **changes: Any) -> None:
        def update(state: dict[str, Any]) -> None:
            state.setdefault("sessions", {}).setdefault(symbol, {}).setdefault(str(session_ms), {}).update(changes)
        await asyncio.to_thread(self.state.update, update)
        self._health = None

    async def _capture_and_record(self, underlying: str, session_ms: int, *, retry_exhausted: bool) -> OptionCaptureResult:
        from app.data_lake.option_session import read_option_session
        from app.data_lake.option_session_capture import capture_option_session
        recovery_block = self.state.read().get("provider_blocks", {}).get(underlying)
        await self._record_session(underlying, session_ms, started_ms=self.clock(), complete=False, recorded_ms=self.clock())
        token = archive_request.set(True)
        counts: dict[str, int] = {}
        try:
            try:
                result = await (self.capture or capture_option_session)(underlying, et_date_at_ms(session_ms), now_ms=self.clock(),
                    should_pause=self.should_pause, history_floor=archive_history_boundary(et_date_at_ms(self.clock())),
                    retry_exhausted=retry_exhausted, max_staging_bytes=self.config.STAGING_MAX_BYTES)
                stored = await asyncio.to_thread(read_option_session, underlying, et_date_at_ms(session_ms), check_underlying=True)
                if stored is not None:
                    counts = {"listed": stored.listed_count, "answered": stored.answered_count,
                              "empty": len(stored.confirmed_empty), "with_bars": len(stored.captured)}
                if result.failure is None and (stored is None or not stored.complete):
                    raise ValueError("capture returned success without a readable complete session container")
            except (LakeAdmissionError, ValueError, OSError) as exc:
                result = OptionCaptureResult.failed(OptionDataFailure("validation_failed", str(exc), underlying, et_date_at_ms(session_ms)))
            failure = result.failure
            retryable = failure is not None and failure.reason in {
                "provider_rate_limited", "provider_api_error", "lease_timeout", "io_error",
            }
            capacity_paused = _capacity_pause(result)
            paused = capacity_paused or (failure is not None and failure.reason == "lease_timeout" and self.should_pause())
            reason = f"{failure.reason}: {failure.detail}" if failure is not None else None
            changes = dict(**counts, complete=failure is None,
                failed=failure is not None and not paused, lost=failure is not None and failure.reason == "outside_data_plan",
                reason=reason, ended_ms=self.clock(), retryable=retryable, capacity_paused=capacity_paused,
                blocked_day_ms=None if retryable or failure is None else et_midnight_ms(et_date_at_ms(self.clock())))
            def record_provider_access(state: dict[str, Any]) -> None:
                state.setdefault("sessions", {}).setdefault(underlying, {}).setdefault(str(session_ms), {}).update(changes)
                capture_job = state.get("jobs", {}).get("capture", {})
                if failure is None and capture_job.get("phase") == "capacity_pause":
                    capture_job.update(phase="waiting", pause_reason=None)
                blocks = state.setdefault("provider_blocks", {})
                if failure is not None and failure.reason in ("provider_auth_error", "provider_entitlement_error"):
                    blocks[underlying] = {"session_ms": session_ms, "reason": failure.reason,
                        "detail": failure.detail, "recorded_ms": self.clock()}
                elif (failure is None and retry_exhausted and recovery_block is not None
                      and blocks.get(underlying) == recovery_block and recovery_block["session_ms"] == session_ms):
                    del blocks[underlying]
            await asyncio.to_thread(self.state.update, record_provider_access)
            self._health = None
            if failure is not None:
                logger.warning("Option archive could not complete a session", extra={"action": "option_archive_session_failed",
                    "underlying": underlying, "session_ms": session_ms, "reason": reason})
            return result
        except asyncio.CancelledError:
            await self._record_session(underlying, session_ms, complete=False, failed=False, retryable=True,
                reason="Archive collection was interrupted; verified progress is retained.", ended_ms=self.clock())
            raise
        finally:
            archive_request.reset(token)

    async def capture_session(self, underlying: str, session_ms: int, *, retry_exhausted: bool = False) -> OptionCaptureResult:
        key = (underlying, session_ms)
        task = self._session_tasks.get(key)
        if task is not None and not task.done():
            return await asyncio.shield(task)
        block = self.state.read().get("provider_blocks", {}).get(underlying)
        if block is not None and not (retry_exhausted and block["session_ms"] == session_ms):
            return OptionCaptureResult.failed(OptionDataFailure(block["reason"],
                f'{block["detail"]}; explicitly recover blocked session {block["session_ms"]} after correcting provider access',
                underlying, et_date_at_ms(session_ms)))
        task = asyncio.create_task(self._capture_and_record(underlying, session_ms, retry_exhausted=retry_exhausted))
        self._session_tasks[key] = task
        try:
            # The creator owns the collector lifetime. Joining requests may
            # leave independently, but ownership loss cancels and drains it.
            return await task
        finally:
            self._session_tasks.pop(key, None)

    async def capture_pass(self) -> None:
        started = self.clock()
        await self._job_update("capture", started_ms=started, ended_ms=None, phase="capture", failure=None, pause_reason=None)
        boundary = archive_history_boundary(et_date_at_ms(started))
        def preserve_window(state: dict[str, Any]) -> None:
            baseline = state.setdefault("baseline_ms", archive_baseline_ms(et_date_at_ms(started)))
            for day in expected_sessions(et_date_at_ms(baseline), et_date_at_ms(started) - timedelta(days=1)):
                session_ms = session_open_ms_utc(day)
                for symbol in UNDERLYINGS:
                    row = state.setdefault("sessions", {}).setdefault(symbol, {}).setdefault(str(session_ms),
                        {"complete": False, "recorded_ms": started})
                    if day < boundary and not row.get("complete") and not row.get("lost"):
                        row.update(lost=True, reason="The vendor window moved past this uncaptured session.", recorded_ms=started)
        state = await asyncio.to_thread(self.state.update, preserve_window)
        baseline = state["baseline_ms"]
        complete, lost = set(), set()
        for symbol in UNDERLYINGS:
            for key, row in state.get("sessions", {}).get(symbol, {}).items():
                item = (symbol, int(key))
                if row.get("complete"):
                    complete.add(item)
                elif row.get("lost"):
                    lost.add(item)
                elif et_date_at_ms(int(key)) < boundary:
                    await self._record_session(symbol, int(key), lost=True, complete=False,
                        reason="The vendor window moved past this uncaptured session.", recorded_ms=started)
                    lost.add(item)
        plan = plan_archive(now_ms=started, baseline_ms=baseline, complete=frozenset(complete), lost=frozenset(lost),
                            start_minute_et=self.config.CAPTURE_START_MINUTE_ET, margin_ms=self.config.REQUEST_OPEN_MARGIN_MS)
        blocked = set(state.get("provider_blocks", {}))
        paused = plan.phase in ("rth_pause", "waiting")
        capacity_reason = None
        try:
            for work in plan.work:
                if self.should_pause():
                    paused = True
                    break
                if work.underlying in blocked:
                    continue
                prior = state.get("sessions", {}).get(work.underlying, {}).get(str(work.session_ms), {})
                if prior.get("blocked_day_ms") == et_midnight_ms(et_date_at_ms(started)):
                    continue
                result = await self.capture_session(work.underlying, work.session_ms)
                failure = result.failure
                if failure is not None and _capacity_pause(result):
                    capacity_reason = failure.detail
                    break
                if failure is not None and failure.reason in ("provider_auth_error", "provider_entitlement_error"):
                    blocked.add(work.underlying)
                if failure is not None and failure.reason == "lease_timeout" and self.should_pause():
                    paused = True
                    break
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self._cancel_collectors()
            await self._job_update("capture", ended_ms=self.clock(), phase="interrupted", retry_after_ms=self.clock() + 300_000)
            raise
        except Exception as exc:
            await self._job_update("capture", ended_ms=self.clock(), phase="failed", failure=str(exc), retry_after_ms=self.clock()+300_000)
            raise
        retryable = any(row.get("retryable") and not row.get("complete") and not row.get("lost")
            for entries in self.state.read().get("sessions", {}).values() for row in entries.values())
        phase = "complete"
        if capacity_reason:
            phase = "capacity_pause"
        elif paused:
            phase = "snapshot_pause" if self.snapshot_due(self.clock()) else "rth_pause"
        await self._job_update("capture", ended_ms=self.clock(), phase=phase, pause_reason=capacity_reason,
            completed_day_ms=None if paused or retryable or capacity_reason else et_midnight_ms(et_date_at_ms(started)), retry_after_ms=self.clock() + 300_000)

    async def snapshot_pass(self) -> None:
        start = self.clock()
        day = et_date_at_ms(start)
        window = session_window_for_date(day)
        if not self.snapshot_due(start):
            raise BackupRefusedError("snapshot capture is outside its bounded evening due window")
        cutoff = window.close_ms_utc + self.config.SNAPSHOT_DELAY_MS + self.config.SNAPSHOT_WINDOW_MS
        complete = True
        existing = self.state.read().get("snapshots", {})
        for symbol in UNDERLYINGS:
            if existing.get(symbol, {}).get(str(window.open_ms_utc), {}).get("complete"):
                continue
            if self.clock() >= cutoff:
                complete = False
                break
            observation = await observe_chain(symbol, day, clock=self.clock, max_pages=self.config.MAX_SNAPSHOT_PAGES,
                                              should_pause=lambda: self.clock() >= cutoff)
            complete = complete and observation.complete
            result = await save_observation(observation)
            if result.failure is not None:
                raise RuntimeError(result.failure.detail)
            def save(state: dict[str, Any], captured_symbol: str = symbol, captured: ChainObservation = observation) -> None:
                state.setdefault("snapshots", {}).setdefault(captured_symbol, {})[str(window.open_ms_utc)] = {
                    "complete": captured.complete, "reason": captured.reason,
                    "field_gaps": captured.field_gaps, "started_ms": captured.started_ms, "ended_ms": captured.ended_ms,
                }
            await asyncio.to_thread(self.state.update, save)
        await self._job_update("snapshot", last_success_ms=self.clock() if complete else None,
            completed_day_ms=et_midnight_ms(day) if complete else None, retry_after_ms=self.clock() + 300_000)

    def _backup_roots(self) -> dict[str, Path]:
        return {str(active_root_id()): self.root}

    def _maintenance_pause(self) -> bool:
        return regular_hours_pause(self.clock(), self.config.REQUEST_OPEN_MARGIN_MS)

    async def backup_pass(self) -> None:
        config = self.config
        try:
            await asyncio.to_thread(validate_destination, config.BACKUP_DESTINATION, mount=config.BACKUP_MOUNT,
                checkout=self.checkout, roots=self._backup_roots(),
                expected_host_destination=config.BACKUP_HOST_DESTINATION,
                host_checkout=config.HOST_CHECKOUT, host_lake_root=config.HOST_LAKE_ROOT)
            runbook = self.service_root / "restore-data-lake.md"
            if not runbook.is_file():
                runbook = self.checkout / "docs/runbooks/restore-data-lake.md"
            manifest = await create_generation(destination=config.BACKUP_DESTINATION, roots=self._backup_roots(),
                clock=self.clock, should_pause=lambda: regular_hours_pause(self.clock(), config.REQUEST_OPEN_MARGIN_MS),
                restore_files={"restore_lake_generation.py": self.service_root / "scripts/restore_lake_generation.py",
                               "restore-data-lake.md": runbook})
            generation = config.BACKUP_DESTINATION / "generations" / manifest.generation_id
            await bounded_file_work(lambda paused: prune_generations(config.BACKUP_DESTINATION,
                daily_keep=config.BACKUP_DAILY_KEEP, weekly_keep=config.BACKUP_WEEKLY_KEEP, should_pause=paused), self._maintenance_pause)
            free = shutil.disk_usage(config.BACKUP_DESTINATION).free
            await self._job_update("backup", last_success_ms=manifest.completed_ms, generation_id=manifest.generation_id,
                captured_through_ms=manifest.started_ms, manifest_sha256=manifest_digest(generation), size_bytes=sum(file.size_bytes for file in manifest.files),
                free_bytes=free, failure=None, completed_day_ms=et_midnight_ms(et_date_at_ms(self.clock())))
        except Exception as exc:
            await self._job_update("backup", failure=str(exc), retry_after_ms=self.clock() + 300_000)
            raise

    async def restore_pass(self) -> None:
        started = self.clock()
        try:
            backup = self.state.read().get("jobs", {}).get("backup", {})
            generation_id = backup.get("generation_id")
            if generation_id is None:
                raise BackupRefusedError("there is no checked backup generation to test")
            generation = self.config.BACKUP_DESTINATION / "generations" / generation_id
            manifest = await asyncio.to_thread(verify_generation, generation, should_pause=self._maintenance_pause)
            schema = "archive_restore_" + uuid4().hex
            async with catalog_client.connection() as connection:
                await connection.execute(f'CREATE SCHEMA "{schema}"')
                try:
                    await connection.execute(f'CREATE TABLE "{schema}"."DataLakeArtifacts" (LIKE public."DataLakeArtifacts" INCLUDING ALL)')
                    with tempfile.TemporaryDirectory(prefix="lake-restore-") as folder:
                        roots = {root_id: Path(folder) / root_id for root_id in manifest.root_ids}
                        await restore_generation(generation=generation, roots=roots, connection=connection, schema=schema, should_pause=self._maintenance_pause)
                        counts = await bounded_file_work(lambda paused: decode_restored_artifacts(manifest, roots, should_pause=paused),
                                                         self._maintenance_pause)
                        restored_count = await connection.fetchval(f'SELECT COUNT(*) FROM "{schema}"."DataLakeArtifacts"')
                        if restored_count != manifest.catalog_rows:
                            raise BackupRefusedError("restored catalog row count differs from the generation")
                finally:
                    await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            await self._job_update("restore", last_success_ms=self.clock(), started_ms=started, ended_ms=self.clock(),
                generation_id=generation_id, manifest_sha256=manifest_digest(generation), counts=counts, result="passed",
                receipt=RestoreReceipt(generation_id=generation_id, manifest_sha256=manifest_digest(generation),
                    checked_at_ms=self.clock(), passed=True, decoded_counts=counts).model_dump(),
                failure=None, completed_day_ms=et_midnight_ms(et_date_at_ms(self.clock())))
        except Exception as exc:
            await self._job_update("restore", failure=str(exc), result="failed", retry_after_ms=self.clock() + 300_000)
            raise

    def health(self) -> OptionArchiveHealth:
        current = self.clock()
        if self._health is None or current - self._health_at >= 10_000:
            state = self.state.read()
            if self.config.ENABLED:
                try:
                    validate_destination(self.config.BACKUP_DESTINATION, mount=self.config.BACKUP_MOUNT,
                        checkout=self.checkout, roots=self._backup_roots(),
                        expected_host_destination=self.config.BACKUP_HOST_DESTINATION,
                        host_checkout=self.config.HOST_CHECKOUT, host_lake_root=self.config.HOST_LAKE_ROOT)
                except (OSError, ValueError, BackupRefusedError):
                    state.setdefault("jobs", {}).setdefault("backup", {})["failure"] = (
                        "The configured backup destination is unavailable; verify its mount and identity marker.")
            self._health = project_health(state, now_ms=current, config=self.config)
            self._health_at = current
        return self._health
