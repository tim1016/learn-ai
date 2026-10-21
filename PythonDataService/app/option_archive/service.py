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
from app.data_lake.root_identity import active_root_id
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, session_window_for_date
from app.option_archive.backup import (
    BackupRefusedError,
    RestoreReceipt,
    bounded_file_work,
    create_generation,
    manifest_digest,
    prune_generations,
    restore_generation,
    validate_destination,
    verify_generation,
)
from app.option_archive.health import project_health
from app.option_archive.planner import (
    RETRY_TONIGHT,
    UNDERLYINGS,
    SessionOutcome,
    archive_baseline_ms,
    archive_history_boundary,
    classify,
    next_daily_run_ms,
    plan_archive,
    regular_hours_pause,
)
from app.option_archive.restored_reads import decode_restored_lake, read_restored_catalog
from app.option_archive.scheduler import ArchiveJob, ArchiveScheduler, JobOwnership, PostgresJobOwnership
from app.option_archive.settings import STATE_FILE_NAME, ArchiveSettings, archive_settings
from app.option_archive.snapshot import ChainObservation, SnapshotOutsideWindowError, observe_chain, save_observation
from app.option_archive.state import (
    ArchiveState,
    ArchiveStateDoc,
    JobReceipt,
    ProviderBlock,
    SessionReceipt,
    SnapshotReceipt,
)
from app.schemas.option_archive import ArchiveJobName, ArchivePhase, OptionArchiveHealth
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

