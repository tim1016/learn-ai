"""Research runtime ownership: one coordinator per deployment, jobs stamped by generation (#3042).

The deployment's research coordinator is a singleton by database authority,
not by configuration: a session-level PostgreSQL advisory lock, held for the
runtime's process lifetime by one dedicated connection (never a pool member
— asyncpg's pool reset issues ``pg_advisory_unlock_all``). Claiming the lock
advances the singleton row's boot generation; every research dispatch
records its job under that generation. Liveness is the lock itself, read
from ``pg_locks`` — no heartbeat to trust and no lease to resurrect.

Two recoveries read that map. The API's startup sweep keeps failing
abandoned jobs, but skips jobs owned by the current, lock-holding
generation: an API restart must not close a healthy research runtime's
work. The research runtime, on startup, claims authority and closes only
the job records of generations older than its own — a prior runtime that
lost the lock is dead by the singleton rule, and its jobs are Finishable.
Legacy jobs dispatched before this table existed have no owner row and
stay API-owned, exactly as before (ADR 0065 D2, amended).

Ownership is execution metadata. It rides beside the existing study
attempt fence and never enters the scientific context digest, an
evaluation key, or any recorded result.

Formula: boot_generation advances by one per successful claim of the
singleton lock; a research job is protected from the API sweep iff
``research_job_owners.boot_generation`` equals the live row's generation
and ``pg_locks`` shows the coordinator's advisory lock granted.
Reference: #3042 / PRD #3034; ADR 0065 D2 (amended 2026-10-11);
``app.jobs.progress.fail_jobs_without_a_worker`` for the sweep it scopes.
Canonical implementation: this file.
Validated against: tests/research/persistence/test_ownership.py.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, Literal

import asyncpg

from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# Two-int advisory locks map one-to-one onto pg_locks (classid, objid), so
# liveness is one readable query; a single bigint key would need bit
# unpacking to ask the same question.
COORDINATOR_LOCK_CLASSID = 0x1926
COORDINATOR_LOCK_OBJID = 0x0002
SINGLETON_ROW_ID = "coordinator"

ResearchKind = Literal["golden_search", "grid_search", "walk_forward", "recency"]


class CoordinatorHeldElsewhere(RuntimeError):
    """Another live coordinator holds the singleton authority."""


class GenerationFencedError(RuntimeError):
    """The coordinator generation that owned this work is gone; the work stops (#3045).

    Distinct from a lost worker — which a replacement may continue under the
    same attempt — this fences everything the generation minted: no retry,
    renewal or replacement proceeds, and the record waits for explicit
    recovery (Finish for a study stage).
    """


@dataclass
class RuntimeAuthority:
    """A claimed coordinator authority: the lock, its connection, and the generation it minted.

    The connection is the authority: when it closes — crash, network loss,
    deliberate release — the lock is gone and this generation is dead. A
    runtime that notices the loss must stop dispatching; reconnecting later
    means a fresh claim and a new generation, never a resurrection.
    """

    instance_id: str
    role: str
    boot_generation: int
    connection: asyncpg.Connection
    # Serializes stamping on the dedicated connection: asyncpg forbids two
    # concurrent operations on one connection, and two dispatches may land
    # together. Everything else leaves the connection idle.
    stamp_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def connected(self) -> bool:
        return not self.connection.is_closed()

    async def release(self) -> None:
        """Give up authority: unlock, advance nothing, close the connection.

        The row keeps the generation — a released runtime is as dead as a
        crashed one, and the next claim advances past it.
        """
        if not self.connection.is_closed():
            await self.connection.execute("SELECT pg_advisory_unlock($1, $2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID)
            await self.connection.close()


async def claim_coordinator_authority(
    *, dsn: str, instance_id: str, role: str
) -> RuntimeAuthority:
    """Claim the deployment's singleton research-coordinator authority, or raise.

    Raises :class:`CoordinatorHeldElsewhere` when the lock is taken — the
    caller must stay unready and refuse research dispatch — and lets
    connection errors propagate: without the database there is no authority
    to claim, and the caller runs degraded (legacy behavior), never
    half-authoritative.
    """
    connection = await asyncpg.connect(dsn)
    try:
        acquired = await connection.fetchval(
            "SELECT pg_try_advisory_lock($1, $2)", COORDINATOR_LOCK_CLASSID, COORDINATOR_LOCK_OBJID
        )
        if not acquired:
            await connection.close()
            raise CoordinatorHeldElsewhere(
                "another research coordinator holds the singleton authority; refusing to become ready"
            )
        generation = await connection.fetchval("SELECT boot_generation FROM research_runtime_owners WHERE singleton = $1", SINGLETON_ROW_ID)
        boot_generation = 1 if generation is None else int(generation) + 1
        await connection.execute(
            """
            INSERT INTO research_runtime_owners (singleton, instance_id, role, boot_generation, claimed_at_ms)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (singleton) DO UPDATE
               SET instance_id = $2, role = $3, boot_generation = $4, claimed_at_ms = $5
            """,
            SINGLETON_ROW_ID,
            instance_id,
            role,
            boot_generation,
            now_ms_utc(),
        )
    except BaseException:
        if not connection.is_closed():
            await connection.close()
        raise
    return RuntimeAuthority(instance_id=instance_id, role=role, boot_generation=boot_generation, connection=connection)


async def record_dispatch(
    conn: asyncpg.Connection,
    authority: RuntimeAuthority,
    *, job_id: str, kind: ResearchKind, record_id: str
) -> None:
    """Stamp one dispatched research job with the authority that owns it.

    Idempotent per job id: a redelivery under the same id keeps the first
    stamp (the first dispatch's generation is the truth of who ran it).
    """
    await conn.execute(
        """
        INSERT INTO research_job_owners
            (job_id, kind, record_id, owner_instance_id, boot_generation, dispatched_at_ms)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (job_id) DO NOTHING
        """,
        job_id,
        kind,
        record_id,
        authority.instance_id,
        authority.boot_generation,
        now_ms_utc(),
    )


async def protected_job_ids(conn: asyncpg.Connection) -> set[str]:
    """Job ids the API's startup sweep must not touch: live research ownership.

    A job is protected exactly when its owning generation is the singleton
    row's current generation and that generation's lock is still granted.
    Older generations are dead by the singleton rule; a dead lock protects
    nothing. ``pg_locks`` is cluster-wide while advisory locks are
    per-database, so the lock row is pinned to the current database — a
    coordinator holding the same key in another database must not make a
    dead generation here read as protected.
    """
    rows = await conn.fetch(
        """
        SELECT owners.job_id
          FROM research_job_owners owners
          JOIN research_runtime_owners runtime ON runtime.singleton = $1
         WHERE owners.boot_generation = runtime.boot_generation
           AND EXISTS (
                SELECT 1 FROM pg_locks
                 WHERE locktype = 'advisory' AND classid = $2 AND objid = $3 AND granted
                   AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
           )
        """,
        SINGLETON_ROW_ID,
        COORDINATOR_LOCK_CLASSID,
        COORDINATOR_LOCK_OBJID,
    )
    return {row["job_id"] for row in rows}


async def abandoned_job_ids(conn: asyncpg.Connection, *, up_to_generation: int) -> list[str]:
    """Research job ids of generations strictly older than ``up_to_generation``.

    The coordinator may close these records: their runtimes are dead by the
    singleton rule. Completed or closed jobs among them are left alone —
    the caller closes only records still reading queued/running.
    """
    rows = await conn.fetch(
        "SELECT job_id FROM research_job_owners WHERE boot_generation < $1", up_to_generation
    )
    return [row["job_id"] for row in rows]


# ── Process state ─────────────────────────────────────────────────────────

# The API's lifespan sets the authority it claimed at startup; the research
# runtime (once it exists as its own process, #3043) holds its own. One
# process, one slot — the swap under the lock keeps a release-then-claim
# window from dispatching with a dead authority.
_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"authority": None, "held_elsewhere": False}


def set_process_authority(authority: RuntimeAuthority | None, *, held_elsewhere: bool = False) -> None:
    with _STATE_LOCK:
        _STATE["authority"] = authority
        _STATE["held_elsewhere"] = held_elsewhere


def process_authority() -> RuntimeAuthority | None:
    with _STATE_LOCK:
        return _STATE["authority"]


def dispatch_refused() -> bool:
    """Whether this process must refuse new research dispatch (#3042, singleton).

    True only when startup saw the singleton lock held by another live
    coordinator. Without that observation — single-process deployments, a
    database that was unreachable at startup, tests — dispatch runs under
    the legacy lifecycle.
    """
    with _STATE_LOCK:
        return bool(_STATE["held_elsewhere"])


async def stamp_dispatch(*, job_id: str, kind: ResearchKind, record_id: str) -> None:
    """Record one research dispatch under the process's authority (#3042, #3047).

    The lock and the stamp ride the process's writer loop, whatever loop the
    calling route served: an ``asyncio.Lock`` never wakes a future set from
    another loop, so stamps arriving from both the app loop and the writer
    loop would park the second caller forever (#3047 review). A writer stall
    past the call bound raises ``CallerStoppedWaitingError`` — the stamp
    keeps running on the loop; the router answers retryable.

    A process without authority records nothing (legacy lifecycle — the job
    is API-owned and the sweep closes it on restart). A process that saw the
    lock held elsewhere, or whose authority connection has died, refuses:
    raising :class:`CoordinatorHeldElsewhere` for the router to surface.
    """
    from app.research.persistence.db import writer_loop_call

    await writer_loop_call(_stamp_on_authority(job_id=job_id, kind=kind, record_id=record_id))


async def _stamp_on_authority(*, job_id: str, kind: ResearchKind, record_id: str) -> None:
    """The stamp itself, on the writer loop that owns ``stamp_lock``.

    The insert runs on the lock-owning session itself, not a pool member:
    a check on one connection followed by an insert on another is not a
    fence — a successor could already hold the lock while the stale insert
    lands for a dead generation. On the lock's own connection, a dead
    authority fails the insert, and a successor's claim implies this
    session's lock — and its inserts — are gone (#3042).
    """
    authority = process_authority()
    if authority is None:
        if dispatch_refused():
            raise CoordinatorHeldElsewhere("research dispatch refused: another coordinator holds the singleton authority")
        return
    if not authority.connected:
        # The lock connection died: this generation is fenced, and a runtime
        # that lost authority must not dispatch on its way down.
        raise CoordinatorHeldElsewhere("research dispatch refused: this coordinator's authority connection was lost")
    async with authority.stamp_lock:
        await record_dispatch(authority.connection, authority, job_id=job_id, kind=kind, record_id=record_id)


async def startup_authority_and_protection(
    *, dsn: str | None, instance_id: str, role: str
) -> tuple[RuntimeAuthority | None, Callable[[], AbstractSet[str]] | None]:
    """The composition a data-plane process runs at startup (#3042).

    Claims the singleton authority when free, records that it is held
    elsewhere when not, and hands back the sweep's protected set: the jobs
    owned by whichever generation currently holds the lock — including
    another runtime's. A database that cannot be reached leaves the process
    degraded under the legacy lifecycle (no authority, no protection), the
    same behavior as before this module existed.
    """
    if not dsn:
        set_process_authority(None)
        return None, None
    authority: RuntimeAuthority | None = None
    try:
        # Through the canonical locked apply: a bare DDL replay here would race
        # a concurrently-starting data plane's ensure_schema and bypass the
        # migration ledger the digest test pins.
        from app.research.persistence.schema import ensure_schema

        probe = await asyncpg.connect(dsn)
        try:
            await ensure_schema(probe)
        finally:
            await probe.close()
        authority = await claim_coordinator_authority(dsn=dsn, instance_id=instance_id, role=role)
        set_process_authority(authority)
    except CoordinatorHeldElsewhere as exc:
        logger.warning("research dispatch disabled: %s", exc)
        set_process_authority(None, held_elsewhere=True)
    except (asyncpg.PostgresConnectionError, OSError) as exc:
        # Only a genuinely unreachable database degrades to the legacy
        # lifecycle — the behavior that predates this module.
        logger.warning("running without research runtime ownership (database unreachable at startup): %s", exc)
        set_process_authority(None)
        return None, None
    except asyncpg.PostgresError as exc:
        # Reachable but refusing us — permissions, a DDL race, a lock
        # timeout: fail closed for research dispatch instead of degrading
        # to the unstamped legacy path, which is exactly the split-brain
        # a second healthy coordinator could ride (#3042).
        logger.error("research dispatch disabled: ownership setup failed against a reachable database: %s", exc)
        set_process_authority(None, held_elsewhere=True)
        return None, None

    async def protected() -> AbstractSet[str]:
        conn = await asyncpg.connect(dsn)
        try:
            return await protected_job_ids(conn)
        finally:
            await conn.close()

    return authority, protected


async def startup_protection_only(
    *, dsn: str | None
) -> Callable[[], AbstractSet[str]] | None:
    """The sweep's protected set for a process that forwards research execution (#3043).

    An API configured with ``RESEARCH_EXECUTION_MODE=research`` claims no
    authority — the isolated runtime owns it — but its startup sweep still
    must not close the live runtime's jobs. This reads the same protection
    without claiming anything or touching process state.
    """
    if not dsn:
        return None

    async def protected() -> AbstractSet[str]:
        conn = await asyncpg.connect(dsn)
        try:
            return await protected_job_ids(conn)
        finally:
            await conn.close()

    return protected
