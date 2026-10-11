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

from fastapi import Depends, FastAPI

from app.config import settings
from app.routers import golden_search
from app.security.data_plane_control import require_data_plane_control_secret
from app.utils.error_handlers import install_fleet_control_error_handler

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


async def _claim_authority_or_die() -> None:
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
            "(another runtime holds it, or the database is unreachable); refusing to start"
        )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    refuse_reload()
    await _claim_authority_or_die()
    from app.research.persistence.db import ensure_schema_at_startup

    schema_task = asyncio.create_task(ensure_schema_at_startup(), name="research-runtime-schema-ensure")
    try:
        yield
    finally:
        schema_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await schema_task
        from app.research.persistence import ownership

        authority = ownership.process_authority()
        if authority is not None:
            await authority.release()
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
async def healthz() -> dict[str, str]:
    """Reachability plus the one fact an operator needs: authority held or not."""
    from app.research.persistence import ownership

    authority = ownership.process_authority()
    return {
        "status": "ok" if authority is not None else "degraded",
        "version": RUNTIME_VERSION,
        "boot_generation": str(authority.boot_generation) if authority is not None else "none",
    }