logger = logging.getLogger(__name__)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class OptionArchiveService:
    def __init__(self, *, config: ArchiveSettings = archive_settings, state: ArchiveState | None = None,
                 clock: Callable[[], int] = now_ms,
                 capture: Callable[..., Awaitable[OptionCaptureResult]] | None = None,
                 ownership: JobOwnership | None = None) -> None:
        self.config, self.clock = config, clock
        self.root = Path(settings.LEAN_DATA_WRITE_ROOT)
        self.service_root = Path(__file__).resolve().parents[2]
        self.checkout = self.service_root.parent if (self.service_root.parent / "compose.yaml").is_file() else self.service_root
        self.state = state or ArchiveState(self.root / STATE_FILE_NAME)
        self.capture = capture
        self.scheduler = ArchiveScheduler(jobs=(
            ArchiveJob("capture", self.capture_due, self.capture_pass),
            ArchiveJob("snapshot", self.snapshot_due, self.snapshot_pass),
            ArchiveJob("backup", lambda now: self.daily_due("backup", now, config.BACKUP_START_MINUTE_ET), self.backup_pass),
            ArchiveJob("restore", self.restore_due, self.restore_pass),
        ), ownership=ownership or PostgresJobOwnership(), clock=clock, record=self._job_update,
            retry_backoff_ms=config.RETRY_BACKOFF_MS)
        self._health: OptionArchiveHealth | None = None
        self._health_at = 0
        self._session_tasks: dict[tuple[str, int], asyncio.Task[SessionOutcome]] = {}

    async def start(self) -> None:
        now = self.clock()
        def initialize(state: ArchiveStateDoc) -> None:
            if state.baseline_ms is None:
                state.baseline_ms = archive_baseline_ms(et_date_at_ms(now))
            if state.snapshot_activation_ms is None:
                state.snapshot_activation_ms = now
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

    def daily_due(self, name: ArchiveJobName, now: int, minute_et: int) -> bool:
        receipt = self.state.read().job(name)
        return next_daily_run_ms(now_ms=now, minute_et=minute_et, completed_day_ms=receipt.completed_day_ms,
                                 retry_after_ms=receipt.retry_after_ms, margin_ms=self.config.REQUEST_OPEN_MARGIN_MS) <= now

    def capture_due(self, now: int) -> bool:
        return self.daily_due("capture", now, self.config.CAPTURE_START_MINUTE_ET) and not self.snapshot_due(now)

    def _snapshot_window_due(self, now: int, receipt: JobReceipt) -> bool:
        today = et_date_at_ms(now)
        try:
            window = session_window_for_date(today)
        except LookupError:
            return False
        due = window.close_ms_utc + self.config.SNAPSHOT_DELAY_MS
        return (due <= now < due + self.config.SNAPSHOT_WINDOW_MS
                and receipt.completed_day_ms != et_midnight_ms(today)
                and now >= (receipt.retry_after_ms or 0))

    def snapshot_due(self, now: int) -> bool:
        return self._snapshot_window_due(now, self.state.read().job("snapshot"))

    def restore_due(self, now: int) -> bool:
        last = self.state.read().job("restore").last_success_ms or 0
        return self.daily_due("restore", now, self.config.BACKUP_START_MINUTE_ET) and now - last >= 604_800_000

    async def _job_update(self, job: ArchiveJobName, **changes: Any) -> None:
        def update(state: ArchiveStateDoc) -> None:
            receipt = state.jobs.setdefault(job, JobReceipt())
            for name, value in changes.items():
                setattr(receipt, name, value)
        await asyncio.to_thread(self.state.update, update)
        self._health = None

    def _pause_rule(self, snapshot: JobReceipt) -> Callable[[], bool]:
        """Whether capture must yield now: to regular hours or to a due evening observation.

        The caller reads the snapshot receipt once, so the collector's check before
        every request reads no file. A receipt older than the call can only keep the
        observation due, so it errs toward pausing; the next pass reads it afresh.
        """
        def pausing() -> bool:
            now = self.clock()
            return regular_hours_pause(now, self.config.REQUEST_OPEN_MARGIN_MS) or self._snapshot_window_due(now, snapshot)
        return pausing

    def _pause_phase(self) -> ArchivePhase:
        return "rth_pause" if regular_hours_pause(self.clock(), self.config.REQUEST_OPEN_MARGIN_MS) else "snapshot_pause"

    async def _record_session(self, symbol: str, session_ms: int, **changes: Any) -> None:
        def update(state: ArchiveStateDoc) -> None:
            row = state.session(symbol, session_ms)
            for name, value in changes.items():
                setattr(row, name, value)
        await asyncio.to_thread(self.state.update, update)
        self._health = None

    async def _capture_and_record(self, underlying: str, session_ms: int, *, retry_exhausted: bool) -> SessionOutcome:
        from app.options_backtest.option_session_capture import capture_option_session, read_session_completeness
        day = et_date_at_ms(session_ms)
        state = self.state.read()
        recovery_block = state.provider_blocks.get(underlying)
        pausing = self._pause_rule(state.job("snapshot"))
        await self._record_session(underlying, session_ms, started_ms=self.clock(), outcome=None, recorded_ms=self.clock())
        token = archive_request.set(True)
        counts: dict[str, int] = {}
        try:
            try:
                result = await (self.capture or capture_option_session)(underlying, day, now_ms=self.clock(),
                    should_pause=pausing, history_floor=archive_history_boundary(et_date_at_ms(self.clock())),
                    retry_exhausted=retry_exhausted)
                stored, complete = await asyncio.to_thread(read_session_completeness, underlying, day)
                if stored is not None:
                    counts = {"listed": stored.listed_count, "answered": stored.answered_count,
                              "empty": len(stored.confirmed_empty), "with_bars": len(stored.captured)}
                if result.failure is None and not complete:
                    raise ValueError("capture returned success without a readable complete session container")
            except (LakeAdmissionError, ValueError, OSError) as exc:
                result = OptionCaptureResult.failed(OptionDataFailure("validation_failed", str(exc), underlying, day))
            failure = result.failure
            outcome = classify(failure, pausing=pausing())
            reason = f"{failure.reason}: {failure.detail}" if failure is not None else None
            def record(state: ArchiveStateDoc) -> None:
                row = state.session(underlying, session_ms)
                for name, value in counts.items():
                    setattr(row, name, value)
                row.outcome, row.reason, row.ended_ms = outcome, reason, self.clock()
                if failure is not None and outcome is SessionOutcome.BLOCK_PROVIDER:
                    state.provider_blocks[underlying] = ProviderBlock(session_ms=session_ms, reason=failure.reason,
                        detail=failure.detail, recorded_ms=self.clock())
                elif (outcome is SessionOutcome.COMPLETE and retry_exhausted and recovery_block is not None
                      and state.provider_blocks.get(underlying) == recovery_block and recovery_block.session_ms == session_ms):
                    del state.provider_blocks[underlying]
            await asyncio.to_thread(self.state.update, record)
            self._health = None
            if failure is not None:
                logger.warning("Option archive could not complete a session", extra={"action": "option_archive_session_failed",
                    "underlying": underlying, "session_ms": session_ms, "reason": reason, "outcome": outcome})
            return outcome
        except asyncio.CancelledError:
            await self._record_session(underlying, session_ms, outcome=SessionOutcome.PAUSED,
                reason="Archive collection was interrupted; verified progress is retained.", ended_ms=self.clock())
            raise
        finally:
            archive_request.reset(token)

    async def capture_session(self, underlying: str, session_ms: int, *, retry_exhausted: bool = False) -> SessionOutcome:
        key = (underlying, session_ms)
        task = self._session_tasks.get(key)
        if task is not None and not task.done():
            return await asyncio.shield(task)
        block = self.state.read().provider_blocks.get(underlying)
        if block is not None and not (retry_exhausted and block.session_ms == session_ms):
            logger.warning("Option archive session waits for its blocked underlying's recovery", extra={
                "action": "option_archive_session_blocked", "underlying": underlying, "session_ms": session_ms,
                "blocked_session_ms": block.session_ms, "reason": block.reason})
            return SessionOutcome.BLOCK_PROVIDER
        task = asyncio.create_task(self._capture_and_record(underlying, session_ms, retry_exhausted=retry_exhausted))
        self._session_tasks[key] = task
        try:
            # The creator owns the collector lifetime: cancelling it cancels and
            # drains this collector. Joining requests may leave independently.
            return await task
        finally:
            self._session_tasks.pop(key, None)

    async def capture_pass(self) -> None:
        started = self.clock()
        today = et_date_at_ms(started)
        await self._job_update("capture", phase="capture")
        boundary = archive_history_boundary(today)
        def preserve_window(state: ArchiveStateDoc) -> None:
            if state.baseline_ms is None:
                state.baseline_ms = archive_baseline_ms(today)
            for day in expected_sessions(et_date_at_ms(state.baseline_ms), today - timedelta(days=1)):
                for symbol in UNDERLYINGS:
                    state.sessions.setdefault(symbol, {}).setdefault(session_open_ms_utc(day), SessionReceipt(recorded_ms=started))
            for symbol in UNDERLYINGS:
                for session_ms, row in state.sessions.get(symbol, {}).items():
                    if et_date_at_ms(session_ms) < boundary and not row.complete and row.outcome is not SessionOutcome.LOST:
                        row.outcome, row.recorded_ms = SessionOutcome.LOST, started
                        row.reason = "The vendor window moved past this uncaptured session."
        state = await asyncio.to_thread(self.state.update, preserve_window)
        rows = [(symbol, session_ms, row) for symbol in UNDERLYINGS for session_ms, row in state.sessions.get(symbol, {}).items()]
        plan = plan_archive(now_ms=started, baseline_ms=state.baseline_ms or archive_baseline_ms(today),
                            complete=frozenset((symbol, session_ms) for symbol, session_ms, row in rows if row.complete),
                            lost=frozenset((symbol, session_ms) for symbol, session_ms, row in rows if row.outcome is SessionOutcome.LOST),
                            start_minute_et=self.config.CAPTURE_START_MINUTE_ET, margin_ms=self.config.REQUEST_OPEN_MARGIN_MS)
        blocked = set(state.provider_blocks)
        pausing = self._pause_rule(state.job("snapshot"))
        phase: ArchivePhase = plan.phase if plan.phase in ("rth_pause", "waiting") else "complete"
        for work in plan.work:
            if pausing():
                phase = self._pause_phase()
                break
            prior = state.sessions[work.underlying].get(work.session_ms)
            if work.underlying in blocked or (prior is not None and prior.skipped_on(today)):
                continue
            outcome = await self.capture_session(work.underlying, work.session_ms)
            if outcome is SessionOutcome.CAPACITY_PAUSED:
                phase = "capacity_pause"
                break
            if outcome is SessionOutcome.PAUSED:
                phase = self._pause_phase()
                break
            if outcome is SessionOutcome.BLOCK_PROVIDER:
                blocked.add(work.underlying)
            await asyncio.sleep(0)
        open_tonight = any(row.outcome in RETRY_TONIGHT
                           for entries in self.state.read().sessions.values() for row in entries.values())
        await self._job_update("capture", phase=phase,
            completed_day_ms=et_midnight_ms(today) if phase == "complete" and not open_tonight else None)

    async def snapshot_pass(self) -> None:
        start = self.clock()
        day = et_date_at_ms(start)
        if not self.snapshot_due(start):
            raise SnapshotOutsideWindowError("snapshot capture is outside its bounded evening due window")
        window = session_window_for_date(day)
        cutoff = window.close_ms_utc + self.config.SNAPSHOT_DELAY_MS + self.config.SNAPSHOT_WINDOW_MS
        complete = True
        existing = self.state.read().snapshots
        for symbol in UNDERLYINGS:
            prior = existing.get(symbol, {}).get(window.open_ms_utc)
            if prior is not None and prior.complete:
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
            def save(state: ArchiveStateDoc, captured_symbol: str = symbol, captured: ChainObservation = observation) -> None:
                state.snapshots.setdefault(captured_symbol, {})[window.open_ms_utc] = SnapshotReceipt(
                    complete=captured.complete, reason=captured.reason, field_gaps=captured.field_gaps,
                    started_ms=captured.started_ms, ended_ms=captured.ended_ms)
            await asyncio.to_thread(self.state.update, save)
        await self._job_update("snapshot", last_success_ms=self.clock() if complete else None,
            completed_day_ms=et_midnight_ms(day) if complete else None)

    def _backup_roots(self) -> dict[str, Path]:
        return {str(active_root_id()): self.root}

    def _maintenance_pause(self) -> bool:
        return regular_hours_pause(self.clock(), self.config.REQUEST_OPEN_MARGIN_MS)

    def _check_destination(self) -> None:
        config = self.config
        if config.BACKUP_HOST_DESTINATION is None:
            raise BackupRefusedError("set OPTION_ARCHIVE_BACKUP_HOST_DESTINATION to the backup folder's absolute host path")
        validate_destination(config.BACKUP_DESTINATION, checkout=self.checkout, roots=self._backup_roots(),
            expected_host_destination=config.BACKUP_HOST_DESTINATION,
            host_checkout=config.HOST_CHECKOUT, host_lake_root=config.HOST_LAKE_ROOT)

    async def backup_pass(self) -> None:
        config = self.config
        await asyncio.to_thread(self._check_destination)
        runbook = self.service_root / "restore-data-lake.md"
        if not runbook.is_file():
            runbook = self.checkout / "docs/runbooks/restore-data-lake.md"
        manifest = await create_generation(destination=config.BACKUP_DESTINATION, roots=self._backup_roots(),
            clock=self.clock, should_pause=lambda: regular_hours_pause(self.clock(), config.REQUEST_OPEN_MARGIN_MS),
            restore_files={"restore_lake_generation.py": self.service_root / "app/option_archive/lake_restore.py",
                           "restore-data-lake.md": runbook})
        generation = config.BACKUP_DESTINATION / "generations" / manifest.generation_id
        unverified = await bounded_file_work(lambda paused: prune_generations(config.BACKUP_DESTINATION,
            clock=self.clock, daily_keep=config.BACKUP_DAILY_KEEP, weekly_keep=config.BACKUP_WEEKLY_KEEP, should_pause=paused), self._maintenance_pause)
        free = shutil.disk_usage(config.BACKUP_DESTINATION).free
        await self._job_update("backup", last_success_ms=manifest.completed_ms, generation_id=manifest.generation_id,
            captured_through_ms=manifest.started_ms, manifest_sha256=manifest_digest(generation), size_bytes=sum(file.size_bytes for file in manifest.files),
            free_bytes=free, unverified_generations=unverified, completed_day_ms=et_midnight_ms(et_date_at_ms(self.clock())))

    async def restore_pass(self) -> None:
        generation_id = self.state.read().job("backup").generation_id
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
                    artifacts = await read_restored_catalog(connection, schema)
                    counts = await bounded_file_work(lambda paused: decode_restored_lake(artifacts, roots, should_pause=paused),
                                                     self._maintenance_pause)
                    restored_count = await connection.fetchval(f'SELECT COUNT(*) FROM "{schema}"."DataLakeArtifacts"')
                    if restored_count != manifest.catalog_rows:
                        raise BackupRefusedError("restored catalog row count differs from the generation")
            finally:
                await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        digest = manifest_digest(generation)
        await self._job_update("restore", last_success_ms=self.clock(), generation_id=generation_id, manifest_sha256=digest,
            counts=counts, receipt=RestoreReceipt(generation_id=generation_id, manifest_sha256=digest,
                checked_at_ms=self.clock(), passed=True, decoded_counts=counts),
            completed_day_ms=et_midnight_ms(et_date_at_ms(self.clock())))

    def health(self) -> OptionArchiveHealth:
        current = self.clock()
        if self._health is None or current - self._health_at >= 10_000:
            problem = None
            if self.config.ENABLED:
                try:
                    self._check_destination()
                except (OSError, ValueError, BackupRefusedError):
                    problem = "The configured backup destination is unavailable; verify its mount and identity marker."
            self._health = project_health(self.state.read(), now_ms=current, config=self.config, destination_problem=problem)
            self._health_at = current
        return self._health
