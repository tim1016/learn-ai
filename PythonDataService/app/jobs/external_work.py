"""Real acquisition receipts on ordinary job events; observations never renew a lease."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from app.jobs import progress
from app.jobs.checkpoints import GatedLease
from app.jobs.progress import ProgressEmitter
from app.utils.timestamps import now_ms_utc

MAX_EXTERNAL_WAIT_MS = 1_860_000
WORK_PUBLICATION_INTERVAL_S = 0.5
MAX_PENDING_WORK_RECEIPTS = 4096
STALL_SECONDS = 65.0


class WorkReceipts:
    """A bounded off-loop publisher; callbacks enqueue, and their owner drains before releasing files."""

    def __init__(self, emit: ProgressEmitter, check: Callable[[], None]) -> None:
        self.emit, self._check_owner = emit, check
        self._admission = threading.Lock()
        self._active = True
        self._failure: BaseException | None = None
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue(maxsize=MAX_PENDING_WORK_RECEIPTS)
        self._seen: set[str] = set()
        self._cursor = 0
        self._published = 0.0
        self._check_requested = 0.0
        self._deadline_ms: int | None = None
        self._phase: str | None = None
        self._thread = threading.Thread(target=self._publish, name=f"job-receipts-{emit.job_id[:8]}", daemon=True)
        self._thread.start()

    def _offer(self, kind: str, value: Any = None) -> None:
        # No I/O under this lock: callbacks also run on the provider's event
        # loop, which the publisher's generation query may need to use.
        with self._admission:
            if not self._active:
                return
            if self._failure is not None:
                raise self._failure
            if kind == "check":
                now = time.monotonic()
                if now - self._check_requested < WORK_PUBLICATION_INTERVAL_S:
                    return
                self._check_requested = now
            try:
                self._queue.put_nowait((kind, value))
            except queue.Full as exc:
                raise ValueError("acquisition receipt queue bound exceeded") from exc

    def check(self) -> None:
        """Request a cancellation/fence observation; a poll publishes and renews nothing."""
        self._offer("check")

    def phase(self, name: str) -> None:
        self._offer("phase", name)

    def progress(self, done: int, total: int) -> None:
        self._offer("progress", (done, total))

    def work(self, receipt: dict[str, Any]) -> None:
        # The original I/O start owns its deadline, even if publication waits
        # behind a slow job store or ownership query.
        self._offer("work", {**receipt, "occurred_at_ms": now_ms_utc()})

    def _publish(self) -> None:
        while (item := self._queue.get()) is not None:
            if self._failure is not None:
                continue
            kind, value = item
            try:
                if kind == "check":
                    self._check_owner()
                elif kind == "phase":
                    if value != self._phase:
                        self._check_owner()
                        self._phase = value
                        self._renew()
                        self.emit.phase(value)
                elif kind == "progress":
                    self._check_owner()
                    self._cursor += 1
                    self._renew()
                    self.emit.progress(*value, unit="sessions", detail={"work_cursor": self._cursor})
                    self._published = time.monotonic()
                else:
                    self._work(value)
            except BaseException as exc:
                # The next producer check and the final drain both observe
                # this failure. The worker consumes no later publication.
                with self._admission:
                    self._failure = exc

    def _renew(self) -> None:
        remaining_s = 0 if self._deadline_ms is None else max(0, (self._deadline_ms - now_ms_utc() + 999) // 1000)
        if not progress.get_redis().expire(progress.lease_key(self.emit.job_id), max(300, remaining_s + 5)):
            raise progress.JobCancelled("the acquisition worker lease expired")

    def _work(self, receipt: dict[str, Any]) -> None:
        kind = receipt["kind"]
        if kind == "work":
            key = receipt["work_kind"] + ":" + receipt["identity"]
            if key in self._seen:
                return
            if len(self._seen) >= 1_000_000:
                raise ValueError("acquisition receipt bound exceeded")
            self._seen.add(key)
            self._cursor += 1
            if time.monotonic() - self._published < WORK_PUBLICATION_INTERVAL_S:
                return
            self._check_owner()
            self._renew()
            self.emit.progress(0, 0, unit="artifacts", detail={"work_cursor": self._cursor})
        elif kind == "external_wait":
            bound = receipt["bound_ms"]
            if type(bound) is not int or not 0 < bound <= MAX_EXTERNAL_WAIT_MS:
                raise ValueError("external acquisition wait exceeds its declared bound")
            self._check_owner()
            deadline_ms = receipt["occurred_at_ms"] + bound
            if deadline_ms <= now_ms_utc():
                raise TimeoutError("bounded acquisition wait expired before publication")
            self._deadline_ms = deadline_ms
            self._renew()
            self.emit.emit_event("job.external_wait", {"deadline_ms": self._deadline_ms})
        elif kind == "external_wait_end":
            self._check_owner()
            self._deadline_ms = None
            self._renew()
            self.emit.emit_event("job.external_wait_end", {})
        else:
            raise ValueError("unknown acquisition progress receipt")
        self._published = time.monotonic()

    def deactivate(self) -> None:
        """Stop new callbacks, drain pending publication, and report any publisher failure."""
        with self._admission:
            if not self._active:
                return
            self._active = False
        self._queue.put(None)
        self._thread.join()
        if self._failure is not None:
            raise self._failure


class ExternalJobObserver:
    """Relay each real job event once; polling and lost dispatch replies provide no work evidence."""

    def __init__(self, emit: ProgressEmitter, lease: GatedLease) -> None:
        self.emit, self.lease = emit, lease
        self.deadline = time.monotonic() + STALL_SECONDS
        self._last: dict[str, tuple[int, int]] = {}
        self._cursors: dict[str, int] = {}
        self._phase: str | None = None
        self._waiting = False
        self._receipts = 0

    def accept(self, job_id: str, entry_id: str, event: dict[str, Any]) -> None:
        stamp = tuple(int(value) for value in entry_id.split("-"))
        if stamp <= self._last.get(job_id, (-1, -1)):
            return
        self._last[job_id] = stamp
        kind = event["type"]
        advanced = False
        if kind == "job.phase" and event["phase"] != self._phase:
            self._phase = event["phase"]
            self.emit.phase(self._phase)
            self.lease.phase(self._phase)
            advanced = True
        elif kind == "job.progress" and event.get("work_cursor", 0) > self._cursors.get(job_id, 0):
            self._cursors[job_id] = event["work_cursor"]
            self._receipts += 1
            self.lease.child_checkpoint({"cursor": self._receipts, "cursor_kind": "admitted_artifacts", "phase": self._phase})
            self.emit.progress(event["current"], event["total"], unit=event["unit"])
            advanced = True
        elif kind == "job.external_wait":
            remaining_ms = event["deadline_ms"] - now_ms_utc()
            if not 0 < remaining_ms <= MAX_EXTERNAL_WAIT_MS:
                raise ValueError("external acquisition wait is expired or exceeds its declared bound")
            self.lease.bounded_external_wait(f"{job_id}:{entry_id}", event["deadline_ms"])
            self.deadline = time.monotonic() + remaining_ms / 1000 + 5
            self._waiting = True
        elif kind == "job.external_wait_end":
            self.lease.external_wait_finished()
            self._waiting = False
            advanced = True
        if advanced and not self._waiting:
            self.deadline = time.monotonic() + STALL_SECONDS
