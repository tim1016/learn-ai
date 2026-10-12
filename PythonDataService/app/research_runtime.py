"""The isolated research runtime: one process that owns research job execution (#3043, PRD #3034 slice 8).

A deliberately small ASGI application — the shape of the LEAN sidecar
launcher, not of ``app.main`` — that mounts only the internal research
dispatch routes and starts only research resources: the singleton
coordinator authority (``app.research.persistence.ownership``), the research
schema, and the job store. No broker stack, no clerk stores, no provider
subscriptions, no fleet surfaces: this process starts none of them, and its
dispatch surface is golden search's alone (grid, walk-forward and recency
join it in later PRD #3034 slices).

Deployment (ADR 0077): its own container and cgroup — eight CPUs, 16 GiB,
512 PIDs, the qualification envelope from PRD #3034 — started behind the
``research`` compose profile, opt-in per deployment. The API forwards
authorized dispatches here when ``RESEARCH_EXECUTION_MODE=research``; it
claims no authority of its own then, so an API restart cannot take this
process's jobs with it (#3042's ownership scoping). The evaluation inside is
still the existing serial engine; the bounded-worker boundary is the next
slice (#3044), and until it ships nothing here enables concurrency.

Run with::

    uvicorn app.research_runtime:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10

Reference: #3043 / PRD #3034; ADR 0065 D2 (per-runtime ownership);
ADR 0055's serial-engine amendment; ``app.lean_sidecar.launcher.app`` for
the minimal-app precedent; ``scripts/render_fleet_topology.py`` for the
topology this service appears in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import golden_search
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.error_handlers import install_fleet_control_error_handler

if TYPE_CHECKING:
    from app.research.persistence import ownership

logger = logging.getLogger(__name__)

RUNTIME_VERSION = "research-runtime/v1"


def refuse_reload() -> None:
    """Startup error under a development reloader (#3043).

    Reload restarts the process on every file touch — mid-evaluation — and a
    runtime that silently re-executes imports under a held advisory lock is
    exactly the corruption the singleton fence exists to prevent. The check
    reads the same ``UVICORN_RELOAD`` the compose launch script branches on,
    and the reloader's own ``--reload`` argument when invoked directly; both
    are configuration the operator controls, so refusing is honest, not
    heuristic.
    """
    enabled = os.environ.get("UVICORN_RELOAD", "").strip().lower() in ("1", "true", "yes", "on")
    if enabled or "--reload" in sys.argv:
        raise RuntimeError(
            "the research runtime refuses to start under a development reloader; "
            "restart without --reload (research execution plus reload is a startup error, #3043)"
        )


async def _claim_authority_or_die() -> ownership.RuntimeAuthority:
    """Claim the deployment's research coordinator authority or fail startup.

    A second live coordinator must not become ready (#3042): the lock is the
    authority, and a runtime that cannot take it has nothing to serve. A
    database that cannot be reached also fails startup here — unlike the API,
    this process exists to own research execution, and starting without the
    ownership it exists to hold would be a lie.
    """
    from app.research.persistence import ownership

    authority, _protected = await ownership.startup_authority_and_protection(
        dsn=settings.POSTGRES_URL or None,
        instance_id=f"research:{os.getpid()}",
        role="research",
    )
    if authority is None:
        raise RuntimeError(
            "the research runtime could not claim the singleton coordinator authority "
            "(another runtime holds it, or ownership setup failed); refusing to start"
        )
    return authority


async def _close_abandoned_generations(authority: ownership.RuntimeAuthority) -> None:
    """Close the job records of generations older than the one just claimed (#3042).

    A prior runtime that lost the lock is dead by the singleton rule, but
    its jobs' records would read queued/running until the 24 h TTL and
    refuse Finish. Only older generations close here — the API's own legacy
    jobs and anything stamped under the new generation are not this
    process's to touch. Whether a lost runtime's results committed before
    it died is #3045's uncertain-write question; this closes records,
    never accounting.
    """
    import asyncpg

    from app.research.persistence import ownership

    dsn = settings.POSTGRES_URL
    if not dsn:
        return
    conn = await asyncpg.connect(dsn)
    try:
        job_ids = await ownership.abandoned_job_ids(conn, up_to_generation=authority.boot_generation)
    finally:
        await conn.close()
    if not job_ids:
        return
    from app.jobs.progress import close_orphaned_job_records

    await asyncio.to_thread(close_orphaned_job_records, job_ids)


async def enforce_connection_envelope() -> dict[str, Any]:
    """Cap and validate this process's PostgreSQL envelope (#3047, PRD #3034 slice 12).

    Admission verdicts get their own loop and pool at
    ``RESEARCH_ADMISSION_CONNECTIONS`` (started right after this passes);
    coordination, materialization and study persistence share the background
    writer loop, pre-initialized here at ``RESEARCH_COORDINATION_CONNECTIONS``
    minus one — the coordinator authority's dedicated lock connection, held
    since the claim above, counts inside that bound. The sum must stay
    inside the twelve the connection inventory reserves for this runtime,
    and the inventory's free-connection verdict — whenever the operator
    supplied the server maximum — must hold at least twenty spare. An
    envelope that fails either check refuses startup; nothing here ever
    raises the server's ``max_connections`` to make room.
    """
    from app.data_lake import catalog_client
    from app.research.benchmark import CONNECTION_INVENTORY_RESEARCH_CAP, connection_inventory
    from app.research.persistence.db import writer_loop_call

    admission_n = settings.RESEARCH_ADMISSION_CONNECTIONS
    coordination_n = settings.RESEARCH_COORDINATION_CONNECTIONS
    if admission_n + coordination_n > CONNECTION_INVENTORY_RESEARCH_CAP:
        raise RuntimeError(
            f"the configured research connection envelope ({admission_n} admission + "
            f"{coordination_n} coordination) exceeds the {CONNECTION_INVENTORY_RESEARCH_CAP}-connection "
            "research cap the deployment's connection inventory reserves; refusing to start"
        )
    await writer_loop_call(
        catalog_client.init_pool(max_size=coordination_n - 1), timeout=30.0
    )
    inventory = connection_inventory(research_cap=admission_n + coordination_n)
    if inventory["meets_required_free"] is False:
        raise RuntimeError(f"the connection inventory refuses this envelope: {inventory['mismatch']}")
    return inventory


async def _ensure_schema_via_writer_loop() -> None:
    """The research schema rides the capped background pool (#3047).

    The app loop this lifespan runs on must never own a catalog pool — a
    lazy ``ensure_schema_at_startup`` here would size one at the module
    ceiling and blow the envelope this runtime just enforced.
    """

    async def _ensure() -> None:
        from app.research.persistence.db import connection

        async with connection():
            # Opening this loop's first pooled connection applies every pending version.
            pass

    from app.research.persistence.db import writer_loop_call

    try:
        await writer_loop_call(_ensure())
    except Exception:
        logger.warning(
            "research schema could not be ensured; research routes retry on first use",
            extra={"action": "research_schema_ensure_failed"},
            exc_info=True,
        )
    else:
        logger.info("research schema ensured", extra={"action": "research_schema_ensured"})


def _start_shared_worker_pool() -> None:
    """Create the shared pool now, so a width the envelope cannot fit fails startup (#3048 AC 7).

    The eager create also declares the operator profile the configured
    width runs under — the singleton the dispatch factory binds every
    dispatch to as one owner.
    """
    from app.research.golden_search.shared_pool import (
        SUBMISSION_FACTOR,
        shared_worker_pool,
        worker_profile,
    )

    count = settings.RESEARCH_WORKER_COUNT
    if count < 2:
        return
    pool = shared_worker_pool(
        workers=count,
        submission_bound=SUBMISSION_FACTOR * count,
        payload_bound_bytes=settings.RESEARCH_POOL_PAYLOAD_BOUND_BYTES,
        result_bound_bytes=settings.RESEARCH_POOL_RESULT_BOUND_BYTES,
    )
    logger.info(
        "research worker pool started: %s",
        worker_profile(count),
        extra={"action": "research_pool_started", **pool.facts()},
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from app.data_lake.admission import start_admission_batcher, stop_admission_batcher
    from app.research.golden_search.shared_pool import close_shared_pool

    refuse_reload()
    authority = await _claim_authority_or_die()
    await enforce_connection_envelope()
    await _close_abandoned_generations(authority)
    batcher = start_admission_batcher()
    _start_shared_worker_pool()
    schema_task = asyncio.create_task(_ensure_schema_via_writer_loop(), name="research-runtime-schema-ensure")
    try:
        yield
    finally:
        schema_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await schema_task
        batcher.close()
        stop_admission_batcher()
        close_shared_pool()
        from app.research.persistence import ownership

        held = ownership.process_authority()
        if held is not None:
            await held.release()
        ownership.set_process_authority(None)


app = FastAPI(
    title="learn-ai Research Runtime",
    description=(
        "Owns research job execution for the deployment (PRD #3034): the "
        "existing serial Golden Search stage, dispatched by the API across "
        "the internal boundary. Progress, results and cancellation ride the "
        "shared job store exactly as before."
    ),
    version=RUNTIME_VERSION,
    lifespan=_lifespan,
)

# The internal dispatch surface only. Study commands, approvals and reads
# stay on the API; this process receives bound jobs and runs them. The
# dispatch route demands the data-plane control secret — the authenticated
# internal boundary the PRD requires — where the API's own jobs-internal
# mounts rely on network placement alone.
app.include_router(
    golden_search.jobs_router,
    prefix="/api/jobs-internal",
    tags=["jobs-internal"],
    dependencies=[Depends(require_data_plane_control_secret)],
)

install_fleet_control_error_handler(app)
# The same host fence every served app installs: the runtime answers the
# compose network's names for it, nothing else.
from fastapi.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.get_trusted_hosts())


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Reachability plus the one fact an operator needs: authority held or not.

    Held means connected as well as claimed: a runtime whose dedicated lock
    connection has died holds nothing and must stop passing healthchecks —
    the compose check only fails on the HTTP status, so the degraded branch
    answers 503, never a 200 with a sad body (#3042).
    """
    from app.research.persistence import ownership

    authority = ownership.process_authority()
    held = authority is not None and authority.connected
    return JSONResponse(
        status_code=200 if held else 503,
        content={
            "status": "ok" if held else "degraded",
            "version": RUNTIME_VERSION,
            "boot_generation": str(authority.boot_generation) if authority is not None else "none",
        },
    )
