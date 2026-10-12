"""Catalog admission for exact lake bytes (#2456).

The filesystem rename precedes the database commit. Existence is therefore
not evidence of publication. Every consumer of a managed lake file verifies
its bytes against a committed, root- and mode-scoped receipt before parsing.
There is deliberately no positive cache: a refresh revokes admission even
when the file's inode and timestamps have not changed yet.

The research workers' verdicts batch (#3047, PRD #3034 slice 12):
:class:`AdmissionBatcher` answers checks that are already pending with one
set-based receipt lookup — no gathering delay, no positive cache surviving
a round trip — on a loop of its own inside a bounded connection envelope.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import asyncpg

from app.config import settings
from app.data_lake import catalog_client
from app.data_lake.path_policy import lake_root_for
from app.data_lake.root_identity import LakeRootIdentityError, read_marker
from app.utils.background_loop import CallerStoppedWaitingError, run_on_background_loop

logger = logging.getLogger(__name__)

ReceiptTuple = tuple[UUID, str, str, str, int]
ReceiptLookup = Callable[[list[ReceiptTuple]], Awaitable[frozenset[int]]]


class LakeAdmissionError(RuntimeError):
    """The bytes have no verifiable committed lake receipt; retry capture."""


def read_committed_bytes(path: Path) -> bytes:
    """Read once, then admit those exact bytes. Call from a worker thread.

    Plain LEAN reference fixtures retain their file-only contract. Managed
    lake paths require an identity marker even when Postgres is unavailable.
    """
    payload = path.read_bytes()
    root = lake_root_for(path) or lake_root_for(path.resolve())
    if root is None:
        return payload
    verify_committed_digest(path, root, hashlib.sha256(payload).hexdigest(), len(payload))
    return payload


@dataclass(frozen=True)
class AdmissionCheck:
    """One read's receipt identity: everything the tuple-scoped lookup keys on (#3047)."""

    data_root_id: UUID
    price_adjustment_mode: str
    file_path: str
    file_sha256: str
    file_size_bytes: int

    def as_tuple(self) -> ReceiptTuple:
        return (self.data_root_id, self.price_adjustment_mode, self.file_path, self.file_sha256, self.file_size_bytes)


def admission_check_for(path: Path, root: Path, digest: str, size: int) -> AdmissionCheck:
    """Resolve one read to its receipt tuple: root-relative path plus the root's identity marker.

    The marker read and path walk are file I/O — they run on the caller's
    thread, never on the admission loop (#3047). Refusal taxonomy is the
    per-file one: an unresolvable root is a per-file gap, not an outage.
    """
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(root.resolve()).as_posix()
        marker = read_marker(root.parent.parent)
    except (LakeRootIdentityError, ValueError) as exc:
        raise LakeAdmissionError(f"{path}: cannot verify committed lake root identity") from exc
    if marker is None:
        raise LakeAdmissionError(f"{path}: no root identity for committed lake admission")
    return AdmissionCheck(marker.data_root_id, root.name, relative, digest, size)


