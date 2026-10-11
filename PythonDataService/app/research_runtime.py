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
from typing import TYPE_CHECKING

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


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    refuse_reload()
    authority = await _claim_authority_or_die()
    await _close_abandoned_generations(authority)
    from app.research.persistence.db import ensure_schema_at_startup

    schema_task = asyncio.create_task(ensure_schema_at_startup(), name="research-runtime-schema-ensure")
    try:
        yield
    finally:
        schema_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await schema_task
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
