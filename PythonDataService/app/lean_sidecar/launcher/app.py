"""FastAPI app for the launcher service.

Run with:

    uvicorn app.lean_sidecar.launcher.app:app --host 127.0.0.1 --port 8090

The launcher binds to ``127.0.0.1`` by default. Auth is **mandatory**:
every request must carry an ``X-Launcher-Token`` header that matches
the launcher's token. The token resolves from
``LEAN_LAUNCHER_TOKEN`` env (operator override) or, when env is
unset, from a file the launcher auto-generates and writes to the
artifacts root. The data-plane container reads the same file through
the shared bind mount. Open-Q1 review-fix: previously the token was
opt-in (unset env on either side ⇒ no auth check), which on a
0.0.0.0-bound launcher with arbitrary user source accepted means no
auth in practice.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool

from app.lean_sidecar.config import DEFAULT_ARTIFACTS_ROOT
from app.lean_sidecar.launcher.models import (
    ExtractMetadataRequest,
    ExtractMetadataResponse,
    LaunchRequest,
    LaunchResponse,
)
from app.lean_sidecar.launcher.service import (
    LaunchRejectedError,
    extract_metadata,
    launch,
)
from app.lean_sidecar.launcher_auth import ensure_launcher_token

logger = logging.getLogger(__name__)

LAUNCHER_VERSION = "phase-1-spike-0"


def _artifacts_root() -> Path:
    """Resolve the host-side artifacts root from env or default.

    The launcher creates the root if missing (deploys often start with
    an empty volume). It rejects a configured path that already exists
    as a non-directory — a regular file at that path would silently
    cause every later mount + manifest write to fail with confusing
    errors; failing fast here is the boundary check that "validate
    inputs at system boundaries" calls for.
    """
    raw = os.environ.get("LEAN_LAUNCHER_ARTIFACTS_ROOT")
    root = Path(raw).resolve() if raw else DEFAULT_ARTIFACTS_ROOT.resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise RuntimeError(f"LEAN_LAUNCHER_ARTIFACTS_ROOT must be a directory; got {root}")
    return root


def _expected_token() -> str:
    """Return the launcher's auth token.

    Resolution: env → on-disk file → freshly-generated. The file is
    persisted at startup so the data plane (which doesn't share this
    process's env) can read the same value via the bind mount. The
    token is always non-None — Open-Q1 review-fix made auth
    mandatory; there is no "open localhost" mode.
    """
    return ensure_launcher_token(_artifacts_root())


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Materialize mandatory launcher auth before the first client request."""
    _expected_token()
    yield


app = FastAPI(
    title="LEAN Sidecar Launcher",
    description=(
        "Owns Podman API access for the LEAN Sidecar Lab. Receives launch "
        "requests from the data plane; invokes a pinned LEAN container "
        "with the security shape from lean-sidecar-lab.md."
    ),
    version=LAUNCHER_VERSION,
    lifespan=_lifespan,
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": LAUNCHER_VERSION}


@app.post("/launch", response_model=LaunchResponse)
async def post_launch(
    request: LaunchRequest,
    x_launcher_token: str | None = Header(default=None, alias="X-Launcher-Token"),
) -> LaunchResponse:
    expected = _expected_token()
    if x_launcher_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or wrong X-Launcher-Token",
        )
    try:
        return await run_in_threadpool(launch, request, artifacts_root=_artifacts_root())
    except LaunchRejectedError as e:
        # 4xx covers all "this request is malformed in a way the
        # launcher refuses to act on". The body carries a stable
        # ``reason`` label so the caller can branch without parsing.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": e.reason, "message": e.detail},
        ) from e


@app.post("/extract-metadata", response_model=ExtractMetadataResponse)
async def post_extract_metadata(
    request: ExtractMetadataRequest,
    x_launcher_token: str | None = Header(default=None, alias="X-Launcher-Token"),
) -> ExtractMetadataResponse:
    """Extract LEAN's image-bundled metadata into the named workspace.

    Same auth + error-envelope shape as ``/launch``. Lets the data plane
    delegate ``podman cp`` work to the launcher when its own container
    has no podman on PATH (the production topology — the data plane is
    not supposed to need podman directly).
    """
    expected = _expected_token()
    if x_launcher_token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or wrong X-Launcher-Token",
        )
    try:
        return await run_in_threadpool(extract_metadata, request, artifacts_root=_artifacts_root())
    except LaunchRejectedError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"reason": e.reason, "message": e.detail},
        ) from e