def verify_committed_digest(path: Path, root: Path, digest: str, size: int) -> None:
    """Admit a digest another process already read the bytes for (#3044).

    The single-check form: the in-process engine path and any caller with
    one read to answer. The research workers' relay batches through
    :class:`AdmissionBatcher` instead (#3047); the verdict is the same
    either way. Call from a worker thread. Refusal raises the identical
    taxonomy — per-file :class:`LakeAdmissionError`, outage
    :class:`CatalogUnavailableError`.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True
    if running_loop:
        raise RuntimeError("lake admission requires a worker thread; use asyncio.to_thread")
    check = admission_check_for(path, root, digest, size)
    try:

        async def admitted() -> bool:
            await catalog_client.init_pool()
            return await catalog_client.has_committed_file_receipt(
                check.data_root_id, check.price_adjustment_mode, check.file_path,
                check.file_sha256, check.file_size_bytes,
            )

        if not run_on_background_loop(admitted(), timeout=35):
            raise LakeAdmissionError(f"{path}: no committed catalog receipt for these bytes; retry capture")
    except (catalog_client.CatalogUnavailableError, asyncpg.PostgresError,
            OSError, TimeoutError, CallerStoppedWaitingError) as exc:
        # An outage is not a per-file gap. Propagate it so a range probe
        # stops after one failure instead of paying a timeout for every day.
        raise catalog_client.CatalogUnavailableError(
            f"{path}: cannot verify committed lake receipt ({type(exc).__name__})"
        ) from exc


# ── The batched verdict relay (#3047, PRD #3034 slice 12) ─────────────────

ADMISSION_CALL_BOUND_S = 35.0

# Shutdown and bound-expiry answers share the outage type: a relay that
# cannot answer is an infrastructure event, never evidence about the bytes
# (#3047 review) — the same taxonomy the single-check relay kept. A fresh
# instance per site: one shared exception object raced its traceback across
# the waiters' threads.
_SHUTTING_DOWN_MESSAGE = "the admission batcher is shutting down; the catalog's answer is unresolved"


def _shutting_down() -> catalog_client.CatalogUnavailableError:
    return catalog_client.CatalogUnavailableError(_SHUTTING_DOWN_MESSAGE)


class _AdmissionWaiter:
    """One submitting thread's parking spot: its check, and the verdict that releases it."""

    __slots__ = ("admitted", "check", "enqueued_at", "event", "failure")

    def __init__(self, check: AdmissionCheck) -> None:
        self.check = check
        self.enqueued_at = time.perf_counter()
        self.event = threading.Event()
        self.admitted: bool | None = None
        self.failure: BaseException | None = None

    def answer(self, admitted: bool) -> None:
        self.admitted = admitted
        self.event.set()

    def fail(self, failure: BaseException) -> None:
        self.failure = failure
        self.event.set()


class AdmissionBatcher:
    """Answers concurrent admission checks with one set-based receipt lookup each (#3047).

    Requests already pending when a lookup starts share it; a lone request
    runs alone — no gathering delay, no waiting for another worker, and each
    worker keeps its own read order (verdicts fan out per requester the
    moment the lookup lands). Batching lives strictly inside one drain:
    identical tuples deduplicate for that lookup only, and the next batch —
    however soon — checks fresh tuples again, so no positive receipt
    survives a round trip and revocation stays per-read. The loop is this
    batcher's alone — never the API's, the fleet's or the study writer's —
    and carries nothing but SQL: hashing, marker reads and path walks stay
    on the callers' threads. A catalog outage answers the whole batch with
    one typed :class:`~app.data_lake.catalog_client.CatalogUnavailableError`
    instead of one full timeout per remaining artifact.
    """

    def __init__(
        self,
        *,
        pool_max_size: int,
        lookup: ReceiptLookup | None = None,
        call_bound_s: float = ADMISSION_CALL_BOUND_S,
    ) -> None:
        self._pool_max_size = pool_max_size
        self._lookup = lookup
        self._call_bound_s = call_bound_s
        self._pending: list[_AdmissionWaiter] = []
        self._wakeup: asyncio.Event | None = None
        # The instrumentation #3047 asks for, as plain counters the loop and
        # the readers share under the GIL: batch-size distribution, request
        # versus SQL counts, round trips saved, last query latency.
        self._requests = 0
        self._sql_lookups = 0
        self._outages = 0
        self._deduplicated = 0
        self._batch_sizes: Counter[int] = Counter()
        self._last_query_ms = 0.0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="admission-loop", daemon=True)
        self._thread.start()
        self._serve_boot = asyncio.run_coroutine_threadsafe(self._start(), self._loop)
        self._serve_task: asyncio.Future[None] | None = None

    async def _start(self) -> None:
        self._wakeup = asyncio.Event()
        self._serve_task = asyncio.ensure_future(self._serve())

    def submit(self, check: AdmissionCheck) -> bool:
        """One read's verdict, blocking the calling thread at most the call bound.

        Raises the relay's established taxonomy: the outage type for an
        unreachable catalog *and for a verdict that never arrived inside the
        bound* — a slow or shutting-down relay is an infrastructure event,
        never evidence about the bytes (#3047 review) — and
        :class:`LakeAdmissionError` for a refused receipt.
        """
        waiter = _AdmissionWaiter(check)

        def deposit() -> None:
            self._pending.append(waiter)
            if self._wakeup is not None:
                self._wakeup.set()

        self._loop.call_soon_threadsafe(deposit)
        if not waiter.event.wait(timeout=self._call_bound_s):
            raise catalog_client.CatalogUnavailableError(
                "no admission verdict in time; the catalog's answer is unresolved"
            )
        if waiter.failure is not None:
            raise waiter.failure
        assert waiter.admitted is not None
        return waiter.admitted

    async def _serve(self) -> None:
        wakeup = self._wakeup
        assert wakeup is not None
        pool_ready = False
        while True:
            while not self._pending:
                await wakeup.wait()
                wakeup.clear()
            batch = self._pending
            self._pending = []
            served_at = time.perf_counter()
            unique: dict[AdmissionCheck, None] = {}
            for waiter in batch:
                unique.setdefault(waiter.check, None)
            checks = list(unique)
            try:
                if self._lookup is None and not pool_ready:
                    await catalog_client.init_pool(max_size=self._pool_max_size)
                    pool_ready = True
                lookup = self._lookup if self._lookup is not None else catalog_client.has_committed_file_receipts
                admitted = await lookup([check.as_tuple() for check in checks])
            except asyncio.CancelledError:
                # Shutdown reached into an in-flight batch: fail it with the
                # outage, never leave a waiter to its own bound (#3047 review).
                for waiter in batch:
                    waiter.fail(_shutting_down())
                raise
            except (catalog_client.CatalogUnavailableError, asyncpg.PostgresError,
                    OSError, TimeoutError, CallerStoppedWaitingError) as exc:
                outage = catalog_client.CatalogUnavailableError(
                    f"cannot verify committed lake receipts ({type(exc).__name__}); the whole batch refuses together"
                )
                for waiter in batch:
                    waiter.fail(outage)
                self._record(len(batch), 0, served_at, outage=True)
                continue
            admitted_checks = {checks[position] for position in admitted}
            for waiter in batch:
                waiter.answer(waiter.check in admitted_checks)
            self._record(len(batch), len(checks), served_at)

    def _record(self, requests: int, unique: int, served_at: float, *, outage: bool = False) -> None:
        self._last_query_ms = (time.perf_counter() - served_at) * 1000.0
        self._requests += requests
        self._batch_sizes.update({requests: 1})
        if outage:
            self._outages += 1
        else:
            self._sql_lookups += 1
            self._deduplicated += max(requests - unique, 0)
        logger.info(
            "research admission batch",
            extra={
                "action": "research_admission_batch",
                "requests": requests,
                "unique_tuples": unique,
                "query_ms": round(self._last_query_ms, 2),
                "outage": outage,
            },
        )

    def facts(self) -> dict[str, object]:
        """The instrumentation #3047 asks for: batch-size distribution, request versus SQL counts, query latency."""
        return {
            "requests": self._requests,
            "sql_lookups": self._sql_lookups,
            "outages": self._outages,
            "deduplicated": self._deduplicated,
            "batch_sizes": dict(self._batch_sizes),
            "round_trips_saved": max(self._requests - self._sql_lookups, 0),
            "last_query_ms": round(self._last_query_ms, 2),
        }

    def close(self) -> None:
        """Stop serving, fail everything still parked with the typed outage, close this loop's pool."""
        if not self._thread.is_alive():
            return

        async def _stop() -> None:
            for waiter in self._pending:
                waiter.fail(_shutting_down())
            self._pending = []
            if self._serve_task is not None:
                self._serve_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._serve_task
            await catalog_client.close_pool()
            self._loop.stop()

        asyncio.run_coroutine_threadsafe(_stop(), self._loop)
        if not self._thread.join(timeout=5.0):
            logger.error("the admission loop did not stop within 5s of close; its thread is abandoned")


_batcher: AdmissionBatcher | None = None
_batcher_lock = threading.Lock()


def start_admission_batcher() -> AdmissionBatcher:
    """The process's admission batcher, sized from the connection envelope (#3047)."""
    global _batcher
    with _batcher_lock:
        if _batcher is None:
            _batcher = AdmissionBatcher(pool_max_size=settings.RESEARCH_ADMISSION_CONNECTIONS)
        return _batcher


def admission_batcher() -> AdmissionBatcher:
    """:func:`start_admission_batcher` for callers that only read the singleton."""
    return start_admission_batcher()


def stop_admission_batcher() -> None:
    """Teardown counterpart of :func:`start_admission_batcher`."""
    global _batcher
    with _batcher_lock:
        batcher, _batcher = _batcher, None
    if batcher is not None:
        batcher.close()
